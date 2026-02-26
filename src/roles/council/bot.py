"""
Бот члена совета: мониторит council_inbox и court_decisions, по новому сообщению выносит позицию и голос (За/Против), публикует в council_deliberations.
После трёх голосов случайный член оглашает решение; при одобрении — исполнение через агента.
"""
from __future__ import annotations

import asyncio
import logging
import re
import random
from datetime import datetime, timezone
from typing import Any

from discord import Message  # type: ignore[reportMissingImports]
from discord.ext import commands  # type: ignore[reportMissingImports]
from sqlalchemy import select, update  # type: ignore[reportMissingImports]

from src.core.agent import Agent
from src.core.agent_ctx import AgentContext
from src.core.db import get_db
from src.core.models import CouncilCase, CouncilVote
from src.core.discord_guild import get_law_block_async
from src.roles.base import RoleBot, RoleDeps
from src.roles.council.tools import make_council_tools

logger = logging.getLogger("basuni.council.bot")

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
        self._nudged_case_ids: set[int] = set()

    def _agent_context(self, guild_id: int, extra: dict[str, Any] | None = None) -> AgentContext:
        cfg = self.config
        channel_ids = {}
        for purpose in ("inbox", "deliberations", "court_decisions"):
            ch_id = cfg.channel_for_role(self.role_key, purpose)
            if ch_id:
                channel_ids[purpose] = ch_id
        extra = extra or {}
        extra["member_index"] = self._member_index
        return AgentContext(
            guild_id=guild_id,
            channel_ids=channel_ids,
            bot=self,
            db_session_factory=self.deps.db_session_factory,
            extra=extra,
        )

    def _build_agent(self, ctx: AgentContext) -> Agent:
        system_prompt = self.load_system_prompt()
        tools = make_council_tools(ctx, ctx.extra.get("member_index", 1))
        base_url = getattr(self.config, "openai_base_url", None)
        return Agent(
            system_prompt=system_prompt,
            tools=tools,
            api_key=self.deps.openai_api_key,
            model=self.config.openai_model,
            max_tool_rounds=8,
            base_url=base_url,
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

    async def _process_message_as_council_case(self, message: Message) -> None:
        """Общая логика: создать/взять дело, если этот член ещё не голосовал — запустить агента, подсчитать голоса."""
        guild = message.guild
        if not guild:
            return
        channel_id = message.channel.id
        content = (message.content or "").strip()
        if not content:
            return
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

    async def _get_or_create_case(self, guild_id: int, channel_id: int, message_id: int, content: str, source: str) -> CouncilCase | None:
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
            await session.flush()
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
        if len(votes) == 2 and case_id not in self._nudged_case_ids:
            missing = next((i for i in (1, 2, 3) if i not in voted_indices), None)
            if missing is not None and self._inbox_channel_id:
                self._nudged_case_ids.add(case_id)
                ch = self.get_channel(self._inbox_channel_id)
                if ch:
                    try:
                        await ch.send(
                            f"**По делу №{case_id}:** не хватает голоса члена {missing}. "
                            f"Член совета {missing}, выскажись (За или Против) в канале обсуждений."
                        )
                        logger.info("Совет: напоминание по делу №%s — член %s не проголосовал", case_id, missing)
                    except Exception as e:
                        logger.warning("Совет: не удалось отправить напоминание по делу №%s: %s", case_id, e)
            return
        if len(votes) < 3:
            return
        yes_count = sum(1 for v in votes if v.vote == "yes")
        no_count = sum(1 for v in votes if v.vote == "no")
        approved = yes_count > no_count
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            await session.execute(
                update(CouncilCase)
                .where(CouncilCase.id == case_id, CouncilCase.guild_id == guild_id)
                .values(
                    status="approved" if approved else "rejected",
                    result_at=now,
                )
            )
        ch_id = self.config.channel_for_role(self.role_key, "deliberations")
        if not ch_id:
            logger.info("Совет: дело №%s завершено, approved=%s (канал обсуждений не настроен)", case_id, approved)
            return
        ch = self.get_channel(ch_id)
        if not ch:
            logger.info("Совет: дело №%s завершено, approved=%s", case_id, approved)
            return
        announcer = random.randint(1, 3)
        if approved:
            summary = (case.content or "Решение совета")[:400]
            try:
                await ch.send(
                    f"**Член совета {announcer} оглашает:** Большинством совета принято: {summary}. "
                    "Решение приводится в исполнение."
                )
            except Exception as e:
                logger.exception("Не удалось отправить оглашение: %s", e)
            try:
                await self._run_execution_for_case(guild_id, case_id, case.content or "")
            except Exception as e:
                logger.exception("Совет: ошибка исполнения по делу №%s: %s", case_id, e)
                try:
                    await ch.send(f"**По делу №{case_id}:** при исполнении произошла ошибка: {str(e)[:300]}")
                except Exception:
                    pass
            async with get_db() as session:
                await session.execute(
                    update(CouncilCase)
                    .where(CouncilCase.id == case_id, CouncilCase.guild_id == guild_id)
                    .values(status="executed", execution_at=datetime.now(timezone.utc))
                )
        else:
            try:
                await ch.send(
                    f"**Член совета {announcer} оглашает:** По делу №{case_id} голоса: За — {yes_count}, Против — {no_count}. "
                    "Совет постановил: **не исполнять**."
                )
            except Exception as e:
                logger.exception("Не удалось отправить итог: %s", e)
        logger.info("Совет: дело №%s завершено, approved=%s", case_id, approved)

    async def _run_execution_for_case(self, guild_id: int, case_id: int, content: str) -> None:
        """Запуск агента-исполнителя: по тексту решения вызвать нужные инструменты (бан, кик, роли и т.д.)."""
        law_block = await get_law_block_async(
            self, guild_id, max_chars=8000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        user_content = (
            f"[ ИСПОЛНЕНИЕ РЕШЕНИЯ СОВЕТА — ДЕЛО №{case_id} ]\n\n"
            "Сейчас ты не голосуешь и не высказываешь позицию — только **исполняешь** уже принятое большинством решение.\n\n"
            f"Текст решения/поручения:\n{content[:3000]}\n\n"
            "По закону (блок выше) и по смыслу текста вызови нужные инструменты и выполни действия: "
            "ban_member, kick_member, add_role_to_member, remove_role_from_member, timeout_member, "
            "create_role, create_text_channel, send_message_to_channel, delete_message и т.д. "
            "Определи по тексту, кого наказать/кого наградить/что создать/удалить. "
            "Используй get_roles_and_members и get_member_info при необходимости для ID участников и ролей."
        )
        messages_for_llm = [
            {"role": "user", "content": law_block + "\n\n---\n\n" + user_content},
        ]
        ctx = self._agent_context(guild_id, extra={"member_index": 1, "execution_case_id": case_id})
        agent = self._build_agent(ctx)
        await agent.run(messages_for_llm)

    async def on_message(self, message: Message) -> None:
        guild = message.guild
        if not guild:
            await self.process_commands(message)
            return
        channel_id = message.channel.id
        # Сообщения от ботов игнорируем, кроме council_inbox: туда старейшина пишет через notify_council — по нему совет начинает процедуру.
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
            if nudge_case_id is not None and nudge_member is not None and nudge_member == self._member_index:
                    if not await self._has_voted(nudge_case_id, self._member_index):
                        logger.info("Совет (член %s): напоминание по делу №%s — высказываюсь", self._member_index, nudge_case_id)
                        await self._process_case_by_id(guild.id, nudge_case_id)
                    await self.process_commands(message)
                    return
            # Сообщение в council_inbox обрабатываем и от бота (старейшина пишет через notify_council от имени бота)
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
