"""
Бот «Старейшина»: агент с памятью переписок. Читает все сообщения в канале и сам решает, кому и когда отвечать.
Ответ приходит как reply на сообщение пользователя. Наблюдает за сроками: при истечении срока суда действует по закону.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# Фразы, которые бот отправляет сразу, пока ждёт ответ модели (429, долгий запрос)
DEFAULT_THINKING_PHRASES = [
    "Дай подумать…",
    "Сейчас посмотрю справочники и вернусь с ответом.",
    "Не торопи — подумаю и отвечу.",
    "Ищу в делах и законе…",
    "Смотрю по делу и прецедентам.",
    "Минуту, разбираю обращение.",
]
# Фразы, когда старейшина решил не отвечать (обращение не к нему) — отправить одну из них вместо молчания
DEFAULT_SKIP_REPLY_PHRASES = [
    "Обращение не ко мне.",
    "Это не ко мне.",
    "Не ко мне — не отвечу.",
    "Ко мне не обращались.",
]

from discord import Message  # type: ignore[reportMissingImports]
from discord.ext import commands  # type: ignore[reportMissingImports]
from sqlalchemy import and_, delete, func, select, update  # type: ignore[reportMissingImports]

from src.core.agent import Agent
from src.core.agent_ctx import AgentContext
from src.core.db import get_db
from src.core.models import ElderCase, ElderCourtLog, ElderCaseCourtVote
from src.core.discord_guild import (
    get_guild_channels_json,
    get_guild_roles_and_members_json,
    get_author_roles_block_async,
    get_law_block_async,
)
from src.core.conversation_memory import (
    save_message,
    load_recent_messages,
    load_branch_summary,
    load_all_branch_summaries,
    save_branch_summary,
)
from src.roles.base import RoleBot, RoleDeps
from src.roles.elder.tools import make_elder_tools, _mentions_for_role, _deadline_from_case

logger = logging.getLogger("basuni.elder.bot")

# Если агент вернёт ровно это — ответ в Discord не отправляем (обращение не к старейшине; заготовленные фразы не используем)
SKIP_REPLY_MARKER = "НЕТ"
# Если агент вернёт это — оскорбление; отправим одну фразу из skip_reply_phrases
INSULT_MARKER = "ОСКОРБЛЕНИЕ"
# В режиме надзора: если агент вернёт это — действие легитимно, в канал ничего не постим
LEGITIMATE_MARKER = "ЛЕГИТИМНО"
# Реакция в канале надзора (текстовый ответ в канал не постим — только эмодзи)
REACT_PREFIX = "REACT:"
# Нелегитимно: текст постим в канал решений старейшин, на сообщение — реакция 👎
INTERRUPT_PREFIX = "INTERRUPT:"


def _has_pmj_role(message: Message, pmj_role_id: int | None) -> bool:
    if not pmj_role_id or not message.guild:
        return True
    member = message.guild.get_member(message.author.id)
    if not member:
        return False
    return any(r.id == pmj_role_id for r in member.roles)


def _build_memory_block(
    channel_id: int,
    thread_id: int | None,
    author_id: int,
    author_name: str,
    branch_summary: str | None,
    current_case_id: int | None,
    other_branches: list[dict],
    channel_names: dict[int, str],
    current_case_display_number: int | None = None,
    pending_procedure_submission: bool = False,
) -> str:
    """Блок «Память и ветки разговоров» для контекста агента."""
    ch_name = channel_names.get(channel_id) or str(channel_id)
    current_line = (
        f"Текущая ветка: канал «{ch_name}» (id={channel_id})"
        + (f", тред {thread_id}" if thread_id else "")
        + f", с тобой пишет **{author_name}** (id={author_id}). "
    )
    if branch_summary:
        current_line += f"Сохранённый контекст ветки: {branch_summary}. "
    if current_case_id is not None:
        display_no = current_case_display_number if current_case_display_number is not None else current_case_id
        current_line += f"Текущее обращение по делу №{display_no}. В инструменты передавай id дела (case_id) из list_elder_cases/get_case, в ответах пользователю — «дело №{display_no}»."
    elif pending_procedure_submission:
        current_line += (
            "Подана заявка на процедуру (законопроект/референдум/инициатива). Сначала реши: одобряешь или отклоняешь. "
            "Если одобряешь — вызови create_elder_case(суть_обращения), затем publish_decision(case_id, referendum_approved, reasoning), notify_court(текст), record_case_sent_to_court(case_id, content_sent). "
            "Если отклоняешь — ответь «Отклонено» и укажи причину по закону; дело в базу не заносится."
        )
    else:
        current_line += "Обращения по процедуре (дело) нет — это вопрос или информационный запрос. Отвечай по существу, не создавай дело и не предлагай передать в суд."
    lines = ["[ ПАМЯТЬ И ВЕТКИ РАЗГОВОРОВ ]", current_line]
    if other_branches:
        others = []
        for b in other_branches:
            if b["channel_id"] == channel_id and b.get("author_id") == author_id and b.get("thread_id") == thread_id:
                continue
            loc = b.get("channel_name", str(b["channel_id"]))
            if b.get("thread_id"):
                loc += f" (тред {b['thread_id']})"
            author_id_oth = b.get("author_id")
            others.append(f"  — {loc}, автор id={author_id_oth}: {b.get('summary', '')[:120]}")
        if others:
            lines.append("Другие активные ветки (общий контекст по каналам):")
            lines.extend(others[:10])
    lines.append("Не забывай контекст: отвечай с учётом того, о чём уже говорили в этой ветке и что происходит в других каналах.")
    return "\n".join(lines)


def _build_judge_vote_summary(guild: Any, vote_info: dict[str, Any]) -> str:
    """Формирует текст отчёта: какие судьи проголосовали и как (за/против)."""
    votes = vote_info.get("votes") or {}
    if not votes:
        return f"Проголосовало судей: {vote_info.get('count', 0)}."
    parts = []
    for jid, v in votes.items():
        name = "?"
        if guild:
            m = guild.get_member(jid)
            if m:
                name = getattr(m, "display_name", None) or getattr(m, "name", "") or str(jid)
        vote_text = "за" if v == "yes" else ("против" if v == "no" else "—")
        parts.append(f"Судья {name}: {vote_text}")
    return "; ".join(parts) if parts else f"Судей: {vote_info.get('count', 0)}"


def _message_refers_to_case(content: str, case_id: int) -> bool:
    """True, если в тексте явно упомянуто дело с номером case_id (дело №1, по делу 1, дело 1 и т.п.)."""
    if not content:
        return False
    text = content.strip().lower()
    # дело №1, дело 1, по делу 1, по делу №1, дело #1, case 1
    patterns = [
        rf"\bдело\s*[№#]?\s*{case_id}\b",
        rf"\bпо\s+делу\s*[№#]?\s*{case_id}\b",
        rf"\bcase\s+{case_id}\b",
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _message_refers_to_other_case(content: str, case_id: int) -> bool:
    """True, если в тексте упомянут номер другого дела (не case_id)."""
    if not content:
        return False
    text = content.strip().lower()
    # Ищем упоминания "дело №N" / "по делу N" для любого числа N != case_id
    for m in re.finditer(r"(?:дело|по\s+делу)\s*[№#]?\s*(\d+)", text, re.IGNORECASE):
        try:
            n = int(m.group(1))
            if n != case_id:
                return True
        except (ValueError, IndexError):
            pass
    for m in re.finditer(r"case\s+(\d+)", text, re.IGNORECASE):
        try:
            n = int(m.group(1))
            if n != case_id:
                return True
        except (ValueError, IndexError):
            pass
    return False


async def _count_judge_votes_in_channel(
    bot: Any,
    channel_id: int,
    guild_id: int,
    judge_role_id: int,
    limit: int = 20,
    case_id: int | None = None,
    sent_to_court_at: datetime | None = None,
) -> dict[str, Any]:
    """По последним сообщениям канала считает голоса судей. Учитываются только авторы с ролью судьи; от одного автора — один голос (первое подходящее сообщение).
    Если передан case_id: считаем только сообщения, относящиеся к этому делу (в тексте «дело №N»/«по делу N» для N=case_id, либо без номера дела — только если сообщение отправлено после sent_to_court_at).
    Без case_id — считаются все голоса в канале (как раньше)."""
    channel = bot.get_channel(channel_id)
    guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else (channel.guild if channel else None)
    if not channel or not judge_role_id:
        return {"count": 0, "judge_ids": [], "votes": {}, "votes_list": [], "two_approved": False, "two_rejected": False}
    votes: dict[int, str] = {}  # author_id -> "yes" | "no"
    votes_list: list[dict[str, Any]] = []  # [{judge_id, vote, message_id, voted_at}, ...]
    yes_markers = (
        "да", "за", "одобряю", "одобрено", "за референдум", "за проведение", "согласен", "согласна",
        "поддерживаю", "поддержую", "yes", "+", "👍", "за.", "да.", "одобряем", "одобряю."
    )
    no_markers = (
        "нет", "против", "отклоняю", "отклонено", "не одобряю", "не поддерживаю",
        "no", "-", "👎", "против.", "нет.", "отклоняем"
    )
    try:
        async for msg in channel.history(limit=limit):
            if not msg.author or getattr(msg.author, "bot", False):
                continue
            if not msg.guild:
                continue
            member = msg.guild.get_member(msg.author.id)
            if member is None and guild:
                try:
                    member = await guild.fetch_member(msg.author.id)
                except Exception:
                    pass
            if member is None:
                continue
            if not any(r.id == judge_role_id for r in member.roles):
                continue
            if msg.author.id in votes:
                continue
            text = (msg.content or "").strip().lower()
            if any(m in text for m in yes_markers):
                vote_val = "yes"
            elif any(m in text for m in no_markers):
                vote_val = "no"
            else:
                vote_val = "unknown"
            if vote_val == "unknown":
                continue
            # Привязка к делу: если указан case_id — считаем только голоса по этому делу.
            # Важно: учитываем только сообщения, отправленные после отправки дела в суд (sent_to_court_at).
            # Иначе после очистки БД старые сообщения в канале («дело №1 за») ошибочно считаются голосами по новому делу №1.
            if case_id is not None:
                if _message_refers_to_other_case(msg.content or "", case_id):
                    continue
                if sent_to_court_at is None:
                    continue
                msg_at = getattr(msg, "created_at", None)
                if not msg_at:
                    continue
                if msg_at.tzinfo is None:
                    msg_at = msg_at.replace(tzinfo=timezone.utc)
                st_at = sent_to_court_at
                if st_at.tzinfo is None:
                    st_at = st_at.replace(tzinfo=timezone.utc)
                if msg_at < st_at:
                    continue
            votes[msg.author.id] = vote_val
            voted_at = getattr(msg, "created_at", None)
            if voted_at and getattr(voted_at, "isoformat", None):
                voted_at = voted_at.isoformat()
            votes_list.append({
                "judge_id": msg.author.id,
                "vote": vote_val,
                "message_id": getattr(msg, "id", None),
                "voted_at": voted_at,
            })
    except Exception as e:
        logger.warning("Ошибка при подсчёте голосов судей: %s", e)
        return {"count": 0, "judge_ids": [], "votes": {}, "votes_list": [], "two_approved": False, "two_rejected": False}
    count = len(votes)
    yes_count = sum(1 for v in votes.values() if v == "yes")
    no_count = sum(1 for v in votes.values() if v == "no")
    two_approved = count == 2 and yes_count == 2
    two_rejected = count == 2 and no_count == 2
    return {
        "count": count,
        "judge_ids": list(votes.keys()),
        "votes": votes,
        "votes_list": votes_list,
        "two_approved": two_approved,
        "two_rejected": two_rejected,
        "yes_count": yes_count,
        "no_count": no_count,
    }


def _is_emoji_only_message(content: str) -> bool:
    """Сообщение считается «только эмодзи»: :word:, Discord <:name:id>/<a:name:id>, или один/несколько Unicode-эмодзи. Тогда GPT не нужен."""
    s = (content or "").strip()
    if not s:
        return False
    # Короткий код :word: (одно или несколько)
    if re.match(r"^(:[\w]+:\s*)+$", s):
        return True
    # Кастомное эмодзи Discord в сообщении (одно или несколько подряд)
    if re.match(r"^(<a?:[\w]+:\d+>\s*)+$", s):
        return True
    # Короткое сообщение из символов в «эмодзи-диапазонах» (без кириллицы/латиницы)
    if 1 <= len(s) <= 6:
        for c in s:
            o = ord(c)
            if o <= 127 or (0x0400 <= o <= 0x04FF):
                return False
        if any(o >= 0x1F300 for o in (ord(c) for c in s)):
            return True
    return False


def _is_procedure_submission(content: str) -> bool:
    """True только если сообщение — явная заявка на процедуру (законопроект, референдум, гражданская инициатива), а не вопрос или обсуждение."""
    text = (content or "").strip()
    if len(text) < 10:
        return False
    t = text.lower().replace("ё", "е")
    # Вопросы и запросы информации — не заявки
    question_starts = ("какие", "какой", "какая", "когда", "кто ", "что такое", "что значит", "как ", "сколько", "где ", "почему", "зачем", "статья ", "ст. ", "часть ", "п. ")
    if any(t.startswith(s) or t.startswith(s.rstrip()) for s in question_starts):
        return False
    if t.startswith("кто у нас") or " кто " in t and ("судь" in t or "прокурор" in t or "роли" in t):
        return False
    # Явная заявка: тип процедуры + разделитель + суть (или устойчивая формула запроса)
    if "законопроект" in t and (" — " in t or ":" in t or " законопроект " in t):
        rest = t.split("законопроект", 1)[-1].strip()
        if rest.startswith("—") or rest.startswith(":") or len(rest) > 3:
            return True
    if "референдум" in t and (" — " in t or " по вопросу " in t or "запрос на референдум" in t or "прошу провести референдум" in t or "провести референдум" in t):
        return True
    if "гражданская инициатива" in t and (" — " in t or ":" in t):
        return True
    if "прошение о законопроекте" in t or "прошение о том" in t:
        return True
    # «Подай/передай прошение в суд» или «подай в суд как закон» с содержательной формулировкой (роль, статья, пункты)
    if ("подай прошение в суд" in t or "подай в суд" in t or "передай прошение в суд" in t) and (
        "закон" in t or "роль" in t or "статья" in t or "положение" in t or re.search(r"\d+\)", t)
    ):
        return True
    return False


# Короткие фразы согласия/подтверждения — не создаём по ним новое дело, используем открытое дело ветки
_AGREEMENT_PHRASES = (
    "я готов", "я готова", "готов", "готова", "да", "давай", "ок", "окей", "помогай", "помоги",
    "оформи", "оформите", "отправляй", "отправь", "передай в суд", "передай в совет",
    "согласен", "согласна", "да, отправляй", "да отправляй", "ниче не отправил", "ничего не отправил",
    "далбайоп", "далбайоп рабатисираваная строктора",
)


def _is_agreement_only_message(content: str) -> bool:
    """Сообщение — только согласие/короткое подтверждение без содержательной сути (не создаём новое дело)."""
    text = (content or "").strip().lower()
    if len(text) > 80:
        return False
    return any(p in text for p in _AGREEMENT_PHRASES) or (len(text) <= 3 and text.isalpha())


async def _get_reusable_branch_case(guild_id: int, branch_case_id: int | None) -> int | None:
    """Если в ветке есть открытое дело, ещё не переданное в суд — вернуть его id для повторного использования."""
    if not branch_case_id:
        return None
    async with get_db() as session:
        result = await session.execute(
            select(ElderCase).where(
                ElderCase.id == branch_case_id,
                ElderCase.guild_id == guild_id,
                ElderCase.status == "open",
                ElderCase.sent_to_court_at.is_(None),
            )
        )
        case = result.scalars().one_or_none()
    return case.id if case else None


def _detect_case_type(content: str) -> str:
    """По тексту обращения определяем тип дела: референдум, гражданская инициатива (ст. 19), законопроект или апелляция. В базе хранятся реальные процедуры."""
    text = (content or "").lower()
    ref_markers = (
        "референдум", "референдума", "референдуму", "проведени", "провести референдум",
        "прошу рассмотреть возможность проведения референдума", "запрос на референдум",
    )
    if any(m in text for m in ref_markers):
        return "referendum_request"
    civil_markers = (
        "прошение о законопроекте", "прошение о том", "гражданская инициатива", "статья 19", "ст. 19",
    )
    if any(m in text for m in civil_markers):
        return "civil_initiative"
    bill_markers = (
        "рассмотрели закон", "законопроект", "законопроекта", "внести закон",
        "подай прошение в суд как закон", "подай в суд как закон", "закон про ",
    )
    if any(m in text for m in bill_markers):
        return "bill"
    return "appeal_procedure"


def _case_display_number(case: ElderCase) -> int:
    """Номер дела для показа пользователю («дело №N»). Используем guild_case_number, иначе id."""
    n = getattr(case, "guild_case_number", None)
    return n if n is not None else case.id


async def _get_case_display_number(guild_id: int, case_id: int) -> int:
    """Загружает дело и возвращает номер для отображения («дело №N»)."""
    async with get_db() as session:
        r = await session.execute(select(ElderCase).where(ElderCase.id == case_id, ElderCase.guild_id == guild_id))
        case = r.scalars().one_or_none()
        return _case_display_number(case) if case else case_id


async def _create_elder_case(guild_id: int, author_id: int, channel_id: int, thread_id: int | None, content: str) -> int:
    case_type = _detect_case_type(content)
    async with get_db() as session:
        # Бэкфилл: у старых записей без guild_case_number проставляем 1, 2, 3… по id
        nulls = await session.execute(
            select(ElderCase).where(ElderCase.guild_id == guild_id, ElderCase.guild_case_number.is_(None)).order_by(ElderCase.id.asc())
        )
        for i, row in enumerate(nulls.scalars().all(), start=1):
            await session.execute(
                update(ElderCase).where(ElderCase.id == row.id, ElderCase.guild_id == guild_id).values(guild_case_number=i)
            )
        # Следующий порядковый номер в гильдии
        max_res = await session.execute(select(func.coalesce(func.max(ElderCase.guild_case_number), 0)).where(ElderCase.guild_id == guild_id))
        next_num = (max_res.scalar() or 0) + 1
        case = ElderCase(
            guild_id=guild_id,
            guild_case_number=next_num,
            case_type=case_type,
            status="open",
            author_id=author_id,
            channel_id=channel_id,
            thread_id=thread_id,
            initial_content=content,
        )
        session.add(case)
        await session.flush()
        return case.id


class ElderBot(RoleBot):
    def __init__(self, deps: RoleDeps, **kwargs: object) -> None:
        super().__init__(role_key="elder", deps=deps, command_prefix="!", **kwargs)
        self._inbox_channel_id: int | None = None
        self._watch_channel_ids: list[int] = []

    async def create_elder_case(
        self, guild_id: int, author_id: int, channel_id: int, thread_id: int | None, content: str
    ) -> int:
        """Создать дело старейшины (при одобрении заявки). Номер дела присваивается здесь. Возвращает case_id."""
        return await _create_elder_case(guild_id, author_id, channel_id, thread_id, content)

    def _agent_context(self, guild_id: int, extra: dict[str, Any] | None = None) -> AgentContext:
        cfg = self.config
        channel_ids = {}
        for purpose in ("inbox", "decisions", "outbox", "notify_court", "notify_council", "referrals"):
            ch_id = cfg.channel_for_role(self.role_key, purpose)
            if ch_id:
                channel_ids[purpose] = ch_id
        return AgentContext(
            guild_id=guild_id,
            channel_ids=channel_ids,
            bot=self,
            db_session_factory=self.deps.db_session_factory,
            extra=extra or {},
        )

    def _build_agent(self, ctx: AgentContext) -> Agent:
        system_prompt = self.load_system_prompt()
        tools = make_elder_tools(ctx)
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
        self._watch_channel_ids = self.config.watch_channel_ids(self.role_key)
        if self._inbox_channel_id:
            logger.info("Старейшина: inbox канал %s (читает все сообщения, сам решает кому отвечать)", self._inbox_channel_id)
        if self._watch_channel_ids:
            logger.info("Старейшина: надзор за каналами %s (проверка легитимности действий)", self._watch_channel_ids)
        self.loop.create_task(self._deadline_watch_loop())
        self.loop.create_task(self._channel_sync_loop())

    async def _deadline_watch_loop(self) -> None:
        """Фоновое наблюдение за сроками: раз в N минут проверяем дела с истёкшим сроком суда, подгружаем закон, действуем по закону."""
        interval_min = 15
        rcfg = self.config.role_config(self.role_key)
        if isinstance(rcfg, dict) and "deadline_check_interval_minutes" in rcfg:
            try:
                interval_min = max(1, int(rcfg["deadline_check_interval_minutes"]))
            except (TypeError, ValueError):
                pass
        await self.wait_until_ready()
        await self._sync_channels_on_startup()
        logger.info("Старейшина: наблюдение за сроками суда включено (интервал %s мин)", interval_min)
        while True:
            try:
                await asyncio.sleep(interval_min * 60)
                await self._check_expired_deadlines()
                await self._remind_judges_pending_vote()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Старейшина: ошибка в цикле наблюдения за сроками: %s", e)

    async def _check_expired_deadlines(self) -> None:
        """Найти дела с истёкшим сроком суда (без эскалации), подгрузить закон из двух каналов, вызвать агента для действий по закону."""
        guild_id = self.config.guild_id
        if not guild_id:
            return
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase).where(
                    ElderCase.guild_id == guild_id,
                    ElderCase.status == "open",
                    ElderCase.sent_to_court_at.isnot(None),
                    ElderCase.court_decided_at.is_(None),
                    ElderCase.deadline_escalation_at.is_(None),
                )
            )
            cases = result.scalars().all()
        to_escalate = []
        for c in cases:
            deadline = _deadline_from_case(c)
            sent_at = c.sent_to_court_at
            if not sent_at:
                continue
            sent_utc = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
            if (now - sent_utc) > deadline:
                to_escalate.append(c)
        if not to_escalate:
            return
        law_block = await get_law_block_async(
            self, guild_id, max_chars=7000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        for case in to_escalate:
            try:
                await self._escalate_expired_case(case, law_block)
            except Exception as e:
                logger.exception("Старейшина: эскалация по делу №%s: %s", case.id, e)

    async def _remind_judges_pending_vote(self) -> None:
        """Раз в цикл (каждые N мин по конфигу): по каждому делу — отчёт по сроку и кто проголосовал, сообщение с упоминанием судей. Первое напоминание по делу — не раньше чем через 2 мин после отправки в суд (чтобы не спамить сразу)."""
        guild_id = self.config.guild_id
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
        judge_role_id = self.config.role_ids().get("judge") or 0
        if not guild_id or not court_ch_id or not judge_role_id:
            return
        cases = await self._get_pending_court_cases(guild_id)
        if not cases:
            return
        # Не ждём 15 мин — иначе при коротком сроке (напр. 15 мин) напоминания не успевают. Минимум 2 мин после отправки дела.
        min_wait_minutes = 2
        guild = self.get_guild(guild_id)
        if not guild:
            return
        role = guild.get_role(judge_role_id)
        if not role or not getattr(role, "members", None):
            return
        all_judge_ids = [m.id for m in role.members if not getattr(m, "bot", False)]
        channel = self.get_channel(court_ch_id)
        if not channel:
            return
        now_utc = datetime.now(timezone.utc)
        for case in cases:
            sent_at = case.sent_to_court_at
            if not sent_at:
                continue
            sent_utc = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
            if (now_utc - sent_utc) < timedelta(minutes=min_wait_minutes):
                continue
            deadline = _deadline_from_case(case)
            if (now_utc - sent_utc) > deadline:
                continue
            async with get_db() as session:
                result = await session.execute(
                    select(ElderCaseCourtVote)
                    .where(
                        ElderCaseCourtVote.case_id == case.id,
                        ElderCaseCourtVote.guild_id == guild_id,
                    )
                )
                votes = result.scalars().all()
            voted_ids = {v.judge_id for v in votes}
            not_voted = [jid for jid in all_judge_ids if jid not in voted_ids]
            if not not_voted:
                continue
            deadline_text = self._case_deadline_text(case)
            voted_names = []
            for vid in voted_ids:
                mem = guild.get_member(vid)
                name = getattr(mem, "display_name", None) or (getattr(mem, "name", None) if mem else None) or str(vid)
                voted_names.append(name)
            voted_str = ", ".join(voted_names) if voted_names else "пока никого"
            mentions = " ".join(f"<@{uid}>" for uid in not_voted)
            case_desc = (
                (getattr(case, "sent_to_court_content", None) or "").strip()
                or (case.initial_content or "").strip()
            )
            if case_desc:
                case_desc = case_desc[:280] + ("…" if len(case_desc) > 280 else "")
            else:
                case_desc = "—"
            display_no = _case_display_number(case)
            text = (
                f"**По делу №{display_no}** ({deadline_text}). "
                f"Суть: {case_desc}\n"
                f"Проголосовали (с ролью судьи): {voted_str}. "
                f"Прошу проголосовать (за или против): {mentions}\n"
                "Проголосуйте ответом на это сообщение или на исходное по делу: за или против."
            )
            try:
                await channel.send(text[:2000])
                logger.info("Старейшина: напоминание по делу №%s — проголосовали: %s; упомянуты: %s", case.id, voted_str, len(not_voted))
            except Exception as e:
                logger.warning("Старейшина: не удалось отправить напоминание по делу №%s: %s", case.id, e)

    async def _sync_channels_on_startup(self) -> None:
        """При запуске: просмотреть каналы надзора, проверить новое (голоса судей, сообщения), обновить БД."""
        guild_id = self.config.guild_id
        if not guild_id or not self._watch_channel_ids:
            return
        logger.info("Старейшина: синхронизация каналов при запуске...")
        try:
            for ch_id in self._watch_channel_ids:
                try:
                    await self._sync_one_channel(ch_id, guild_id)
                except Exception as e:
                    logger.exception("Синхронизация канала %s: %s", ch_id, e)
        except Exception as e:
            logger.exception("Синхронизация каналов при запуске: %s", e)

    async def _channel_sync_loop(self) -> None:
        """Периодически проверять каналы на новое и обновлять БД (голоса судей, решения)."""
        interval_min = 10
        rcfg = self.config.role_config(self.role_key)
        if isinstance(rcfg, dict) and "channel_sync_interval_minutes" in rcfg:
            try:
                interval_min = max(1, int(rcfg["channel_sync_interval_minutes"]))
            except (TypeError, ValueError):
                pass
        await self.wait_until_ready()
        logger.info("Старейшина: цикл синхронизации каналов (интервал %s мин)", interval_min)
        while True:
            try:
                await asyncio.sleep(interval_min * 60)
                guild_id = self.config.guild_id
                if not guild_id:
                    continue
                court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
                if court_ch_id:
                    try:
                        await self._sync_court_channel(guild_id, court_ch_id)
                    except Exception as e:
                        logger.exception("Синхронизация канала суда: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Старейшина: ошибка в цикле синхронизации каналов: %s", e)

    async def _sync_court_channel(self, guild_id: int, court_channel_id: int) -> None:
        """Проверить канал суда: подсчитать голоса по роли судьи, обновить отчёт по первому делу, при решении двух судей — занести в БД."""
        guild = self.get_guild(guild_id)
        if not guild:
            return
        case = await self._get_first_pending_court_case(guild_id)
        if not case or getattr(case, "court_decided_at", None):
            return
        judge_role_id = self.config.role_ids().get("judge") or 0
        sent_at = getattr(case, "sent_to_court_at", None)
        vote_info = await _count_judge_votes_in_channel(
            self, court_channel_id, guild_id, judge_role_id, limit=30,
            case_id=case.id, sent_to_court_at=sent_at,
        )
        if vote_info.get("two_approved") or vote_info.get("two_rejected"):
            vote_summary = _build_judge_vote_summary(guild, vote_info)
            await self._record_court_decision_and_inform(
                guild, case.id,
                approved=vote_info.get("two_approved"),
                vote_summary=vote_summary,
                vote_info=vote_info,
            )
            logger.info("Старейшина (sync): решение суда по делу №%s занесено в БД после проверки канала", case.id)
        else:
            # Голоса в канале относятся к первому ожидающему делу — обновляем отчёт по нему
            await self._update_case_votes_from_channel(guild_id, case.id, vote_info)

    async def _sync_one_channel(self, channel_id: int, guild_id: int) -> None:
        """Просмотреть канал: какие сообщения уже в базе, какие новые — занести в отчёт; для канала суда проверить голоса."""
        channel = self.get_channel(channel_id)
        if not channel or not channel.guild:
            return
        guild = channel.guild
        channel_name = getattr(channel, "name", str(channel_id))
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
        seen_ids = await self._get_logged_message_ids(channel_id, guild_id, limit=200)
        new_count = 0
        try:
            async for msg in channel.history(limit=50):
                if not msg.id or msg.id in seen_ids:
                    continue
                if getattr(msg.author, "bot", False):
                    continue
                summary = (msg.content or "").strip()[:400]
                await self._log_court_event(
                    guild_id, channel_id, msg.id, getattr(msg.author, "id", None),
                    "message_synced",
                    summary or "(без текста)",
                    None,
                )
                new_count += 1
                seen_ids.add(msg.id)
        except Exception as e:
            logger.warning("История канала %s (%s): %s", channel_id, channel_name, e)
            return
        if new_count:
            logger.info("Старейшина (sync): канал %s — внесено %s новых сообщений в отчёт", channel_name, new_count)
        if channel_id == court_ch_id:
            await self._sync_court_channel(guild_id, court_ch_id)

    async def _get_logged_message_ids(self, channel_id: int, guild_id: int, limit: int = 500) -> set[int]:
        """ID сообщений канала, которые уже есть в отчёте (ElderCourtLog)."""
        async with get_db() as session:
            result = await session.execute(
                select(ElderCourtLog.message_id)
                .where(
                    and_(
                        ElderCourtLog.guild_id == guild_id,
                        ElderCourtLog.channel_id == channel_id,
                        ElderCourtLog.message_id.isnot(None),
                    )
                )
                .limit(limit)
            )
            rows = result.scalars().all()
        return {r for r in rows if r}

    async def _get_pending_court_cases(self, guild_id: int) -> list[ElderCase]:
        """Все дела, ожидающие решения суда (по дате отправки в суд)."""
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == guild_id,
                    ElderCase.status == "open",
                    ElderCase.sent_to_court_at.isnot(None),
                    ElderCase.court_decided_at.is_(None),
                )
                .order_by(ElderCase.sent_to_court_at.asc())
            )
            return list(result.scalars().all())

    async def _get_first_pending_court_case(self, guild_id: int) -> ElderCase | None:
        """Первое (по дате отправки в суд) дело, ожидающее решения суда."""
        cases = await self._get_pending_court_cases(guild_id)
        return cases[0] if cases else None

    async def _get_cases_sent_to_court_summary(self, guild_id: int, limit: int = 20) -> str:
        """Краткий список дел, переданных в суд (ожидающих или уже рассмотренных), для проверки: решение/прецедент должны относиться к реальному делу."""
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == guild_id,
                    ElderCase.sent_to_court_at.isnot(None),
                )
                .order_by(ElderCase.sent_to_court_at.desc())
                .limit(limit)
            )
            cases = result.scalars().all()
        if not cases:
            return "Дела, переданные в суд: пока нет (список пуст). Любое решение или прецедент без поступившего запроса — нелегитимно."
        lines = []
        for c in cases:
            content = (getattr(c, "sent_to_court_content", None) or c.initial_content or "").strip()[:300]
            if len((getattr(c, "sent_to_court_content", None) or c.initial_content or "").strip()) > 300:
                content += "..."
            status = "решение принято" if getattr(c, "court_decided_at", None) else "ожидает решения"
            lines.append(f"  id={c.id} | {status} | суть: {content or '(нет текста)'}")
        return "Дела, переданные в суд (проверяй по ним соответствие решений и прецедентов):\n" + "\n".join(lines)

    async def _get_cases_sent_to_council_summary(self, guild_id: int, limit: int = 20) -> str:
        """Краткий список дел, переданных на исполнение в совет (send_to_council или решение суда), для проверки: указ должен относиться к реальному делу."""
        from sqlalchemy import or_
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == guild_id,
                    or_(
                        ElderCase.elder_decision == "send_to_council",
                        ElderCase.court_decided_at.isnot(None),
                    ),
                )
                .order_by(ElderCase.updated_at.desc())
                .limit(limit)
            )
            cases = result.scalars().all()
        if not cases:
            return "Дела, переданные на исполнение в совет: пока нет (список пуст). Указ без поступившего решения/дела — нелегитимно."
        lines = []
        for c in cases:
            content = (c.initial_content or "").strip()[:300]
            if (c.initial_content or "").strip() and len((c.initial_content or "").strip()) > 300:
                content += "..."
            source = "передано старейшиной" if c.elder_decision == "send_to_council" else ("решение суда" if getattr(c, "court_decided_at", None) else "—")
            lines.append(f"  id={c.id} | {source} | суть: {content or '(нет текста)'}")
        return "Дела, переданные на исполнение в совет (проверяй по ним соответствие указов):\n" + "\n".join(lines)

    async def _record_court_decision_and_inform(
        self, guild: Any, case_id: int, approved: bool, vote_summary: str,
        vote_info: dict[str, Any] | None = None,
    ) -> None:
        """Зафиксировать решение суда (двое согласны) в БД: дело, голоса судей по делу (время, кто как), отчёт, уведомление в канал решений."""
        now = datetime.now(timezone.utc)
        result_str = "approved" if approved else "rejected"
        async with get_db() as session:
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == case_id, ElderCase.guild_id == guild.id)
                .values(court_decided_at=now, court_result=result_str)
            )
            # Сохраняем голоса судей по делу — источник правды для ответов «кто проголосовал»
            if vote_info:
                await session.execute(
                    delete(ElderCaseCourtVote).where(
                        ElderCaseCourtVote.case_id == case_id,
                        ElderCaseCourtVote.guild_id == guild.id,
                    )
                )
                votes_list = vote_info.get("votes_list") or []
                for v in votes_list:
                    judge_id = v.get("judge_id")
                    vote_val = (v.get("vote") or "").strip().lower()
                    if judge_id is None or vote_val not in ("yes", "no"):
                        continue
                    voted_at = now
                    if v.get("voted_at"):
                        try:
                            voted_at = datetime.fromisoformat(str(v["voted_at"]).replace("Z", "+00:00"))
                            if voted_at.tzinfo is None:
                                voted_at = voted_at.replace(tzinfo=timezone.utc)
                        except Exception:
                            pass
                    entry = ElderCaseCourtVote(
                        case_id=case_id,
                        guild_id=guild.id,
                        judge_id=int(judge_id),
                        vote=vote_val,
                        message_id=v.get("message_id"),
                        voted_at=voted_at,
                    )
                    session.add(entry)
        display_no = await _get_case_display_number(guild.id, case_id)
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court") or 0
        await self._log_court_event(
            guild.id, court_ch_id, None, None,
            "court_decision",
            f"Дело №{display_no}: решение суда — {result_str}. Голоса: {vote_summary}",
            {"case_id": case_id, "court_result": result_str},
        )
        ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
        if ch_decisions:
            ch = self.get_channel(ch_decisions)
            if ch:
                result_text = "одобрено" if approved else "отклонено"
                try:
                    await ch.send(
                        f"**По делу №{display_no}: решение суда принято успешно.**\n"
                        f"Голоса: {vote_summary}.\nРезультат: **{result_text}**."
                    )
                except Exception as e:
                    logger.exception("Не удалось отправить уведомление о решении суда: %s", e)
        logger.info("Старейшина: решение суда по делу №%s записано в БД: %s", case_id, result_str)

    async def _update_case_votes_from_channel(
        self, guild_id: int, case_id: int, vote_info: dict[str, Any],
    ) -> None:
        """Записать голоса по делу из vote_info в ElderCaseCourtVote (отчёт по делу — кто из судей проголосовал). Не меняет court_decided_at."""
        if not vote_info or not vote_info.get("votes_list"):
            return
        guild = self.get_guild(guild_id)
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            await session.execute(
                delete(ElderCaseCourtVote).where(
                    ElderCaseCourtVote.case_id == case_id,
                    ElderCaseCourtVote.guild_id == guild_id,
                )
            )
            for v in vote_info.get("votes_list") or []:
                judge_id = v.get("judge_id")
                vote_val = (v.get("vote") or "").strip().lower()
                if judge_id is None or vote_val not in ("yes", "no"):
                    continue
                voted_at = now
                if v.get("voted_at"):
                    try:
                        voted_at = datetime.fromisoformat(str(v["voted_at"]).replace("Z", "+00:00"))
                        if voted_at.tzinfo is None:
                            voted_at = voted_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                session.add(ElderCaseCourtVote(
                    case_id=case_id,
                    guild_id=guild_id,
                    judge_id=int(judge_id),
                    vote=vote_val,
                    message_id=v.get("message_id"),
                    voted_at=voted_at,
                ))

    async def _send_case_to_court_fallback(
        self, guild_id: int, case_id: int, content: str, author_name: str, author_id: int
    ) -> bool:
        """Если модель сказала «передал в суд», но не вызвала notify_court — отправить в суд из кода и занести в БД."""
        logger.info("Старейшина: _send_case_to_court_fallback вызван guild_id=%s case_id=%s", guild_id, case_id)
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            logger.warning("Старейшина: fallback не выполнен — case_id не число: %s", case_id)
            return False
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == guild_id)
            )
            case = result.scalars().one_or_none()
        if not case:
            logger.warning("Старейшина: fallback не выполнен — дело case_id=%s не найдено", cid)
            return False
        if case.sent_to_court_at is not None:
            logger.warning("Старейшина: fallback не выполнен — дело №%s уже передано в суд (sent_to_court_at)", cid)
            return False
        if case.case_type not in ("referendum_request", "civil_initiative", "bill"):
            logger.warning("Старейшина: fallback не выполнен — тип дела %s не подходит для суда", case.case_type)
            return False
        if getattr(case, "elder_decision", None) == "referendum_rejected":
            logger.warning("Старейшина: fallback не выполнен — дело №%s отклонено (referendum_rejected)", cid)
            return False
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
        if not court_ch_id:
            logger.warning("Старейшина: fallback не выполнен — канал суда (notify_court) не настроен")
            return False
        channel = self.get_channel(court_ch_id)
        if not channel:
            logger.warning("Старейшина: fallback не выполнен — канал суда id=%s не найден", court_ch_id)
            return False
        # Убрать из сути просьбу «отправь в совет», чтобы в суд ушла только суть (напр. «создать роль X»)
        substance = (content or "").strip()
        for prefix in ("отправь в совет ", "отправляй в совет ", "отправь в совет", "отправляй в совет"):
            if substance.lower().startswith(prefix.lower()):
                substance = substance[len(prefix) :].strip()
                break
        if not substance:
            substance = (content or "").strip()
        petition_text = (
            f"Дело №{_case_display_number(case)}. От гражданина {author_name} (<@{author_id}>): законопроект — {substance}. "
            "Проголосуйте ответом на это сообщение: за или против."
        )
        mentions = _mentions_for_role(self, guild_id, "judge")
        full = (f"{mentions}\n\n{petition_text}" if mentions else petition_text).strip()[:2000]
        try:
            await channel.send(full)
        except Exception as e:
            logger.exception("Fallback: не удалось отправить прошение в суд: %s", e)
            return False
        deadline_hours = 24.0
        try:
            rcfg = self.config.role_config(self.role_key)
            if isinstance(rcfg, dict):
                val = rcfg.get("court_deadline_hours", 24)
                deadline_hours = float(val) if val is not None else 24.0
                if deadline_hours <= 0:
                    deadline_hours = 24.0
        except (TypeError, ValueError):
            pass
        now = datetime.now(timezone.utc)
        if 0 < deadline_hours < 1:
            values = {"sent_to_court_at": now, "court_deadline_minutes": round(deadline_hours * 60), "court_deadline_hours": None, "sent_to_court_content": petition_text.strip()[:8000]}
        else:
            values = {"sent_to_court_at": now, "court_deadline_hours": round(deadline_hours), "court_deadline_minutes": None, "sent_to_court_content": petition_text.strip()[:8000]}
        values["elder_decided_at"] = now
        values["elder_decision"] = "referendum_approved"
        values["elder_reasoning"] = "Одобрено к передаче в суд (оформлено автоматически при передаче)."
        values["elder_already_decided"] = True
        async with get_db() as session:
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == cid, ElderCase.guild_id == guild_id)
                .values(**values)
            )
        display_no = _case_display_number(case)
        ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
        if ch_decisions:
            ch = self.get_channel(ch_decisions)
            if ch:
                try:
                    await ch.send(
                        f"**По делу №{display_no}:** Принято решение: **одобрено, передано в суд** (referendum_approved). "
                        "Обращение передано в канал суда; отсчёт срока начат."
                    )
                    logger.info("Старейшина: fallback — сообщение в канал решений отправлено (channel_id=%s)", ch_decisions)
                except Exception as e:
                    logger.exception("Старейшина: fallback — не удалось отправить в канал решений (channel_id=%s): %s", ch_decisions, e)
            else:
                logger.warning("Старейшина: fallback — канал решений id=%s не найден ботом", ch_decisions)
        else:
            logger.warning("Старейшина: fallback — канал решений (decisions) не настроен в конфиге")
        return True

    def _case_deadline_text(self, case: ElderCase) -> str:
        """Текст по сроку дела для напоминаний: «осталось X ч» / «срок истёк»."""
        sent_at = case.sent_to_court_at
        deadline = _deadline_from_case(case)
        if not sent_at:
            return "срок не начат"
        now = datetime.now(timezone.utc)
        sent_utc = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
        deadline_at = sent_utc + deadline
        delta = deadline_at - now
        secs = int(delta.total_seconds())
        if secs <= 0:
            if -secs >= 3600:
                return f"срок истёк {-secs // 3600} ч назад"
            if -secs >= 60:
                return f"срок истёк {-secs // 60} мин назад"
            return "срок истёк"
        if secs >= 3600:
            return f"осталось {secs // 3600} ч {(secs % 3600) // 60} мин"
        if secs >= 60:
            return f"осталось {secs // 60} мин"
        return "осталось менее минуты"

    async def _return_case_to_elder(self, case_id: int, reason: str, guild_id: int) -> None:
        """Дело возвращено старейшине: фиксируем, постим в канал решений и запускаем агента для решения по закону."""
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == case_id, ElderCase.guild_id == guild_id)
                .values(returned_to_elder_at=now, returned_to_elder_reason=reason)
            )
        display_no = await _get_case_display_number(guild_id, case_id)
        ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
        if ch_decisions:
            ch = self.get_channel(ch_decisions)
            if ch:
                try:
                    await ch.send(
                        f"**Дело №{display_no} возвращено старейшинам.**\n"
                        f"Причина: {reason}"
                    )
                except Exception as e:
                    logger.exception("Не удалось отправить уведомление о возврате дела: %s", e)
        law_block = await get_law_block_async(
            self, guild_id, max_chars=7000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        user_content = (
            "[ ДЕЛО ВОЗВРАЩЕНО СТАРЕЙШИНАМ — ДЕЙСТВУЙ ПО ЗАКОНУ ]\n\n"
            f"Дело №{display_no}. Причина возврата: {reason}\n\n"
            "По закону из блока выше определи, что делать с этим делом (подтвердить процесс, вернуть в суд с указанием нарушения, передать в совет и т.д.). "
            "Вызови нужные инструменты: get_case, list_cases_pending_court, publish_decision, notify_court, notify_council — и выполни действия. Не жди уведомления от пользователя."
        )
        messages_for_llm = [
            {"role": "user", "content": law_block + "\n\n---\n\n" + user_content},
        ]
        ctx = self._agent_context(guild_id, extra={"current_case_id": case_id})
        agent = self._build_agent(ctx)
        try:
            await agent.run(messages_for_llm)  # returns (reply, tools_called)
            logger.info("Старейшина обработал возвращённое дело №%s", case_id)
        except Exception as e:
            logger.exception("Ошибка агента при обработке возвращённого дела №%s: %s", case_id, e)

    async def _escalate_expired_fallback_send_to_council(
        self,
        ctx: AgentContext,
        agent: Agent,
        case_id: int,
        guild_id: int,
        *,
        reason: str = "Срок суда истёк; передача в совет по умолчанию.",
        post_combined_message: bool = True,
    ) -> None:
        """Передать дело в совет (при истечении срока) и объявить решение в канале. post_combined_message: сразу отправить одно сообщение «возвращено + передано в совет»."""
        display_no = await _get_case_display_number(guild_id, case_id)
        by_name = getattr(agent, "_tool_by_name", None) or {t.name: t for t in agent.tools}
        case_id_str = str(case_id)
        # 0) Сразу объявить в канале решений: возвращено + решение (передано в совет), чтобы было видно, что старейшина это сделал
        if post_combined_message:
            ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
            if ch_decisions:
                ch = self.get_channel(ch_decisions)
                if ch:
                    try:
                        await ch.send(
                            f"**Дело №{display_no} возвращено старейшинам.**\n"
                            f"Причина: {reason}\n\n"
                            "**Решение старейшин: передано на исполнение в совет.**"
                        )
                    except Exception as e:
                        logger.warning("Fallback: не удалось отправить объявление в канал: %s", e)
        # 1) Решение: передать в совет (в БД и формальное сообщение от publish_decision)
        decision_recorded = False
        if "publish_decision" in by_name:
            try:
                out = await by_name["publish_decision"].execute(
                    case_id=case_id_str,
                    decision="send_to_council",
                    reasoning=reason,
                )
                decision_recorded = out and "опубликовано" in out.lower() or "зафиксировано" in out.lower()
            except Exception as e:
                logger.warning("Fallback publish_decision по делу №%s: %s", case_id, e)
        if not decision_recorded:
            now = datetime.now(timezone.utc)
            async with get_db() as session:
                await session.execute(
                    update(ElderCase)
                    .where(ElderCase.id == case_id, ElderCase.guild_id == guild_id)
                    .values(
                        status="closed",
                        elder_decided_at=now,
                        elder_decision="send_to_council",
                        elder_reasoning=reason,
                        elder_already_decided=True,
                    )
                )
            logger.info("Старейшина: по делу №%s решение send_to_council зафиксировано в БД (fallback)", case_id)
        # 2) Уведомить совет
        if "notify_council" in by_name:
            try:
                await by_name["notify_council"].execute(case_id=case_id_str)
            except Exception as e:
                logger.warning("Fallback notify_council по делу №%s: %s", case_id, e)
        logger.info("Старейшина: по делу №%s выполнена передача в совет (fallback)", case_id)

    async def _escalate_expired_case(self, case: ElderCase, law_block: str) -> None:
        """По одному делу с истёкшим сроком: сразу передаём в совет и одним сообщением объявляем «возвращено + передано в совет»."""
        guild_id = self.config.guild_id
        deadline = _deadline_from_case(case)
        total_mins = int(deadline.total_seconds() / 60)
        deadline_label = f"{total_mins} мин" if total_mins < 60 else f"{total_mins // 60} ч"
        reason = f"Срок суда истёк ({deadline_label}). Суд не вынес решение."
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == case.id, ElderCase.guild_id == guild_id)
                .values(
                    returned_to_elder_at=now,
                    returned_to_elder_reason=reason,
                    court_deadline_expired_at=now,
                )
            )
        display_no = _case_display_number(case)
        # Один раз уведомить суд о истечении срока
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
        if court_ch_id:
            court_ch = self.get_channel(court_ch_id)
            if court_ch:
                try:
                    mentions = _mentions_for_role(self, guild_id, "judge")
                    text = f"По делу №{display_no} срок истёк. Старейшины примут решение."
                    full = (f"{mentions}\n\n{text}" if mentions else text).strip()[:2000]
                    await court_ch.send(full)
                except Exception as e:
                    logger.warning("Не удалось отправить уведомление суду (срок истёк): %s", e)
        try:
            ctx = self._agent_context(guild_id, extra={"current_case_id": case.id})
            agent = self._build_agent(ctx)
            await self._escalate_expired_fallback_send_to_council(
                ctx, agent, case.id, guild_id,
                reason=reason,
                post_combined_message=True,
            )
        except Exception as e:
            logger.exception("Старейшина: ошибка при эскалации дела №%s (срок истёк): %s", case.id, e)
            # Всё равно помечаем, что эскалация начата, чтобы не обрабатывать дело бесконечно
        async with get_db() as session:
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == case.id, ElderCase.guild_id == guild_id)
                .values(deadline_escalation_at=datetime.now(timezone.utc))
            )
        logger.info("Старейшина: по делу №%s зафиксирована эскалация (срок истёк), передано в совет", case.id)

    async def on_message(self, message: Message) -> None:
        guild = message.guild
        channel_id = message.channel.id
        ch_name = getattr(message.channel, "name", channel_id)
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "?")
        is_bot = getattr(message.author, "bot", False)
        preview = (message.content or "")[:200].replace("\n", " ")
        logger.info(
            "Старейшина ← [#%s] %s%s: %s",
            ch_name, author_name, " (бот)" if is_bot else "", preview,
        )
        if message.author.bot:
            await self.process_commands(message)
            return

        # Надзор: сообщение в отслеживаемом канале (суд, совет) — проверяем легитимность действия
        if guild and self._watch_channel_ids and channel_id in self._watch_channel_ids:
            await self._handle_oversight(message)
            await self.process_commands(message)
            return

        inbox_id = self._inbox_channel_id or self.config.channel_for_role(self.role_key, "inbox")
        is_inbox = inbox_id and (channel_id == inbox_id or getattr(message.channel, "parent_id", None) == inbox_id)
        if not is_inbox:
            await self.process_commands(message)
            return
        if not guild:
            await self.process_commands(message)
            return

        pmj_role_id = self.config.role_ids().get("pmj") or 0
        if not _has_pmj_role(message, pmj_role_id):
            try:
                await message.reply("К старейшинам могут обращаться только граждане с ПМЖ. У вас нет статуса ПМЖ.")
            except Exception as e:
                logger.exception("Не удалось отправить отказ ПМЖ")
            await self.process_commands(message)
            return

        content = (message.content or "").strip()
        if not content:
            await self.process_commands(message)
            return

        if getattr(message.channel, "parent_id", None):
            channel_id = message.channel.parent_id
            thread_id = message.channel.id
        else:
            channel_id = message.channel.id
            thread_id = None

        # Дело создаём только при одобрении и передаче в суд (вызов create_elder_case агентом или fallback). При подаче заявки дело не создаём — старейшина сначала решает: одобрить или отклонить; при отклонении дело в базу не заносится.
        branch_summary_pre, branch_case_id_pre = await load_branch_summary(
            self.role_key, guild.id, channel_id, thread_id, message.author.id
        )
        reusable_case_id = await _get_reusable_branch_case(guild.id, branch_case_id_pre) if branch_case_id_pre else None
        pending_procedure_submission = False
        if _is_procedure_submission(content):
            case_id = None
            pending_procedure_submission = True
        elif _is_agreement_only_message(content) and reusable_case_id is not None:
            case_id = reusable_case_id
        else:
            case_id = None

        # Сообщение — эмодзи (текст :word:, Discord <:name:id>, или один Unicode-эмодзи): ответ случайным эмодзи, без GPT
        if _is_emoji_only_message(content):
            emoji_msg = self._pick_random_server_emoji_message(guild)
            if not emoji_msg:
                emoji_msg = random.choice(("✅", "👎", "👍", "❌", "⬆️", "🔴", "🟢", "⚪", "🙂", "📌"))
            try:
                await message.reply(emoji_msg)
            except Exception as e:
                logger.debug("Ответ эмодзи не удался: %s", e)
            author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "Гражданин"
            await save_message(
                role_key=self.role_key,
                guild_id=guild.id,
                channel_id=channel_id,
                thread_id=thread_id,
                case_id=case_id,
                discord_message_id=message.id,
                author_id=message.author.id,
                author_display_name=author_name,
                role="user",
                content=content,
            )
            await save_message(
                role_key=self.role_key,
                guild_id=guild.id,
                channel_id=channel_id,
                thread_id=thread_id,
                case_id=case_id,
                discord_message_id=None,
                author_id=None,
                author_display_name=None,
                role="assistant",
                content=emoji_msg,
            )
            await self.process_commands(message)
            return

        history = await load_recent_messages(
            self.role_key, guild.id, channel_id, thread_id, limit=22, author_id=message.author.id
        )
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "Гражданин"
        channel_names = {ch.id: getattr(ch, "name", str(ch.id)) for ch in guild.text_channels}
        other_branches = await load_all_branch_summaries(
            self.role_key, guild.id, limit=8, channel_names=channel_names
        )
        display_no = await _get_case_display_number(guild.id, case_id) if case_id is not None else None
        memory_block = _build_memory_block(
            channel_id, thread_id, message.author.id, author_name=author_name, branch_summary=branch_summary_pre,
            current_case_id=case_id, other_branches=other_branches, channel_names=channel_names,
            current_case_display_number=display_no,
            pending_procedure_submission=pending_procedure_submission,
        )
        # Роли обратившегося: передаём member из сообщения, чтобы данные были точными (не зависят от fetch_member/Intent)
        author_block, author_role_names = await get_author_roles_block_async(
            self, guild.id, message.author.id, author_name, member=getattr(message.author, "roles", None) and message.author or None
        )
        # Закон в контексте при каждом сообщении — оба канала права из конфига (база, судебные прецеденты) целиком
        law_block = await get_law_block_async(
            self, guild.id, max_chars=18000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        channels_json = get_guild_channels_json(self, guild.id)
        roles_json = get_guild_roles_and_members_json(self, guild.id)
        # Ограничение размера контекста (лимит TPM 30k): каналы+роли не более ~10k символов
        _max_data_chars = 10000
        _data = "Каналы:\n" + channels_json + "\n\nРоли и участники:\n" + roles_json
        if len(_data) > _max_data_chars:
            _data = _data[:_max_data_chars] + "\n[... обрезано для лимита токенов ...]"
        ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
        ch_court = self.config.channel_for_role(self.role_key, "notify_court")
        ch_council = self.config.channel_for_role(self.role_key, "notify_council")
        ch_judicial = self.config.channels().get("law_judicial_precedents") if hasattr(self.config, "channels") and callable(self.config.channels) else None
        now_utc = datetime.now(timezone.utc)
        months_ru = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        time_line = f"Текущее время (UTC): {now_utc.day} {months_ru[now_utc.month - 1]} {now_utc.year}, {now_utc.hour:02d}:{now_utc.minute:02d}. Используй его для ответов «сколько сейчас время?» и для расчёта истечения сроков по делам.\n"
        elder_channels_line = (
            f"Каналы старейшин: decisions={ch_decisions or '—'}, notify_court={ch_court or '—'}, notify_council={ch_council or '—'}, "
            f"law_judicial_precedents={ch_judicial or '—'} (прецеденты — publish_judicial_precedent). "
            f"Для суда/совета: notify_court/notify_council; для прецедента по делу — publish_judicial_precedent(content).\n"
        )
        context_block = (
            time_line
            + elder_channels_line
            + "Данные сервера: каналы (id, name, category_name, topic, viewable_by_roles, denied_for_roles) и роли с участниками. "
            "Перед рекомендацией канала проверь доступ обратившегося (его роли — см. блок «КОМУ ТЫ ОТВЕЧАЕШЬ»).\n"
            + _data + "\n\n---\n"
        )
        if memory_block:
            context_block = context_block + "\n" + memory_block + "\n\n---\n"
        current_user_content = (
            author_block
            + (f"Обращение №{case_id}. Сообщение: " if case_id is not None else ("Заявка на процедуру. Сообщение: " if pending_procedure_submission else "Сообщение: "))
            + content
        )
        # В начало контекста — закон, чтобы агент всегда опирался на него
        law_prefix = law_block + "\n\n---\n"

        messages_for_llm: list[dict[str, Any]] = []
        if history:
            messages_for_llm.extend(history)
        full_user_content = law_prefix + context_block + current_user_content
        messages_for_llm.append({"role": "user", "content": full_user_content})

        agent_ctx = self._agent_context(
            guild.id,
            extra={
                "current_case_id": case_id,
                "author_id": message.author.id,
                "author_display_name": author_name,
                "channel_id": channel_id,
                "thread_id": thread_id,
                "pending_procedure_submission": pending_procedure_submission,
            },
        )
        agent = self._build_agent(agent_ctx)

        # В Discord показываем «старейшина печатает» на время подготовки ответа
        async with message.channel.typing():
            # Сначала заготовленная фраза «думаю» только если сообщение длиннее порога (чтобы короткие типа «привет» получали сразу ответ без фразы)
            rcfg = self.config.role_config(self.role_key)
            max_len_for_thinking_phrase = 40
            if isinstance(rcfg, dict) and "thinking_only_over_chars" in rcfg:
                try:
                    max_len_for_thinking_phrase = int(rcfg["thinking_only_over_chars"])
                except (TypeError, ValueError):
                    pass
            if len(content) > max_len_for_thinking_phrase:
                thinking_phrases = DEFAULT_THINKING_PHRASES
                if isinstance(rcfg, dict) and rcfg.get("thinking_phrases"):
                    thinking_phrases = list(rcfg["thinking_phrases"])
                if thinking_phrases:
                    try:
                        await message.reply(random.choice(thinking_phrases))
                    except Exception as e:
                        logger.debug("Не удалось отправить фразу «думаю»: %s", e)

            try:
                reply, tools_called = await agent.run(messages_for_llm)
            except Exception as e:
                err_str = str(e).lower()
                tools_called = []
                if "429" in err_str or "rate_limit" in err_str or "tokens" in err_str and "limit" in err_str:
                    logger.warning("Лимит токенов/запросов API (429): %s", e)
                    reply = (
                        "Сейчас запрос получился слишком большим для лимита API (лимит по токенам). "
                        "Попробуй написать короче или подожди минуту и повтори."
                    )
                else:
                    logger.exception("Ошибка агента старейшины")
                    reply = f"Произошла ошибка при обработке обращения: {e!r}"

            raw_reply = (reply or "").strip()
            if not raw_reply:
                reply_clean = "Не удалось подготовить ответ. Повторите обращение или переформулируйте."
            else:
                reply_clean = raw_reply
            # Strip moral commentary from any reply
            _MORAL_MARKERS = (
                "дискриминац", "равенств", "гуманност", "гуманизм", "толерантн",
                "расизм", "расист", "ксенофоб", "достоинств", "права человек",
                "уважени", "нравственн", "этическ", "этичн", "моральн", "морал",
                "права несовершеннолетн", "защит прав", "насили", "порядочн",
                "рекомендуется проявлять", "помнить о правах", "нарушение норм",
                "безопасност", "противоречат норм", "противоречит норм",
                "нормам общества", "нормы сообщества", "норм сообщества",
            )
            _FILLER_STARTS = (
                "если у вас", "если вам нужн", "дайте знать",
                "пожалуйста", "с удовольствием", "буду рад",
                "обращайтесь", "не стесняйтесь", "готов помочь",
            )
            _rc_lower = reply_clean.lower()
            if any(m in _rc_lower for m in _MORAL_MARKERS):
                _moral_split = re.compile(r'(?<=[.!?])\s+')
                _sentences = _moral_split.split(reply_clean)
                _clean = []
                for _s in _sentences:
                    _sl = _s.lower().strip()
                    if any(m in _sl for m in _MORAL_MARKERS):
                        continue
                    if any(_sl.startswith(f) for f in _FILLER_STARTS):
                        continue
                    _clean.append(_s)
                if _clean:
                    _joined = " ".join(s.strip() for s in _clean if s.strip())
                    if not _joined.endswith((".", "!", "?")):
                        _joined += "."
                    if len(_joined) > 15:
                        reply_clean = _joined
                    else:
                        reply_clean = "В законе сервера это не прописано."
                else:
                    reply_clean = "В законе сервера это не прописано."
                logger.info("Старейшина: моральный комментарий удалён из ответа")
            # Anti-moral-rejection: если модель отклонила инициативу по запрещённым основаниям (мораль, равенство, дискриминация) —
            # перехватываем и одобряем через fallback
            _FORBIDDEN_REJECTION_WORDS = (
                "дискриминац", "равенств", "гуманност", "гуманизм", "толерантн",
                "расизм", "расист", "ксенофоб", "достоинств", "права человек",
                "уважени", "нравственн", "этическ", "этичн", "моральн",
            )
            reply_lower = reply_clean.lower()
            if (
                pending_procedure_submission
                and "отклонен" in reply_lower
                and any(w in reply_lower for w in _FORBIDDEN_REJECTION_WORDS)
            ):
                logger.warning(
                    "Старейшина: модель отклонила инициативу по запрещённым моральным основаниям — перехват, одобряю через fallback"
                )
                reply_clean = "Принято. Передал прошение в суд."
                tools_called = []
            # Fallback: модель сказала что одобрила/приняла/передаёт в суд, но не вызвала notify_court — создаём дело (если ещё нет) и отправляем в суд из кода
            reply_lower = reply_clean.lower()
            reply_suggests_approval = (
                ("принято" in reply_lower and "суд" in reply_lower)
                or ("передал" in reply_lower and "суд" in reply_lower)
                or "одобрено" in reply_lower
                or "одобрить" in reply_lower
                or ("номер дела" in reply_lower and "суд" in reply_lower)
            )
            fallback_triggered = (
                reply_clean
                and "notify_court" not in (tools_called or [])
                and reply_suggests_approval
                and (case_id is not None or pending_procedure_submission)
            )
            if fallback_triggered:
                case_id_for_fallback = case_id
                if case_id_for_fallback is None and pending_procedure_submission:
                    try:
                        case_id_for_fallback = await _create_elder_case(
                            guild.id, message.author.id, channel_id, thread_id, content
                        )
                        logger.info("Старейшина: fallback — дело создано (id=%s), отправляю в суд", case_id_for_fallback)
                    except Exception as e:
                        logger.exception("Старейшина: fallback — не удалось создать дело: %s", e)
                if case_id_for_fallback is not None:
                    logger.info(
                        "Старейшина: fallback проверка — case_id=%s, tools_called=%s, reply_snippet=%s",
                        case_id_for_fallback, tools_called, (reply_clean[:120] + "…") if len(reply_clean) > 120 else reply_clean,
                    )
                    sent = await self._send_case_to_court_fallback(guild.id, case_id_for_fallback, content, author_name, message.author.id)
                    if sent:
                        logger.info("Старейшина: fallback выполнен — дело №%s отправлено в суд и в канал решений", case_id_for_fallback)
                        case_id = case_id_for_fallback
                    else:
                        logger.warning("Старейшина: fallback не выполнен для дела №%s (дело уже в суде или другая причина)", case_id_for_fallback)
            context_marker = "КОНТЕКСТ:"
            if context_marker.upper() in reply_clean.upper():
                idx = reply_clean.upper().find(context_marker.upper())
                after_marker = reply_clean[idx + len(context_marker):]
                context_line = after_marker.split("\n")[0].strip() if after_marker else ""
                if context_line:
                    try:
                        await save_branch_summary(
                            self.role_key, guild.id, channel_id, thread_id, message.author.id,
                            context_line[:1500], case_id=case_id,
                        )
                        logger.debug("Старейшина сохранил контекст ветки: %s", context_line[:100])
                    except Exception as e:
                        logger.warning("Не удалось сохранить контекст ветки: %s", e)
                before_ctx = reply_clean[:idx].strip()
                after_ctx = after_marker.split("\n", 1)[-1].strip() if "\n" in after_marker else ""
                reply_clean = (before_ctx + ("\n" + after_ctx if after_ctx else "")).strip()
            # В inbox не постим текст для суда: призыв «проголосуйте» — только в канал суда (notify_court)
            if reply_clean and (
                "проголосуйте ответом на это сообщение" in reply_clean.lower()
                or "проголосуйте за или против" in reply_clean.lower()
            ):
                logger.warning("Старейшина: в ответе гражданину попал текст для суда — заменяем на короткое уведомление")
                reply_clean = (
                    "По процедуре обращение передаётся в суд. Оформлено и направлено в канал суда; судьи получат уведомление."
                )
                # Если модель не вызвала notify_court, но написала текст для суда — создать дело при необходимости и отправить в суд (fallback)
                if "notify_court" not in (tools_called or []):
                    cid = case_id
                    if cid is None and pending_procedure_submission:
                        try:
                            cid = await _create_elder_case(guild.id, message.author.id, channel_id, thread_id, content)
                        except Exception:
                            cid = None
                    if cid is not None:
                        sent = await self._send_case_to_court_fallback(
                            guild.id, cid, content, author_name, message.author.id
                        )
                        if sent:
                            logger.info("Старейшина: fallback отправка дела №%s в суд (модель вывела текст суда в ответ, но не вызвала notify_court)", cid)
            if reply_clean.upper().strip() == SKIP_REPLY_MARKER:
                # В inbox не игнорируем: на любое сообщение даём хотя бы короткий ответ
                if is_inbox:
                    reply_clean = (
                        "Чем могу помочь? Опиши запрос — помогу по процедуре и закону (референдум, законопроект, ст. 19, статус дела)."
                    )
                else:
                    reply_clean = ""
            elif reply_clean.upper().strip() == INSULT_MARKER:
                # Оскорбление — 50/50: фраза из списка или ответное сообщение с одним кастомным эмодзи сервера
                skip_phrases = DEFAULT_SKIP_REPLY_PHRASES
                rcfg_skip = self.config.role_config(self.role_key)
                if isinstance(rcfg_skip, dict) and rcfg_skip.get("skip_reply_phrases"):
                    skip_phrases = list(rcfg_skip["skip_reply_phrases"])
                emoji_msg = self._pick_random_server_emoji_message(guild) if guild else None
                use_phrase = (random.random() < 0.5 and skip_phrases) or not emoji_msg
                if use_phrase and skip_phrases:
                    to_send = random.choice(skip_phrases)
                    try:
                        await message.reply(to_send)
                    except Exception as e:
                        logger.debug("Не удалось отправить фразу (оскорбление): %s", e)
                    reply_clean = to_send
                elif emoji_msg:
                    try:
                        await message.reply(emoji_msg)
                        reply_clean = emoji_msg
                    except Exception as e:
                        logger.debug("Не удалось отправить эмодзи (оскорбление): %s", e)
                        reply_clean = ""
                else:
                    reply_clean = ""
            elif reply_clean:
                try:
                    await message.reply(reply_clean[:2000])
                except Exception as e:
                    logger.exception("Не удалось отправить ответ")
                    try:
                        await message.channel.send(reply_clean[:2000])
                    except Exception:
                        pass
        await save_message(
            role_key=self.role_key,
            guild_id=guild.id,
            channel_id=channel_id,
            thread_id=thread_id,
            case_id=case_id,
            discord_message_id=message.id,
            author_id=message.author.id,
            author_display_name=author_name,
            role="user",
            content=current_user_content,
        )
        if reply_clean:
            await save_message(
                role_key=self.role_key,
                guild_id=guild.id,
                channel_id=channel_id,
                thread_id=thread_id,
                case_id=case_id,
                discord_message_id=None,
                author_id=None,
                author_display_name=None,
                role="assistant",
                content=reply_clean,
            )

        await self.process_commands(message)

    async def _handle_oversight(self, message: Message) -> None:
        """Надзор за каналом: проверка легитимности, подсчёт голосов судей. В канал надзора текст не постим — только реакции. Ответы старейшины только в elder_inbox и elder_decisions; прерывания — в канал решений."""
        guild = message.guild
        if not guild:
            return
        content = (message.content or "").strip()
        if not content:
            return
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "?"
        channel_name = getattr(message.channel, "name", str(message.channel.id))
        author_id = message.author.id
        judge_role_id = self.config.role_ids().get("judge") or 0
        court_ch_id = self.config.channel_for_role(self.role_key, "notify_court")
        case = await self._get_first_pending_court_case(guild.id) if (court_ch_id and message.channel.id == court_ch_id) else None

        # Всегда получаем участника гильдии с актуальными ролями (fetch), чтобы надзор и проверка судьи опирались на одни данные
        member = guild.get_member(author_id)
        if member is None:
            try:
                member = await guild.fetch_member(author_id)
            except Exception:
                pass

        # В канале суда: если сообщение похоже на голос (за/против), а у автора нет роли судьи — сразу 👎. Проверяем только по реальным ролям (member.roles)
        if court_ch_id and message.channel.id == court_ch_id and case and judge_role_id:
            content_lower = content.lower()
            yes_m = ("да", "за", "одобряю", "одобрено", "согласен", "согласна", "поддерживаю", "yes", "+", "за.", "да.")
            no_m = ("нет", "против", "отклоняю", "не одобряю", "не поддерживаю", "no", "-", "против.", "нет.")
            looks_like_vote = len(content) <= 300 and (any(m in content_lower for m in yes_m) or any(m in content_lower for m in no_m))
            if looks_like_vote and member is not None:
                has_judge_role = any(getattr(r, "id", None) == judge_role_id for r in member.roles)
                if not has_judge_role:
                    try:
                        await message.add_reaction("👎")
                    except Exception:
                        pass
                    await self._log_court_event(
                        guild.id, message.channel.id, message.id, author_id,
                        "non_judge_vote", "Голосовать в суде вправе только судьи; у автора нет роли судьи.",
                        None, legitimacy="rejected",
                    )
                    return

        sent_at = getattr(case, "sent_to_court_at", None) if case else None
        vote_info = await _count_judge_votes_in_channel(
            self, message.channel.id, guild.id, judge_role_id, limit=20,
            case_id=case.id if case else None,
            sent_to_court_at=sent_at,
        )
        if court_ch_id and message.channel.id == court_ch_id and case and not (vote_info.get("two_approved") or vote_info.get("two_rejected")):
            await self._update_case_votes_from_channel(guild.id, case.id, vote_info)
        # Роли автора для надзора — только из участника гильдии (member), чтобы агент видел актуальный список
        author_block, _ = await get_author_roles_block_async(
            self, guild.id, message.author.id, author_name,
            member=member,
        )
        law_block = await get_law_block_async(
            self, guild.id, max_chars=6000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        court_report = await self._get_court_report_text(guild.id, limit=20)
        try:
            from src.core.discord_guild import get_guild_emojis_json
            emojis_json = get_guild_emojis_json(self, guild.id)
        except Exception:
            emojis_json = "[]"
        vote_line = (
            f"Голоса судей в этом канале (последние сообщения): проголосовало {vote_info['count']} судей. "
            + (f"Оба за: {vote_info['two_approved']}. Оба против: {vote_info['two_rejected']}." if vote_info["count"] == 2 else "")
        )
        ch_map = self.config.channels() if hasattr(self.config, "channels") and callable(getattr(self.config, "channels")) else {}
        court_decisions_id = ch_map.get("court_decisions")
        law_judicial_id = ch_map.get("law_judicial_precedents")
        council_inbox_id = ch_map.get("council_inbox")
        try:
            cd_id = int(court_decisions_id) if court_decisions_id is not None else None
            lj_id = int(law_judicial_id) if law_judicial_id is not None else None
            ci_id = int(council_inbox_id) if council_inbox_id is not None else None
        except (TypeError, ValueError):
            cd_id, lj_id, ci_id = None, None, None
        is_decisions_or_precedents_ch = (
            (cd_id is not None and message.channel.id == cd_id)
            or (lj_id is not None and message.channel.id == lj_id)
        )
        is_council_inbox_ch = ci_id is not None and message.channel.id == ci_id
        is_court_inbox_ch = court_ch_id is not None and message.channel.id == court_ch_id
        cases_sent_summary = ""
        judge_role_name_for_prompt = ""
        elder_role_name_for_prompt = ""
        if is_court_inbox_ch and judge_role_id and guild:
            judge_role = guild.get_role(int(judge_role_id))
            if judge_role and getattr(judge_role, "name", None):
                judge_role_name_for_prompt = f" На этом сервере роль судьи: «{judge_role.name}». В блоке «Автор» выше указаны роли автора — среди них должна быть эта роль."
        if is_decisions_or_precedents_ch:
            cases_sent_summary = await self._get_cases_sent_to_court_summary(guild.id, limit=20)
            judge_role_id = self.config.role_ids().get("judge")
            if judge_role_id and guild:
                judge_role = guild.get_role(int(judge_role_id))
                if judge_role and getattr(judge_role, "name", None):
                    judge_role_name_for_prompt = f" На этом сервере роль судьи называется: «{judge_role.name}». В блоке «Автор» выше перечислены роли автора — среди них должна быть эта роль."
        council_cases_summary = ""
        if is_council_inbox_ch:
            council_cases_summary = await self._get_cases_sent_to_council_summary(guild.id, limit=20)
            elder_role_id = self.config.role_ids().get("elder")
            if elder_role_id and guild:
                elder_role = guild.get_role(int(elder_role_id))
                if elder_role and getattr(elder_role, "name", None):
                    elder_role_name_for_prompt = f" На этом сервере роль старейшины называется: «{elder_role.name}». В блоке «Автор» выше перечислены роли автора — среди них должна быть эта роль."
        oversight_rules = (
            "Правила ответа (строго):\n"
            "1) Текстовые ответы старейшина постит ТОЛЬКО в канал обращений (inbox) и канал решений (decisions). В этот канал надзора текст НЕ постим.\n"
            "2) Если действие легитимно — ответь ровно: ЛЕГИТИМНО.\n"
            "3) Если легитимно и ровно двое судей проголосовали (оба за или оба против) — поставь реакцию на это сообщение: ответь REACT:имя_эмодзи (из списка эмодзи сервера выше, например REACT:thumbs_up при одобрении или подходящий эмодзи при отклонении).\n"
            "4) Если нелегитимно — ответь INTERRUPT: и далее один короткий текст. На сообщение будет поставлена только реакция 👎; дело при этом не бракуется и не прерывается — бракуется дело только когда двое судей проголосовали «против» (решение суда).\n"
            "5) **Проверка роли — только по блоку «Автор».** В блоке «Автор» выше перечислены роли автора с сервера (актуальные). Если среди них указана роль судьи (то же название, что «роль судьи» в правилах ниже) — голос/решение легитимны, ответь ЛЕГИТИМНО. INTERRUPT про «нет роли судьи» — только если в блоке «Автор» этой роли действительно нет. Не делай INTERRUPT по предположению."
        )
        if is_decisions_or_precedents_ch:
            oversight_rules += (
                "\n\n6) **Канал «решения суда» или «судебные прецеденты»** — дополнительные проверки:\n"
                "   (a) **Роль судьи:** выносить решение или формировать прецедент вправе ТОЛЬКО участник с ролью судьи."
                + judge_role_name_for_prompt
                + " В блоке «Автор» выше указаны роли автора. Если у автора НЕТ роли судьи — ответь INTERRUPT: только судьи вправе выносить решения суда и формировать судебные прецеденты.\n"
                "   (b) **Реальное дело:** решение или прецедент должны относиться к делу, по которому запрос ПОСТУПИЛ и был передан в суд. Список таких дел — ниже. Если в сообщении упоминается «дело №N» или по смыслу это решение/прецедент по конкретному делу — id этого дела должен быть в списке. Суд не вправе выносить решение или прецедент «из головы», без поступившего запроса. Если дела в списке нет или запрос не поступал — ответь INTERRUPT: решение/прецедент по несуществующему запросу недопустимо."
            )
        if is_court_inbox_ch:
            oversight_rules += (
                "\n\n6) **Канал суда (court_inbox)** — здесь судьи голосуют по делам (за/против). Голосовать вправе ТОЛЬКО участники с ролью судьи."
                + judge_role_name_for_prompt
                + " Если сообщение похоже на голос (за, против, да, нет и т.п.) и у автора НЕТ роли судьи — ответь INTERRUPT: голосовать в суде вправе только судьи; у автора нет роли судьи. Не одобряй такое сообщение и не считай его голосом суда."
            )
        if is_council_inbox_ch:
            oversight_rules += (
                "\n\n7) **Канал «указы на исполнение в совет» (council_inbox)** — дополнительные проверки:\n"
                "   (a) **Роль старейшины:** направлять указ на исполнение в совет вправе ТОЛЬКО участник с ролью старейшины (elder)."
                + elder_role_name_for_prompt
                + " В блоке «Автор» выше указаны роли автора. Если у автора НЕТ роли старейшины — ответь INTERRUPT: только старейшины вправе направлять указы на исполнение в совет.\n"
                "   (b) **Реальное дело:** указ должен относиться к делу, которое переданo на исполнение в совет (решение старейшин send_to_council или решение суда). Список таких дел — ниже. Если в сообщении упоминается «дело №N» или по смыслу это указ по конкретному делу — id этого дела должен быть в списке. Указ по несуществующему или не переданному в совет делу — нелегитимен. Если дела в списке нет — ответь INTERRUPT: указ по несуществующему делу недопустим."
            )
        oversight_user = (
            law_block + "\n\n---\n\n"
            + "[ РЕЖИМ НАДЗОРА — ТОЛЬКО РЕАКЦИИ, БЕЗ ТЕКСТА В ЭТОТ КАНАЛ ]\n\n"
            + f"Канал: {channel_name} (id={message.channel.id}). Сообщение id: {message.id}. Автор: {author_name} (id={author_id}).\n"
            + author_block
            + f"\nТекст сообщения: {content[:1500]}\n\n"
            + f"Отчёт по судейским каналам (последние события):\n{court_report}\n\n"
            + f"{vote_line}\n\n"
        )
        if cases_sent_summary:
            oversight_user += f"{cases_sent_summary}\n\n"
        if council_cases_summary:
            oversight_user += f"{council_cases_summary}\n\n"
        oversight_user += (
            "Эмодзи сервера (для реакций, используй name): " + (emojis_json[:2000] if isinstance(emojis_json, str) else "") + "\n\n"
            + oversight_rules
            + "\n\n**НЕ ВЫЗЫВАЙ ИНСТРУМЕНТЫ.** Все данные для решения уже в тексте выше (закон, автор, голоса судей, отчёт, списки дел). Ответь ровно одной строкой: ЛЕГИТИМНО или REACT:имя_эмодзи или INTERRUPT:краткий_текст."
        )
        agent_ctx = self._agent_context(guild.id, extra={"author_id": author_id, "oversight_message_id": message.id, "oversight_channel_id": message.channel.id})
        agent = self._build_agent(agent_ctx)
        try:
            reply, _ = await agent.run([{"role": "user", "content": oversight_user}])
        except Exception as e:
            logger.exception("Ошибка агента надзора старейшины")
            await self._log_court_event(guild.id, message.channel.id, message.id, author_id, "oversight_error", str(e), None)
            return
        reply_clean = (reply or "").strip()
        logger.info(
            "Надзор: голоса судей count=%s two_approved=%s two_rejected=%s reply=%s",
            vote_info.get("count", 0), vote_info.get("two_approved"), vote_info.get("two_rejected"),
            reply_clean[:80] if reply_clean else "",
        )
        if reply_clean.upper() == LEGITIMATE_MARKER:
            vote_summary = _build_judge_vote_summary(guild, vote_info)
            if vote_info.get("two_approved") or vote_info.get("two_rejected"):
                emoji = self._pick_random_emoji_for_reaction(guild)
                try:
                    await message.add_reaction(emoji)
                    logger.info("Старейшина: реакция %s на сообщение %s (двое судей)", emoji, message.id)
                except Exception as e:
                    logger.warning("Реакция %s не удалась (сообщение %s): %s. Проверь право бота «Add Reactions» в канале.", emoji, message.id, e)
                case = await self._get_first_pending_court_case(guild.id)
                if case:
                    await self._record_court_decision_and_inform(
                        guild, case.id,
                        approved=vote_info.get("two_approved"),
                        vote_summary=vote_summary,
                        vote_info=vote_info,
                    )
            elif vote_info.get("count") == 2 and not vote_info.get("two_approved") and not vote_info.get("two_rejected"):
                case = await self._get_first_pending_court_case(guild.id)
                if case:
                    await self._return_case_to_elder(
                        case.id,
                        f"Судьи разошлись во мнениях. {vote_summary}",
                        guild.id,
                    )
            await self._log_court_event(
                guild.id, message.channel.id, message.id, author_id,
                "judge_vote" if (vote_info.get("count", 0) >= 1 and (vote_info.get("two_approved") or vote_info.get("two_rejected"))) else "court_message",
                vote_summary if vote_info.get("count", 0) >= 1 else (content[:400] if content else "(сообщение в канале надзора)"),
                vote_info if vote_info.get("count", 0) >= 1 else None,
                legitimacy="approved",
            )
            return
        if not reply_clean:
            return
        if REACT_PREFIX.upper() in reply_clean.upper():
            idx = reply_clean.upper().find(REACT_PREFIX.upper())
            suffix = reply_clean[idx + len(REACT_PREFIX):].strip()
            emoji_name = suffix.split()[0] if suffix else "✅"
            try:
                await self._add_reaction_to_message(message, emoji_name)
                logger.info("Старейшина поставил реакцию %s на сообщение %s", emoji_name, message.id)
            except Exception as e:
                logger.exception("Не удалось поставить реакцию '%s' на сообщение %s: %s", emoji_name, message.id, e)
            vote_summary_react = _build_judge_vote_summary(guild, vote_info)
            await self._log_court_event(
                guild.id, message.channel.id, message.id, author_id,
                "judge_vote_two" if (vote_info.get("two_approved") or vote_info.get("two_rejected")) else "reaction",
                f"{vote_summary_react}; реакция {emoji_name}",
                vote_info,
                legitimacy="approved",
            )
            if vote_info.get("two_approved") or vote_info.get("two_rejected"):
                case = await self._get_first_pending_court_case(guild.id)
                if case:
                    await self._record_court_decision_and_inform(
                        guild, case.id,
                        approved=vote_info.get("two_approved"),
                        vote_summary=vote_summary_react,
                        vote_info=vote_info,
                    )
            return
        if reply_clean.upper().startswith(INTERRUPT_PREFIX):
            interrupt_text = reply_clean[len(INTERRUPT_PREFIX):].strip()[:2000]
            # Только 👎 на сообщение и лог надзора. Дело не бракуем: брак дела (court_result=rejected) — только когда двое судей проголосовали «против»; здесь лишь отметка нелегитимности сообщения, дело идёт в суде дальше.
            try:
                await message.add_reaction("👎")
            except Exception:
                pass
            event_type = "non_judge_vote" if (court_ch_id and message.channel.id == court_ch_id) else "interrupt"
            await self._log_court_event(
                guild.id, message.channel.id, message.id, author_id,
                event_type, interrupt_text[:500], None, legitimacy="rejected",
            )
            return
        if reply_clean.upper().startswith("ОТВЕТ:"):
            to_send = reply_clean[6:].strip()[:2000]
            if to_send:
                ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
                if ch_decisions:
                    ch = self.get_channel(ch_decisions)
                    if ch:
                        try:
                            await ch.send(f"По каналу **{channel_name}** (обращение от <@{author_id}>):\n{to_send}")
                        except Exception as e:
                            logger.exception("Ответ старейшины в канал решений: %s", e)
            return

    async def _get_court_report_text(self, guild_id: int, limit: int = 20) -> str:
        """Последние записи отчёта по судейским каналам для контекста надзора."""
        from sqlalchemy import select
        async with get_db() as session:
            result = await session.execute(
                select(ElderCourtLog)
                .where(ElderCourtLog.guild_id == guild_id)
                .order_by(ElderCourtLog.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        if not rows:
            return "Событий пока нет."
        lines = []
        for r in reversed(rows):
            leg = getattr(r, "legitimacy", None) or "pending"
            lines.append(f"{r.created_at} | {r.event_type} | legitimacy={leg} | {r.summary or ''}")
        return "\n".join(lines)

    async def _log_court_event(
        self, guild_id: int, channel_id: int, message_id: int | None, author_id: int | None,
        event_type: str, summary: str | None, meta: dict | None,
        legitimacy: str | None = None,
    ) -> None:
        """Добавить запись в отчёт по судейским каналам. Если передан message_id и legitimacy — обновить существующую запись по (guild_id, channel_id, message_id) или создать новую с легитимностью (approved/rejected)."""
        import json
        now = datetime.now(timezone.utc)
        async with get_db() as session:
            if message_id is not None and legitimacy:
                result = await session.execute(
                    select(ElderCourtLog).where(
                        ElderCourtLog.guild_id == guild_id,
                        ElderCourtLog.channel_id == channel_id,
                        ElderCourtLog.message_id == message_id,
                    ).limit(1)
                )
                existing = result.scalars().first()
                if existing:
                    existing.legitimacy = legitimacy
                    existing.legitimacy_at = now
                    if event_type:
                        existing.event_type = event_type
                    if summary is not None:
                        existing.summary = summary
                    if meta is not None:
                        existing.meta = json.dumps(meta, ensure_ascii=False)
                    return
            entry = ElderCourtLog(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                author_id=author_id,
                event_type=event_type,
                summary=summary,
                meta=json.dumps(meta, ensure_ascii=False) if meta else None,
                legitimacy=legitimacy if legitimacy else None,
                legitimacy_at=now if legitimacy else None,
            )
            session.add(entry)

    def _pick_random_emoji_for_reaction(self, guild: Any) -> Any:
        """Случайное эмодзи для реакции: из эмодзи сервера или Unicode-запас. Возвращает то, что принимает message.add_reaction()."""
        if guild and getattr(guild, "emojis", None):
            use = [em for em in guild.emojis if getattr(em, "name", None)]
            if use:
                return random.choice(use)
        return random.choice(("✅", "👎", "👍", "❌", "⬆️", "🔴", "🟢", "⚪"))

    def _pick_random_server_emoji_message(self, guild: Any) -> str | None:
        """Случайное кастомное эмодзи сервера как строка для ответа (<:name:id>). None если нет кастомных."""
        if not guild or not getattr(guild, "emojis", None):
            return None
        use = [em for em in guild.emojis if getattr(em, "name", None)]
        if not use:
            return None
        return str(random.choice(use))

    async def on_raw_reaction_add(self, payload: Any) -> None:
        """Если судья ставит реакцию на сообщение по делу после истечения срока суда — ставим на это сообщение ⏰.
        Проверяем, что реагирует именно судья; на реакции не-судей не отвечаем."""
        guild_id = getattr(payload, "guild_id", None)
        channel_id = getattr(payload, "channel_id", None)
        message_id = getattr(payload, "message_id", None)
        user_id = getattr(payload, "user_id", None)
        if not guild_id or not channel_id or not message_id or not user_id:
            return
        guild = self.get_guild(guild_id)
        if not guild:
            return
        # Не реагируем на собственные реакции бота
        if user_id == self.user.id:
            return
        judge_role_id = self.config.role_ids().get("judge") or 0
        if not judge_role_id:
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return
        if not member or getattr(member, "bot", False):
            return
        if not any(getattr(r, "id", None) == judge_role_id for r in member.roles):
            return
        ch_map = self.config.channels() if hasattr(self.config, "channels") and callable(getattr(self.config, "channels")) else {}
        court_inbox_id = ch_map.get("court_inbox") or self.config.channel_for_role(self.role_key, "notify_court")
        court_decisions_id = ch_map.get("court_decisions")
        court_ids: set[int] = set()
        try:
            if court_inbox_id:
                court_ids.add(int(court_inbox_id))
        except (TypeError, ValueError):
            pass
        try:
            if court_decisions_id:
                court_ids.add(int(court_decisions_id))
        except (TypeError, ValueError):
            pass
        if channel_id not in court_ids:
            return
        # Есть ли по гильдии дело, по которому срок суда уже истёк и решения суда нет
        from sqlalchemy import select
        async with get_db() as session:
            result = await session.execute(
                select(ElderCase).where(
                    ElderCase.guild_id == guild_id,
                    ElderCase.sent_to_court_at.isnot(None),
                    ElderCase.court_decided_at.is_(None),
                    ElderCase.court_deadline_expired_at.isnot(None),
                ).order_by(ElderCase.sent_to_court_at.asc()).limit(1)
            )
            case = result.scalars().first()
        if not case:
            return
        channel = self.get_channel(channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(message_id)
        except Exception:
            return
        try:
            await message.add_reaction("⏰")
        except Exception:
            logger.debug("Старейшина: не удалось добавить реакцию ⏰ на сообщение %s", message_id)

    async def _add_reaction_to_message(self, message: Message, emoji_name: str) -> None:
        """Поставить реакцию на сообщение: Unicode (✅, 👎) или имя эмодзи сервера (без учёта регистра)."""
        raw = (emoji_name or "").strip() or "✅"
        guild = message.guild
        if guild and raw not in ("✅", "👎", "👍", "❌"):
            raw_lower = raw.lower()
            for em in guild.emojis:
                if em.name and em.name.lower() == raw_lower:
                    try:
                        await message.add_reaction(em)
                        return
                    except Exception as e:
                        logger.warning("Реакция кастомным эмодзи %s не удалась: %s", em.name, e)
                    break
        try:
            await message.add_reaction(raw)
        except Exception as e:
            logger.warning("Реакция '%s' не удалась: %s", raw, e)
            if raw not in ("✅", "👎"):
                try:
                    await message.add_reaction("✅")
                except Exception as e2:
                    logger.warning("Не удалось поставить ✅: %s", e2)


def create_elder_bot(deps: RoleDeps) -> RoleBot:
    return ElderBot(deps=deps)
