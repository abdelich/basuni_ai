"""
Бот члена совета: мониторит council_inbox и court_decisions, по новому сообщению выносит позицию и голос (За/Против), публикует в council_deliberations.
После трёх голосов случайный член оглашает решение; при одобрении — исполнение через агента.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from discord import Message  # type: ignore[reportMissingImports]
from discord.ext import commands  # type: ignore[reportMissingImports]
from sqlalchemy import select, update  # type: ignore[reportMissingImports]

from sqlalchemy.exc import IntegrityError  # type: ignore[reportMissingImports]

from src.core.agent import Agent
from src.core.agent_ctx import AgentContext
from src.core.db import get_db
from src.core.models import CouncilCase, CouncilVote
from src.core.discord_guild import get_law_block_async
from src.roles.base import RoleBot, RoleDeps
from src.roles.council.tools import make_council_tools

logger = logging.getLogger("basuni.council.bot")

_case_creation_lock = asyncio.Lock()

# Реакции старейшины: галочка = запрос легитимен, идёт в процесс; крестик = нелигитимный, не рассматривать
ELDER_APPROVE_EMOJIS = ("✅", "👍", "✔", "☑", "✔️", "white_check_mark")
ELDER_REJECT_EMOJIS = ("❌", "👎", "✖", "👎🏻", "❎")


def _member_index_from_role_key(role_key: str) -> int:
    """council_1 -> 1, council_2 -> 2, council_3 -> 3."""
    if role_key.startswith("council_"):
        try:
            return int(role_key.split("_")[1])
        except (IndexError, ValueError):
            pass
    return 1


class CouncilBot(RoleBot):
    def __init__(self, deps: RoleDeps, role_key: str = "council_1", **kwargs: object) -> None:
        super().__init__(role_key=role_key, deps=deps, command_prefix="!", **kwargs)
        self._member_index = _member_index_from_role_key(role_key)
        self._inbox_channel_id: int | None = None
        self._court_decisions_channel_id: int | None = None
        self._watch_channel_ids: list[int] = []
        self._processing_cases: set[tuple[int, int]] = set()

    def _agent_context(self, guild_id: int, extra: dict[str, Any] | None = None) -> AgentContext:
        cfg = self.config
        channel_ids = {}
        for purpose in ("inbox", "deliberations", "court_decisions", "execution_blog"):
            ch_id = cfg.channel_for_role(self.role_key, purpose)
            if ch_id:
                channel_ids[purpose] = ch_id
        law_ch = (cfg.channels() or {}).get("law_judicial_precedents")
        if law_ch:
            channel_ids["law_judicial_precedents"] = int(law_ch)
        extra = extra or {}
        extra["member_index"] = self._member_index
        return AgentContext(
            guild_id=guild_id,
            channel_ids=channel_ids,
            bot=self,
            db_session_factory=self.deps.db_session_factory,
            extra=extra,
        )

    _EXECUTOR_SYSTEM_PROMPT = (
        "Ты — исполнитель решений совета. Твоя ЕДИНСТВЕННАЯ задача — вызывать инструменты "
        "для исполнения принятого решения. Не голосуй, не обсуждай, не рассуждай. "
        "Читай текст решения и НЕМЕДЛЕННО вызывай нужные инструменты: "
        "create_role, publish_new_law_article, add_role_to_member, и т.д. "
        "После исполнения ОБЯЗАТЕЛЬНО вызови post_council_outcome_to_deliberations и post_to_execution_blog."
    )

    def _build_agent(self, ctx: AgentContext) -> Agent:
        execution_mode = bool(ctx.extra.get("execution_case_id"))
        system_prompt = self._EXECUTOR_SYSTEM_PROMPT if execution_mode else self.load_system_prompt()
        tools = make_council_tools(ctx, ctx.extra.get("member_index", 1), execution_mode=execution_mode)
        base_url = getattr(self.config, "openai_base_url", None)
        return Agent(
            system_prompt=system_prompt,
            tools=tools,
            api_key=self.deps.openai_api_key,
            model=self.config.openai_model,
            max_tool_rounds=12 if execution_mode else 8,
            base_url=base_url,
            stop_after_tools={"post_to_execution_blog"} if execution_mode else None,
        )

    async def setup_hook(self) -> None:
        await super().setup_hook()
        self._inbox_channel_id = self.config.channel_for_role(self.role_key, "inbox")
        self._court_decisions_channel_id = self.config.channel_for_role(self.role_key, "court_decisions")
        self._watch_channel_ids = self.config.watch_channel_ids(self.role_key)
        logger.info(
            "Совет (член %s): inbox=%s, court_decisions=%s, watch=%s",
            self._member_index,
            self._inbox_channel_id,
            self._court_decisions_channel_id,
            self._watch_channel_ids,
        )
        self.loop.create_task(self._resume_pending_votes())

    async def _message_elder_verdict(self, message: Message) -> bool | None:
        """
        Проверка реакций старейшины на сообщении.
        Галочка (✅ и т.д.) от пользователя с ролью «старейшина» — легитимный запрос, идёт в процесс.
        Крестик (❌, 👎 и т.д.) от старейшины — нелигитимный запрос, не рассматривать.
        Возвращает: True = одобрено старейшиной, False = отклонено старейшиной, None = нет реакции старейшины.
        """
        guild = message.guild
        if not guild:
            return None
        elder_role_id = (self.config.role_ids() or {}).get("elder") or 0
        if not elder_role_id:
            return True
        try:
            for reaction in message.reactions:
                emoji_str = str(getattr(reaction, "emoji", "")).strip()
                if not emoji_str:
                    continue
                is_approve = emoji_str in ELDER_APPROVE_EMOJIS or (len(emoji_str) <= 4 and any(e in emoji_str for e in ("✅", "👍", "✔")))
                is_reject = emoji_str in ELDER_REJECT_EMOJIS or (len(emoji_str) <= 4 and any(e in emoji_str for e in ("❌", "👎", "✖")))
                if not is_approve and not is_reject:
                    continue
                async for user in reaction.users():
                    if getattr(user, "bot", False):
                        continue
                    member = guild.get_member(user.id)
                    if not member:
                        try:
                            member = await guild.fetch_member(user.id)
                        except Exception:
                            continue
                    if not member:
                        continue
                    if any(r.id == elder_role_id for r in member.roles):
                        if is_reject:
                            return False
                        if is_approve:
                            return True
        except Exception as e:
            logger.warning("Совет: ошибка при проверке реакций старейшины: %s", e)
        return None

    async def _resume_pending_votes(self) -> None:
        """При старте: найти open-дела, где этот член ещё не голосовал, и доголосовать."""
        await self.wait_until_ready()
        await asyncio.sleep(5 + (self._member_index - 1) * 3)
        try:
            async with get_db() as session:
                result = await session.execute(
                    select(CouncilCase).where(CouncilCase.status == "open")
                )
                open_cases = result.scalars().all()
            for case in open_cases:
                if await self._has_voted(case.id, self._member_index):
                    continue
                logger.info(
                    "Совет (член %s): восстановление — дело №%s открыто, голос не подан, голосую",
                    self._member_index, case.id,
                )
                await self._process_case_by_id(case.guild_id, case.id)
        except Exception as e:
            logger.exception("Совет (член %s): ошибка восстановления незавершённых голосований: %s", self._member_index, e)

    async def _process_message_as_council_case(self, message: Message) -> None:
        """Общая логика: создать/взять дело, если этот член ещё не голосовал — запустить агента, подсчитать голоса."""
        guild = message.guild
        if not guild:
            return
        channel_id = message.channel.id
        content = (message.content or "").strip()
        if not content:
            return
        # Stagger: каждый член начинает с задержкой, чтобы не долбить API одновременно
        await asyncio.sleep((self._member_index - 1) * 3)
        source = "elder" if channel_id == self._inbox_channel_id else "court"
        try:
            case = await self._get_or_create_case(guild.id, channel_id, message.id, content, source)
        except Exception as e:
            logger.exception("Ошибка get_or_create_case: %s", e)
            return
        if not case:
            return
        if await self._has_voted(case.id, self._member_index):
            return
        lock_key = (case.id, self._member_index)
        if lock_key in self._processing_cases:
            return
        self._processing_cases.add(lock_key)
        try:
            law_block = await get_law_block_async(
                self, guild.id, max_chars=10000,
                reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
                config=self.config,
            )
            user_content = (
                f"[ ДЕЛО СОВЕТА №{case.id} ]\n\n"
                f"Источник: {source}. Содержание:\n{content[:2000]}\n\n"
                "По закону (блок выше) и по своему характеру вынеси позицию и голос. "
                "Голос только **За** (yes) или **Против** (no). Воздержаний нет. "
                f"Вызови post_my_deliberation(case_id=\"{case.id}\", thoughts=\"...\", vote=\"yes\" или \"no\")."
            )
            messages_for_llm = [
                {"role": "user", "content": law_block + "\n\n---\n\n" + user_content},
            ]
            ctx = self._agent_context(guild.id, extra={"current_case_id": case.id, "member_index": self._member_index})
            agent = self._build_agent(ctx)
            async with message.channel.typing():
                try:
                    await agent.run(messages_for_llm)
                except Exception as e:
                    logger.exception("Совет (член %s): ошибка агента по делу №%s: %s", self._member_index, case.id, e)
            await self._count_votes_and_finish(guild.id, case.id)
        finally:
            self._processing_cases.discard(lock_key)

    async def _process_case_by_id(self, guild_id: int, case_id: int) -> None:
        """Запустить голосование этого члена по уже существующему делу (например по напоминанию «не хватает голоса»)."""
        async with get_db() as session:
            result = await session.execute(
                select(CouncilCase).where(
                    CouncilCase.id == case_id,
                    CouncilCase.guild_id == guild_id,
                    CouncilCase.status == "open",
                )
            )
            case = result.scalars().one_or_none()
        if not case or await self._has_voted(case_id, self._member_index):
            return
        lock_key = (case_id, self._member_index)
        if lock_key in self._processing_cases:
            return
        self._processing_cases.add(lock_key)
        try:
            guild = self.get_guild(guild_id)
            if not guild:
                return
            law_block = await get_law_block_async(
                self, guild_id, max_chars=10000,
                reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
                config=self.config,
            )
            source = case.source or "elder"
            content = (case.content or "")[:2000]
            user_content = (
                f"[ ДЕЛО СОВЕТА №{case.id} ]\n\n"
                f"Источник: {source}. Содержание:\n{content}\n\n"
                "По закону (блок выше) и по своему характеру вынеси позицию и голос. "
                "Голос только **За** (yes) или **Против** (no). Воздержаний нет. "
                f"Вызови post_my_deliberation(case_id=\"{case.id}\", thoughts=\"...\", vote=\"yes\" или \"no\")."
            )
            messages_for_llm = [
                {"role": "user", "content": law_block + "\n\n---\n\n" + user_content},
            ]
            ctx = self._agent_context(guild_id, extra={"current_case_id": case.id, "member_index": self._member_index})
            agent = self._build_agent(ctx)
            ch_id = self.config.channel_for_role(self.role_key, "deliberations") or self._inbox_channel_id
            channel = self.get_channel(ch_id) if ch_id else None
            if channel:
                async with channel.typing():
                    try:
                        await agent.run(messages_for_llm)
                    except Exception as e:
                        logger.exception("Совет (член %s): ошибка агента по делу №%s: %s", self._member_index, case.id, e)
            else:
                try:
                    await agent.run(messages_for_llm)
                except Exception as e:
                    logger.exception("Совет (член %s): ошибка агента по делу №%s: %s", self._member_index, case.id, e)
            await self._count_votes_and_finish(guild_id, case.id)
        finally:
            self._processing_cases.discard(lock_key)

    async def _get_or_create_case(self, guild_id: int, channel_id: int, message_id: int, content: str, source: str) -> CouncilCase | None:
        async with _case_creation_lock:
            async with get_db() as session:
                result = await session.execute(
                    select(CouncilCase).where(
                        CouncilCase.guild_id == guild_id,
                        CouncilCase.source_channel_id == channel_id,
                        CouncilCase.source_message_id == message_id,
                    )
                )
                case = result.scalars().one_or_none()
                if case:
                    return case
                case = CouncilCase(
                    guild_id=guild_id,
                    source=source,
                    source_channel_id=channel_id,
                    source_message_id=message_id,
                    content=(content or "")[:4000],
                    status="open",
                )
                session.add(case)
                try:
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(
                        select(CouncilCase).where(
                            CouncilCase.guild_id == guild_id,
                            CouncilCase.source_channel_id == channel_id,
                            CouncilCase.source_message_id == message_id,
                        )
                    )
                    return result.scalars().one_or_none()
                await session.refresh(case)
                return case

    async def _has_voted(self, case_id: int, member_index: int) -> bool:
        async with get_db() as session:
            result = await session.execute(
                select(CouncilVote).where(
                    CouncilVote.case_id == case_id,
                    CouncilVote.member_index == member_index,
                )
            )
            return result.scalars().first() is not None

    async def _count_votes_and_finish(self, guild_id: int, case_id: int) -> None:
        async with get_db() as session:
            result = await session.execute(
                select(CouncilCase).where(CouncilCase.id == case_id, CouncilCase.guild_id == guild_id)
            )
            case = result.scalars().one_or_none()
            result_v = await session.execute(
                select(CouncilVote).where(CouncilVote.case_id == case_id)
            )
            votes = result_v.scalars().all()
        if not case:
            return
        if case.status != "open":
            return
        voted_indices = {v.member_index for v in votes}
        if len(votes) == 2:
            if case.nudge_2votes_sent_at is not None:
                return
            missing = next((i for i in (1, 2, 3) if i not in voted_indices), None)
            if missing is not None and self._inbox_channel_id:
                async with get_db() as session:
                    upd = await session.execute(
                        update(CouncilCase)
                        .where(
                            CouncilCase.id == case_id,
                            CouncilCase.guild_id == guild_id,
                            CouncilCase.nudge_2votes_sent_at.is_(None),
                        )
                        .values(nudge_2votes_sent_at=datetime.now(timezone.utc))
                    )
                    if upd.rowcount == 0:
                        return
                ch = self.get_channel(self._inbox_channel_id)
                if ch:
                    try:
                        nudge_text = (
                            f"**По делу №{case_id}:** не хватает голоса члена {missing}. "
                            f"Член совета {missing}, выскажись (За или Против) в канале обсуждений."
                        )
                        await ch.send(nudge_text)
                        logger.info("Совет → [#%s]: %s", getattr(ch, "name", "?"), nudge_text[:150])
                    except Exception as e:
                        logger.warning("Совет: не удалось отправить напоминание по делу №%s: %s", case_id, e)
            return
        if len(votes) < 3:
            return
        yes_count = sum(1 for v in votes if v.vote == "yes")
        no_count = sum(1 for v in votes if v.vote == "no")
        approved = yes_count > no_count
        now = datetime.now(timezone.utc)
        new_status = "approved" if approved else "rejected"
        async with get_db() as session:
            upd = await session.execute(
                update(CouncilCase)
                .where(
                    CouncilCase.id == case_id,
                    CouncilCase.guild_id == guild_id,
                    CouncilCase.status == "open",
                )
                .values(status=new_status, result_at=now)
            )
            if upd.rowcount == 0:
                logger.debug("Совет: дело №%s уже обработано другим обработчиком, пропуск", case_id)
                return
        ch_id = self.config.channel_for_role(self.role_key, "deliberations")
        if not ch_id:
            logger.info("Совет: дело №%s завершено, approved=%s (канал обсуждений не настроен)", case_id, approved)
            return
        ch = self.get_channel(ch_id)
        if not ch:
            logger.info("Совет: дело №%s завершено, approved=%s", case_id, approved)
            return
        if approved:
            summary = (case.content or "Решение совета")[:400]
            try:
                announce = (
                    f"═══════════════════════════════\n"
                    f"**Член совета {self._member_index} оглашает:** Большинством совета принято: {summary}. "
                    "Решение приводится в исполнение."
                )
                await ch.send(announce)
                logger.info("Совет → [#%s]: %s", getattr(ch, "name", "?"), announce[:200].replace("\n", " "))
            except Exception as e:
                logger.exception("Не удалось отправить оглашение: %s", e)
            logger.info("Совет: начало исполнения дела №%s (захват исполнения)", case_id)
            max_exec_attempts = 3
            execution_ok = False
            case_content = case.content or ""
            for exec_attempt in range(1, max_exec_attempts + 1):
                try:
                    await self._run_execution_for_case(guild_id, case_id, case_content)
                except Exception as e:
                    logger.exception("Совет: ошибка исполнения по делу №%s (попытка %d/%d): %s",
                                     case_id, exec_attempt, max_exec_attempts, e)
                    try:
                        await ch.send(f"**По делу №{case_id}:** при исполнении произошла ошибка: {str(e)[:300]}")
                    except Exception:
                        pass
                    break

                if await self._verify_execution(guild_id, case_content):
                    logger.info("Совет: дело №%s — верификация пройдена (попытка %d)", case_id, exec_attempt)
                    execution_ok = True
                    break

                if exec_attempt < max_exec_attempts:
                    logger.warning("Совет: дело №%s — верификация НЕ пройдена, повтор (%d/%d)",
                                   case_id, exec_attempt, max_exec_attempts)
                    await asyncio.sleep(3)
                else:
                    logger.error("Совет: дело №%s — исполнение не верифицировано после %d попыток",
                                 case_id, max_exec_attempts)

            if execution_ok:
                async with get_db() as session:
                    await session.execute(
                        update(CouncilCase)
                        .where(CouncilCase.id == case_id, CouncilCase.guild_id == guild_id)
                        .values(status="executed", execution_at=datetime.now(timezone.utc))
                    )
                logger.info("Совет: дело №%s помечено как выполнено (status=executed)", case_id)
            else:
                logger.error("Совет: дело №%s — исполнение НЕ удалось, статус остаётся 'approved'", case_id)
        else:
            try:
                reject_msg = (
                    f"═══════════════════════════════\n"
                    f"**Член совета {self._member_index} оглашает:** По делу №{case_id} голоса: За — {yes_count}, Против — {no_count}. "
                    "Совет постановил: **не исполнять**."
                )
                await ch.send(reject_msg)
                logger.info("Совет → [#%s]: %s", getattr(ch, "name", "?"), reject_msg[:200].replace("\n", " "))
            except Exception as e:
                logger.exception("Не удалось отправить итог: %s", e)
        logger.info("Совет: дело №%s завершено, approved=%s", case_id, approved)

    @staticmethod
    def _extract_target_participant(content: str) -> str | None:
        """Извлечь имя/ник целевого участника из текста решения (regex)."""
        _pats = [
            re.compile(r'у\s+участника[^(]*\(([^)]+)\)', re.IGNORECASE),
            re.compile(r'участнику[^(]*\(([^)]+)\)', re.IGNORECASE),
            re.compile(r'у\s+участника\s+([^\s,.:;()]+)', re.IGNORECASE),
            re.compile(r'участнику\s+([^\s,.:;()]+)', re.IGNORECASE),
        ]
        for pat in _pats:
            m = pat.search(content)
            if m:
                name = m.group(1).strip()
                if name and len(name) > 1:
                    return name
        return None

    async def _verify_execution(self, guild_id: int, content: str) -> bool:
        """Проверить, что ролевые изменения реально применились к целевому участнику."""
        target_name = self._extract_target_participant(content)
        if not target_name:
            return True

        guild_obj = self.get_guild(guild_id)
        if not guild_obj:
            return True

        t_lower = target_name.lower()
        target_member = None
        for mbr in guild_obj.members:
            dn = (getattr(mbr, 'display_name', '') or '').lower()
            nm = (getattr(mbr, 'name', '') or '').lower()
            if t_lower == dn or t_lower == nm:
                target_member = mbr
                break
        if not target_member:
            for mbr in guild_obj.members:
                dn = (getattr(mbr, 'display_name', '') or '').lower()
                nm = (getattr(mbr, 'name', '') or '').lower()
                if t_lower in dn or t_lower in nm:
                    target_member = mbr
                    break
        if not target_member:
            return True

        try:
            target_member = await guild_obj.fetch_member(target_member.id)
        except Exception:
            pass

        current_roles = [r for r in target_member.roles if not r.is_default()]
        current_names = {r.name.lower() for r in current_roles}
        content_lower = content.lower()

        wants_remove_all = any(kw in content_lower for kw in (
            'забрать все роли', 'забрать всех ролей',
            'забирании всех ролей', 'забирание всех ролей',
            'лишить всех ролей', 'снять все роли',
        ))

        role_to_add: str | None = None
        m = re.search(
            r'(?:присвоить|выдать|назначить|присвоении)\s+(?:ему\s+)?роль\s+([^\s,.:;]+)',
            content, re.IGNORECASE,
        )
        if m:
            role_to_add = m.group(1).strip()

        if wants_remove_all and role_to_add:
            ok = len(current_roles) == 1 and role_to_add.lower() in current_names
            logger.info("Верификация: забрать все + выдать %s → текущие роли: %s → %s",
                        role_to_add, [r.name for r in current_roles], "OK" if ok else "FAIL")
            return ok
        if wants_remove_all:
            ok = len(current_roles) == 0
            logger.info("Верификация: забрать все → текущие роли: %s → %s",
                        [r.name for r in current_roles], "OK" if ok else "FAIL")
            return ok
        if role_to_add:
            ok = role_to_add.lower() in current_names
            logger.info("Верификация: выдать %s → текущие роли: %s → %s",
                        role_to_add, [r.name for r in current_roles], "OK" if ok else "FAIL")
            return ok

        return True

    async def _run_execution_for_case(self, guild_id: int, case_id: int, content: str) -> None:
        """Запуск агента-исполнителя: по тексту решения вызвать нужные инструменты."""
        target_name = self._extract_target_participant(content)
        target_member_id: int | None = None
        target_display_name: str | None = None
        target_roles_list: list[tuple[int, str]] = []
        if target_name:
            guild_obj = self.get_guild(guild_id)
            if guild_obj:
                t_lower = target_name.lower()
                for mbr in guild_obj.members:
                    dn = (getattr(mbr, 'display_name', '') or '').lower()
                    nm = (getattr(mbr, 'name', '') or '').lower()
                    if t_lower == dn or t_lower == nm:
                        target_member_id = mbr.id
                        target_display_name = mbr.display_name
                        target_roles_list = [(r.id, r.name) for r in mbr.roles if not r.is_default()]
                        break
                if not target_member_id:
                    for mbr in guild_obj.members:
                        dn = (getattr(mbr, 'display_name', '') or '').lower()
                        nm = (getattr(mbr, 'name', '') or '').lower()
                        if t_lower in dn or t_lower in nm:
                            target_member_id = mbr.id
                            target_display_name = mbr.display_name
                            target_roles_list = [(r.id, r.name) for r in mbr.roles if not r.is_default()]
                            break
            if target_member_id:
                logger.info("Совет: дело №%s — целевой участник: %s (id=%s, %d ролей)",
                           case_id, target_display_name, target_member_id, len(target_roles_list))
        user_content = (
            f"ИСПОЛНИ РЕШЕНИЕ СОВЕТА — ДЕЛО №{case_id}.\n"
            "Совет проголосовал ЗА. Решение ПРИНЯТО. Исполни его ПРЯМО СЕЙЧАС.\n\n"
            f"ТЕКСТ РЕШЕНИЯ:\n\"\"\"\n{content[:3000]}\n\"\"\"\n\n"
            "═══ ОПРЕДЕЛИ ТИП РЕШЕНИЯ ═══\n"
            "Внимательно прочитай текст. Определи тип:\n\n"
            "ТИП A — ЗАКОНОПРОЕКТ / ЗАКОН / ПОЛОЖЕНИЯ (слова: «законопроект», «положения», «статья», «пункты 1) 2) 3)»):\n"
            "  → ГЛАВНОЕ ДЕЙСТВИЕ: publish_new_law_article(title=\"Название закона\", text=ТОЛЬКО_ПОЛОЖЕНИЯ)\n"
            "  → В text передавай СТРОГО ТОЛЬКО пронумерованные положения/пункты закона (1, 2, 3…).\n"
            "    БЕЗ «цели», БЕЗ вступительного текста, БЕЗ «Дело №N», БЕЗ «Тип процедуры»,\n"
            "    БЕЗ «Решение старейшин», БЕЗ «Запрос полностью», БЕЗ «как описал обратившийся».\n"
            "    Только: 1) ... 2) ... 3) ... — чистые положения.\n"
            "  → Если в тексте сказано «создание роли X» — создай роль (create_role), если её ещё нет.\n"
            "  → НЕ вызывай add_role_to_member — закон создаёт ПРАВИЛА, а не выдаёт роль конкретному человеку.\n"
            "  → НЕ вызывай remove_role_from_member — закон не снимает роли.\n\n"
            "ТИП B — ВЫДАТЬ/СНЯТЬ РОЛЬ КОНКРЕТНОМУ УЧАСТНИКУ (слова: «выдать роль X участнику Y», «забрать роли у участника Y»):\n"
            "  → КРИТИЧЕСКИ ВАЖНО: определи ЦЕЛЕВОГО участника — это тот, О КОМ говорится в решении\n"
            "    (кому забрать/выдать роли), а НЕ тот, кто подал запрос (обратившийся/гражданин).\n"
            "    Пример: «от гражданина поляк: забрать роли у участника 1mpol» → цель = 1mpol, НЕ поляк.\n"
            "    «От гражданина поляк» = кто подал заявку (ИГНОРИРУЙ для действия).\n"
            "    «у участника 1mpol» = с КЕМ делать действие (ЦЕЛЕВОЙ).\n"
            "  → Найди ЦЕЛЕВОГО участника в результатах get_roles_and_members() по нику/имени/ID.\n"
            "  → «Забрать ВСЕ роли» → remove_role_from_member для КАЖДОЙ роли ЦЕЛЕВОГО участника (кроме @everyone).\n"
            "    Если у него 10 ролей — 10 вызовов remove_role_from_member. НЕ один, а ДЛЯ КАЖДОЙ.\n"
            "  → «Выдать роль» → add_role_to_member ЦЕЛЕВОМУ участнику. НЕ снимай другие роли если не сказано.\n\n"
            "ТИП C — ДРУГОЕ (создать канал, изменить правила и т.п.):\n"
            "  → Действуй по тексту.\n\n"
            "═══ ПРАВИЛА ═══\n"
            "- Делай СТРОГО и ТОЛЬКО то, что написано. НИЧЕГО лишнего.\n"
            "- НИКОГДА не путай «обратившегося» (кто подал запрос) с «целевым участником» (о ком запрос).\n"
            "- НЕ выдавай роли участникам если НЕ указан конкретный участник.\n"
            "- НЕ снимай роли если не сказано «забрать/снять роли».\n"
            "- «Забрать ВСЕ роли» = вызови remove_role_from_member для КАЖДОЙ роли (не один раз, а N раз).\n\n"
            "ПЕРВЫЙ ШАГ: get_roles_and_members() — узнать существующие роли и участников.\n"
            "Найди ЦЕЛЕВОГО участника и запомни его member_id и список ролей.\n\n"
            "ПОСЛЕ ДЕЙСТВИЙ:\n"
            "  • Роль УЖЕ существует → используй ID. НЕ создавай повторно.\n"
            "  • Роли НЕТ → create_role(name=...).\n\n"
            "ФИНАЛЬНЫЙ ШАГ:\n"
            f"  • post_council_outcome_to_deliberations(case_id=\"{case_id}\", outcome_text=\"краткий итог\")\n"
            f"  • post_to_execution_blog(case_id=\"{case_id}\", summary=\"краткий отчёт\")\n\n"
            "После post_to_execution_blog — задача ЗАВЕРШЕНА.\n\n"
            "НАЧИНАЙ: сначала get_roles_and_members(), затем действия по типу."
        )
        if target_member_id:
            roles_info = "\n".join(
                f"  - {rname} (role_id={rid})" for rid, rname in target_roles_list
            ) or "  (нет ролей)"
            user_content += (
                f"\n══════ ЦЕЛЕВОЙ УЧАСТНИК (определён системой — ОБЯЗАТЕЛЕН) ══════\n"
                f"Имя: {target_display_name}\n"
                f"member_id = {target_member_id}\n"
                f"Текущие роли:\n{roles_info}\n"
                f"Для «забрать ВСЕ роли» — вызови remove_role_from_member(member_id={target_member_id}, role_id=...) для КАЖДОЙ роли из списка выше.\n"
                f"Для «выдать роль» — вызови add_role_to_member(member_id={target_member_id}, role_id=...).\n"
                f"ЗАБЛОКИРОВАНО для любого другого member_id.\n"
                f"══════════════════════════════════════════════════════\n"
            )
        messages_for_llm = [
            {"role": "user", "content": user_content},
        ]
        extra = {"member_index": self._member_index, "execution_case_id": case_id}
        if target_member_id:
            extra["target_member_id"] = target_member_id
            extra["target_member_name"] = target_display_name or target_name
        ctx = self._agent_context(guild_id, extra=extra)
        agent = self._build_agent(ctx)
        result_text, tools_used = await agent.run(messages_for_llm)
        logger.info(
            "Совет: исполнение дела №%s завершено, вызваны инструменты: %s",
            case_id, tools_used,
        )

    async def on_message(self, message: Message) -> None:
        guild = message.guild
        if not guild:
            await self.process_commands(message)
            return
        channel_id = message.channel.id
        ch_name = getattr(message.channel, "name", channel_id)
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "?")
        is_bot = getattr(message.author, "bot", False)
        preview = (message.content or "")[:200].replace("\n", " ")
        logger.info(
            "Совет (член %s) ← [#%s] %s%s: %s",
            self._member_index, ch_name, author_name, " (бот)" if is_bot else "", preview,
        )
        if message.author.bot and channel_id != self._inbox_channel_id:
            await self.process_commands(message)
            return
        if channel_id not in (self._watch_channel_ids or []):
            await self.process_commands(message)
            return
        content = (message.content or "").strip()
        if not content:
            await self.process_commands(message)
            return

        # Сообщение в council_inbox: обращение старейшины или напоминание «не хватает голоса члена N»
        if channel_id == self._inbox_channel_id:
            content_lower = content.lower()
            nudge_case_id = nudge_member = None
            m1 = re.search(r"по делу №(\d+).*не хватает голоса члена (\d+)", content_lower, re.DOTALL)
            if m1:
                nudge_case_id, nudge_member = int(m1.group(1)), int(m1.group(2))
            else:
                m2 = re.search(r"не хватает голоса члена (\d+).*делу №(\d+)", content_lower, re.DOTALL)
                if m2:
                    nudge_member, nudge_case_id = int(m2.group(1)), int(m2.group(2))
            if nudge_case_id is not None and nudge_member is not None:
                if nudge_member == self._member_index:
                    if not await self._has_voted(nudge_case_id, self._member_index):
                        logger.info("Совет (член %s): напоминание по делу №%s — высказываюсь", self._member_index, nudge_case_id)
                        await self._process_case_by_id(guild.id, nudge_case_id)
                await self.process_commands(message)
                return
            # Сообщения от самого бота совета (nudge и пр.) — игнорируем
            if message.author.id == self.user.id:
                await self.process_commands(message)
                return
            logger.info("Совет: обращение в council_inbox (сообщение %s, автор бот=%s) — запускаем процедуру", message.id, getattr(message.author, "bot", False))
            await self._process_message_as_council_case(message)
            await self.process_commands(message)
            return

        verdict = await self._message_elder_verdict(message)
        if verdict is False:
            logger.info("Совет: сообщение %s помечено старейшиной как нелигитимное (крестик), не рассматриваем", message.id)
            await self.process_commands(message)
            return
        if verdict is None:
            logger.debug("Совет: на сообщении %s нет галочки от старейшины, ждём маркировки", message.id)
            await self.process_commands(message)
            return

        await self._process_message_as_council_case(message)
        await self.process_commands(message)

    async def on_raw_reaction_add(self, payload: Any) -> None:
        """Когда старейшина ставит галочку на сообщение в council_inbox/court_decisions — запускаем рассмотрение."""
        if not payload.guild_id or payload.channel_id not in (self._watch_channel_ids or []):
            return
        if getattr(payload, "user_id", None) and payload.user_id == self.user.id:
            return
        elder_role_id = (self.config.role_ids() or {}).get("elder") or 0
        if not elder_role_id:
            return
        emoji_str = str(getattr(payload.emoji, "name", payload.emoji) or "")
        if not any(e in emoji_str or emoji_str == e for e in ELDER_APPROVE_EMOJIS):
            if emoji_str in ELDER_REJECT_EMOJIS:
                logger.info("Совет: старейшина поставил крестик на сообщение %s — не рассматриваем", payload.message_id)
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        if not member or getattr(member, "bot", False):
            return
        if not any(r.id == elder_role_id for r in member.roles):
            return
        channel = self.get_channel(payload.channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception as e:
            logger.warning("Совет: не удалось загрузить сообщение %s: %s", payload.message_id, e)
            return
        if not message.content or not message.content.strip():
            return
        verdict = await self._message_elder_verdict(message)
        if verdict is not True:
            return
        logger.info("Совет: галочка старейшины на сообщение %s — запускаем рассмотрение", message.id)
        await self._process_message_as_council_case(message)


def create_council_bot(deps: RoleDeps, role_key: str = "council_1") -> RoleBot:
    """Фабрика: создаёт CouncilBot с заданным role_key (council_1, council_2, council_3)."""
    return CouncilBot(deps=deps, role_key=role_key)
