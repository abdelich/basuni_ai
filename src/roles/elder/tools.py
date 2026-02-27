"""
Инструменты агента «Старейшина»: каналы и роли сервера, отправка в любой канал по ID, БД.
Агент сам решает, куда и что писать, на основе get_channels и get_roles_and_members.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select, update  # type: ignore[reportMissingImports]

from src.core.tools import Tool, build_parameters
from src.core.agent_ctx import AgentContext
from src.core.models import ElderCase, ElderCourtLog, ElderCaseCourtVote
from src.core.discord_guild import (
    get_guild_channels_json,
    get_guild_roles_and_members_json,
    get_guild_emojis_json,
    get_member_roles_json_async,
    get_channel_content_async,
    get_channels_where_category_contains,
    get_all_reference_channel_contents_async,
)

logger = logging.getLogger("basuni.elder.tools")


def _case_display_number(case: Any) -> int:
    """Номер дела для показа пользователю («дело №N»). guild_case_number или id."""
    n = getattr(case, "guild_case_number", None)
    return n if n is not None else case.id


def _deadline_from_case(case: Any) -> timedelta:
    """По делу возвращает срок суда как timedelta (минуты или часы из БД)."""
    minutes = getattr(case, "court_deadline_minutes", None)
    if minutes is not None and minutes > 0:
        return timedelta(minutes=int(minutes))
    hours = getattr(case, "court_deadline_hours", None) or 24
    return timedelta(hours=int(hours))


def _court_deadline_info(
    sent_at: datetime | None,
    deadline: timedelta | float | int,
    court_deadline_expired_at: datetime | None = None,
) -> dict[str, Any]:
    """Срок по делу суда. deadline — timedelta или число часов (int/float). Если в БД court_deadline_expired_at — считаем срок истёкшим. Иначе по sent_at + deadline (UTC)."""
    if not isinstance(deadline, timedelta):
        deadline = timedelta(hours=float(deadline))
    now = datetime.now(timezone.utc)
    if not sent_at:
        return {
            "court_deadline_at": None,
            "court_deadline_passed": False,
            "court_deadline_status_label": "срок не начат",
            "court_time_remaining_seconds": None,
            "court_time_remaining_text": "срок не начат (дело не передано в суд)",
            "expired_ru": "Срок истёк: нет (дело не в суде)",
        }
    # Приоритет: запись в БД «срок истёк» — модель всегда видит это из базы
    if court_deadline_expired_at is not None:
        passed = True
        deadline_at = (sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at) + deadline
        return {
            "court_deadline_at": deadline_at.isoformat(),
            "court_deadline_passed": True,
            "court_deadline_status_label": "СРОК ИСТЁК",
            "court_time_remaining_seconds": 0,
            "court_time_remaining_text": "срок истёк (зафиксировано в БД)",
            "expired_ru": "Срок истёк: да",
        }
    sent_utc = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
    deadline_at = sent_utc + deadline
    delta = deadline_at - now
    passed = delta.total_seconds() <= 0
    secs = int(delta.total_seconds())
    if passed:
        ago_secs = -secs
        if ago_secs >= 86400:
            text = f"срок истёк {ago_secs // 86400} дн. назад"
        elif ago_secs >= 3600:
            text = f"срок истёк {ago_secs // 3600} ч назад"
        elif ago_secs >= 60:
            text = f"срок истёк {ago_secs // 60} мин назад"
        else:
            text = "срок истёк"
    else:
        if secs >= 86400:
            text = f"осталось {secs // 86400} дн. {(secs % 86400) // 3600} ч"
        elif secs >= 3600:
            text = f"осталось {secs // 3600} ч {(secs % 3600) // 60} мин"
        elif secs >= 60:
            text = f"осталось {secs // 60} мин"
        else:
            text = "осталось менее минуты"
    status_label = "СРОК ИСТЁК" if passed else "ожидание (срок не истёк)"
    expired_ru = "Срок истёк: да" if passed else "Срок истёк: нет"
    return {
        "court_deadline_at": deadline_at.isoformat(),
        "court_deadline_passed": passed,
        "court_deadline_status_label": status_label,
        "court_time_remaining_seconds": secs,
        "court_time_remaining_text": text,
        "expired_ru": expired_ru,
    }


def _strip_court_boilerplate_for_council(text: str) -> str:
    """Убирает из текста дела фразы для суда: призыв голосовать, упоминания срока. Совету нужна только суть обращения."""
    if not (text or "").strip():
        return ""
    t = text.strip()
    # Удалить призыв голосовать (для судей)
    for phrase in (
        "Проголосуйте ответом на это сообщение: за или против.",
        "Проголосуйте ответом на это сообщение или на исходное по делу: за или против.",
        "проголосуйте ответом на это сообщение: за или против.",
        "проголосуйте за или против.",
    ):
        t = re.sub(re.escape(phrase), "", t, flags=re.IGNORECASE)
    # Удалить предложения про срок (суд/голосование) — совету не нужны
    t = re.sub(
        r"(?m)^.*[Сс]рок\s+(ещё\s+)?(не\s+)?истёк[^.\n]*\.?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"(?m)^.*[Сс]рок\s+для\s+голосования[^.\n]*\.?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return t.strip()


def _mentions_for_role(bot: Any, guild_id: int, role_config_key: str) -> str:
    """Строка упоминаний всех участников с данной ролью: <@id1> <@id2> ..."""
    guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else None
    if not guild or not hasattr(bot, "config") or not bot.config:
        return ""
    role_ids = getattr(bot.config, "role_ids", None)
    if not callable(role_ids):
        return ""
    rid = role_ids().get(role_config_key)
    if not rid:
        return ""
    role = guild.get_role(int(rid))
    if not role or not getattr(role, "members", None):
        return ""
    return " ".join(f"<@{m.id}>" for m in role.members if not getattr(m, "bot", False))


def make_elder_tools(ctx: AgentContext) -> list[Tool]:
    """Создаёт список инструментов старейшины с привязкой к контексту (бот, гильдия, БД)."""

    async def get_channels() -> str:
        """Все каналы: id, name, category_name, topic, viewable_by_roles, denied_for_roles. Не рекомендуй канал, если у обратившегося нет доступа (сравни с ролями обратившегося из контекста)."""
        return get_guild_channels_json(ctx.bot, ctx.guild_id)

    async def get_channels_in_category(category_substring: str) -> str:
        """Каналы по подстроке в названии категории (например «право»). В ответе есть viewable_by_roles и denied_for_roles — не рекомендуй канал, к которому у обратившегося нет доступа."""
        return get_channels_where_category_contains(ctx.bot, ctx.guild_id, category_substring)

    async def get_reference_channels() -> str:
        """Каналы категории «право» (правила, прецеденты, закон) с viewable_by_roles и denied_for_roles. Дальше get_channel_content(id) по нужным id; в ответах ссылайся на закон."""
        sub = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "reference_category_name", None) or "право"
        return get_channels_where_category_contains(ctx.bot, ctx.guild_id, sub)

    async def get_roles_and_members() -> str:
        """Получить все роли сервера и участников в каждой роли: id, name, members (id, display_name). Чтобы знать, кто судья, кто с ПМЖ, кто в совете и т.д."""
        return get_guild_roles_and_members_json(ctx.bot, ctx.guild_id)

    async def get_member_roles(member_query: str) -> str:
        """Найти участника по никнейму, имени или Discord ID и получить его роли. Для вопроса «какие у меня роли?» передай «me» или id автора из контекста (блок «КОМУ ТЫ ОТВЕЧАЕШЬ»). Возвращает id, display_name, name, roles."""
        q = (member_query or "").strip().lower()
        if q in ("me", "я", "автор", "author", "себя"):
            author_id = ctx.extra.get("author_id")
            if author_id is not None:
                return await get_member_roles_json_async(ctx.bot, ctx.guild_id, str(author_id))
        return await get_member_roles_json_async(ctx.bot, ctx.guild_id, member_query)

    async def get_all_law_channel_contents(category_substring: str = "право", limit_per_channel: int = 80) -> str:
        """Получить содержимое каналов категории «право» как один документ (статьи и части в соседних абзацах). Используй для поиска «статья N часть M» — отвечай только по этому тексту."""
        sub = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "reference_category_name", None) or category_substring
        return await get_all_reference_channel_contents_async(
            ctx.bot, ctx.guild_id, sub, min(limit_per_channel, 120), as_law_document=True
        )

    async def get_channel_content(channel_id: int, limit: int = 40) -> str:
        """Прочитать содержимое канала (закреплённые и последние сообщения). channel_id бери из get_channels или get_reference_channels — заходи в те каналы, которые тебе нужны по смыслу (прецеденты, закон)."""
        return await get_channel_content_async(ctx.bot, int(channel_id), limit=min(limit, 50))

    async def get_court_inbox_recent(limit: int = 25) -> str:
        """Прочитать последние сообщения из канала суда (court_inbox). Для референдума: решением суда считаются только сообщения от пользователей с ролью судьи (проверяй по get_roles_and_members / блоку «Роли и участники»). Два судьи должны дать «да» — тогда суд одобрил. Сообщение от человека без роли судьи не считается решением суда."""
        ch_id = ctx.get_channel_id("notify_court")
        if not ch_id:
            return "Канал суда не настроен (notify_court)."
        return await get_channel_content_async(ctx.bot, ch_id, limit=min(limit, 50))

    async def get_council_inbox_recent(limit: int = 25) -> str:
        """Прочитать последние сообщения из канала совета (council_inbox). Вызывай обязательно, когда спрашивают «что решил совет?», «одобрил ли совет?» — отвечай только на основе этого содержимого. Если сообщений нет или нет решения по делу — говори «от совета ответа пока не поступало»."""
        ch_id = ctx.get_channel_id("notify_council")
        if not ch_id:
            return "Канал совета не настроен (notify_council)."
        return await get_channel_content_async(ctx.bot, ch_id, limit=min(limit, 50))

    async def send_message_to_channel(channel_id: int, content: str) -> str:
        """Отправить сообщение в канал по его ID (id бери из get_channels или из блока «Каналы старейшин» в контексте)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал с ID {channel_id} не найден."
        try:
            text = content[:2000]
            await channel.send(text)
            logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), text[:150].replace("\n", " "))
            return "Сообщение отправлено."
        except Exception as e:
            logger.exception("send_message_to_channel")
            return f"Ошибка отправки: {e!r}"

    async def publish_rejection_to_decisions(reasoning: str) -> str:
        """Опубликовать в канал решений (elder_decisions) решение об отклонении обращения. Вызывай при отклонении заявки (когда не создаёшь дело): в канал решений уйдёт «По обращению: Отклонено. По причине: …». Старейшина всегда публикует решение в elder_decisions — при отклонении используй этот вызов."""
        ch_id = ctx.get_channel_id("decisions")
        if not ch_id and getattr(ctx.bot, "config", None) and getattr(ctx.bot, "role_key", None):
            ch_id = ctx.bot.config.channel_for_role(ctx.bot.role_key, "decisions")
        if not ch_id:
            return "Канал решений (decisions) не настроен. Укажи в конфиге roles.elder.decisions_channel_key и channels.elder_decisions."
        channel = ctx.bot.get_channel(int(ch_id))
        if not channel:
            return f"Канал решений {ch_id} не найден."
        reason = (reasoning or "").strip() or "По закону оснований для одобрения нет."
        text = f"**По обращению:** Принято решение: **отклонено** (referendum_rejected). По причине: {reason}"
        try:
            await channel.send(text[:2000])
            logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), text[:150].replace("\n", " "))
            return "Решение об отклонении опубликовано в канал решений (elder_decisions)."
        except Exception as e:
            logger.exception("publish_rejection_to_decisions")
            return f"Ошибка отправки в канал решений: {e!r}"

    async def create_elder_case(content: str) -> str:
        """Создать дело старейшины (при одобрении заявки). Вызывай только когда решил одобрить обращение и передать его в суд. Делу присваивается номер; этот номер (case_id) используй в publish_decision, notify_court, record_case_sent_to_court. content — **полный текст обращения пользователя целиком**: все абзацы, все пункты (1), 2)...), положения закона, обоснования — без сокращений и пересказа. Этот текст сохраняется как initial_content и при передаче дела в совет (notify_council) уходит совету именно он; если передать краткую «суть», совет получит неполную информацию."""
        author_id = ctx.extra.get("author_id")
        channel_id = ctx.extra.get("channel_id")
        thread_id = ctx.extra.get("thread_id")
        if author_id is None or channel_id is None:
            return "Ошибка: в контексте нет author_id или channel_id. create_elder_case вызывается только при обработке заявки гражданина в канале обращений."
        try:
            cid = await ctx.bot.create_elder_case(
                ctx.guild_id,
                int(author_id),
                int(channel_id),
                int(thread_id) if thread_id is not None else None,
                (content or "").strip() or "(суть не указана)",
            )
            return f"Дело создано. case_id={cid}. Дальше **обязательно в этом порядке**: (1) publish_decision(case_id=\"{cid}\", decision=\"referendum_approved\", reasoning=\"причина по закону, напр. ст. 19\") — чтобы решение появилось в канале решений старейшин; (2) notify_court(текст с Дело №{cid} и сутью); (3) record_case_sent_to_court(case_id=\"{cid}\", content_sent=тот же текст). Без publish_decision решение не считается принятым и не отображается в канале решений."
        except Exception as e:
            logger.exception("create_elder_case")
            return f"Ошибка создания дела: {e!r}"

    async def notify_court(content: str) -> str:
        """Уведомить суд: отправить сообщение в канал суда (court_inbox), с упоминанием судей (@). В тексте обязательно: номер дела (Дело №X — подставляй реальный номер из контекста «Текущее обращение по делу №X», никогда не пиши букву N), суть; в конце: «Проголосуйте ответом на это сообщение: за или против.» После вызова обязательно record_case_sent_to_court(case_id, content_sent=этот_текст). Не вызывай notify_court, если по этому делу уже вызван publish_decision(referendum_rejected)."""
        current_cid = ctx.extra.get("current_case_id")
        display_no: int | None = None
        if current_cid is not None:
            try:
                cid = int(current_cid)
            except (TypeError, ValueError):
                cid = None
            if cid is not None:
                async with ctx.db_session_factory() as session:
                    res = await session.execute(select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id))
                    cur_case = res.scalars().one_or_none()
                if cur_case and getattr(cur_case, "elder_decision", None) == "referendum_rejected":
                    return "Дело по текущему обращению уже отклонено (referendum_rejected). В суд не отправляю. Не вызывай notify_court после отклонения."
                if cur_case and getattr(cur_case, "sent_to_court_at", None) is not None:
                    return "Дело уже передано в суд в этом ответе; повторная отправка не выполняется."
                if cur_case:
                    display_no = _case_display_number(cur_case)
                elif cid is not None:
                    # Дело не найдено в этой сессии — подставляем хотя бы id, чтобы никогда не отправлять букву N
                    display_no = cid
        # Если в тексте есть «Дело №N», а номера нет в контексте — берём последнее открытое дело гильдии (ещё не в суде)
        text_to_send = (content or "").strip()
        if display_no is None and re.search(r"№\s*N\b|Дело\s*№\s*N\b", text_to_send, re.IGNORECASE):
            async with ctx.db_session_factory() as session:
                res = await session.execute(
                    select(ElderCase)
                    .where(
                        and_(
                            ElderCase.guild_id == ctx.guild_id,
                            ElderCase.status == "open",
                            ElderCase.sent_to_court_at.is_(None),
                        )
                    )
                    .order_by(ElderCase.id.desc())
                    .limit(1)
                )
                fallback_case = res.scalars().one_or_none()
            if fallback_case:
                display_no = _case_display_number(fallback_case)
                logger.warning(
                    "notify_court: current_case_id отсутствует в контексте (extra), подставляю номер из последнего открытого дела id=%s display_no=%s",
                    fallback_case.id, display_no,
                )
        logger.info(
            "notify_court: current_cid=%s guild_id=%s display_no=%s",
            current_cid, ctx.guild_id, display_no,
        )
        # Подстановка реального номера дела вместо «Дело №N» / «дело №n» — всегда, если есть display_no или хотя бы fallback
        if display_no is not None:
            for pattern in (
                re.compile(r"Дело\s*№\s*N\b", re.IGNORECASE),
                re.compile(r"дело\s*№\s*n\b", re.IGNORECASE),
                re.compile(r"Дело\s*№N\b", re.IGNORECASE),
                re.compile(r"дело\s*#\s*N\b", re.IGNORECASE),
            ):
                text_to_send = pattern.sub(f"Дело №{display_no}", text_to_send)
            # Любой оставшийся «№N» / «№ N» (напр. после «Дело »)
            if re.search(r"№\s*N\b", text_to_send, re.IGNORECASE):
                logger.warning("notify_court: в тексте осталось «№N», подставляю display_no=%s", display_no)
                text_to_send = re.sub(r"№\s*N\b", f"№{display_no}", text_to_send, flags=re.IGNORECASE)
        # Финальная проверка: если буква N всё ещё в контексте номера дела — заменяем (любой формат)
        if re.search(r"Дело\s*[№#]\s*N\b", text_to_send, re.IGNORECASE) and display_no is not None:
            text_to_send = re.sub(r"(Дело\s*[№#]\s*)N\b", rf"\g<1>{display_no}", text_to_send, flags=re.IGNORECASE)
        ch_id = ctx.get_channel_id("notify_court")
        if not ch_id:
            return "В конфиге не задан канал суда (notify_court). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал суда {ch_id} не найден."
        mentions = _mentions_for_role(ctx.bot, ctx.guild_id, "judge")
        full = (f"{mentions}\n\n{text_to_send}" if mentions else text_to_send).strip()[:2000]
        try:
            await channel.send(full)
            logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), full[:200].replace("\n", " "))
        except Exception as e:
            logger.exception("notify_court")
            return f"Ошибка отправки в суд: {e!r}"
        # Сразу фиксируем в БД: отсчёт срока суда (старейшина обязан отслеживать и вернуть дело при истечении)
        if current_cid is not None:
            try:
                cid = int(current_cid)
            except (TypeError, ValueError):
                cid = None
            if cid is not None:
                try:
                    deadline_hours = 24.0
                    try:
                        rcfg = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "role_config", None)
                        if callable(rcfg):
                            rcfg = rcfg("elder")
                        if isinstance(rcfg, dict):
                            val = rcfg.get("court_deadline_hours", 24)
                            deadline_hours = float(val) if val is not None else 24.0
                            if deadline_hours <= 0:
                                deadline_hours = 24.0
                    except (TypeError, ValueError):
                        pass
                    now = datetime.now(timezone.utc)
                    if 0 < deadline_hours < 1:
                        values = {
                            "sent_to_court_at": now,
                            "court_deadline_minutes": round(deadline_hours * 60),
                            "court_deadline_hours": None,
                            "sent_to_court_content": text_to_send[:8000],
                        }
                    else:
                        values = {
                            "sent_to_court_at": now,
                            "court_deadline_hours": round(deadline_hours),
                            "court_deadline_minutes": None,
                            "sent_to_court_content": text_to_send[:8000],
                        }
                    async with ctx.db_session_factory() as session:
                        await session.execute(
                            update(ElderCase)
                            .where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
                            .values(**values)
                        )
                        await session.commit()
                    logger.info("Старейшина: дело №%s зафиксировано как переданное в суд (notify_court), отсчёт срока начат", cid)
                    # Всегда постим в канал решений при отправке в суд (чтобы решение было видно даже если модель не вызвала publish_decision)
                    ch_decisions = ctx.get_channel_id("decisions") if hasattr(ctx, "get_channel_id") else None
                    if not ch_decisions and getattr(ctx.bot, "config", None):
                        ch_decisions = getattr(ctx.bot.config, "channel_for_role", None)
                        if callable(ch_decisions):
                            ch_decisions = ch_decisions("elder", "decisions")
                    if ch_decisions:
                        ch = ctx.bot.get_channel(int(ch_decisions))
                        if ch:
                            try:
                                num = display_no if display_no is not None else cid
                                # Если старейшина не вызвал publish_decision — фиксируем решение в БД и постим в канал с причиной
                                default_reasoning = "Одобрено по процедуре (ст. 19). Обращение передано в суд."
                                reason_text = default_reasoning
                                async with ctx.db_session_factory() as session:
                                    r = await session.execute(select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id))
                                    case_row = r.scalars().one_or_none()
                                    if case_row and not getattr(case_row, "elder_already_decided", False):
                                        await session.execute(
                                            update(ElderCase)
                                            .where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
                                            .values(
                                                elder_decided_at=now,
                                                elder_decision="referendum_approved",
                                                elder_reasoning=default_reasoning,
                                                elder_already_decided=True,
                                            )
                                        )
                                        await session.commit()
                                    elif case_row and getattr(case_row, "elder_reasoning", None):
                                        reason_text = (case_row.elder_reasoning or "").strip() or default_reasoning
                                await ch.send(
                                    f"**По делу №{num}:** Принято решение: **одобрено, передано в суд** (referendum_approved). "
                                    f"По причине: {reason_text}"
                                )
                                logger.info("Старейшина: notify_court — решение опубликовано в канал решений channel_id=%s", ch_decisions)
                            except Exception as dec_e:
                                logger.exception("notify_court: не удалось отправить в канал решений (channel_id=%s): %s", ch_decisions, dec_e)
                        else:
                            logger.warning("notify_court: канал решений id=%s не найден ботом", ch_decisions)
                    else:
                        logger.warning("notify_court: канал решений (decisions) не настроен для старейшины")
                except Exception:
                    logger.exception("notify_court: не удалось записать sent_to_court_at по делу %s", current_cid)
        return "Уведомление в суд отправлено (судьи упомянуты). Отсчёт срока суда зафиксирован."

    async def notify_council(case_id: str) -> str:
        """Уведомить совет: отправить в канал совета (council_inbox) сообщение по делу. Текст берётся из базы (initial_content или sent_to_court_content) — в совет уходит именно то, что сохранено при create_elder_case; поэтому при создании дела в content передавай полный текст обращения пользователя. Из текста убираются только фразы для суда (призыв «проголосовать», упоминания срока). Вызывай после publish_decision(send_to_council). Упоминания совета (@) и галочка ставятся автоматически."""
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: укажи номер дела числом (case_id)."
        ch_id = ctx.get_channel_id("notify_council")
        if not ch_id:
            return "В конфиге не задан канал совета (notify_council). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал совета {ch_id} не найден."
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
            )
            case = result.scalars().one_or_none()
        if not case:
            return f"Дело №{case_id} не найдено. Сначала get_case и publish_decision(send_to_council)."
        _labels = {
            "referendum_request": "референдум",
            "civil_initiative": "гражданская инициатива",
            "bill": "законопроект",
            "appeal_procedure": "апелляция по процедуре",
            "not_established_by_court": "не установлено судом",
        }
        case_type_label = _labels.get(case.case_type) or case.case_type
        # В совет отправляем полный запрос так, как описал пользователь (initial_content в приоритете).
        raw = (case.initial_content or "").strip() or (getattr(case, "sent_to_court_content", None) or "").strip()
        body = _strip_court_boilerplate_for_council(raw)
        if not body:
            body = "(текст дела не сохранён в базе; см. get_case и initial_content при следующей передаче в суд)"
        header = f"Дело №{_case_display_number(case)}. Тип процедуры: {case_type_label}. Решение старейшин: передаётся на исполнение в совет.\n\nЗапрос полностью (как описал обратившийся):\n"
        mentions = _mentions_for_role(ctx.bot, ctx.guild_id, "council")
        chunk_limit = 2000
        try:
            first_content = (f"{mentions}\n\n{header}" if mentions else header).strip()
            remaining = len(first_content)
            if remaining < chunk_limit and body:
                take = min(len(body), chunk_limit - remaining - 1)
                first_content = first_content + "\n" + body[:take]
                body = body[take:]
            elif len(first_content) > chunk_limit:
                first_content = first_content[: chunk_limit - 3].rstrip() + "…"
            last_msg = await channel.send(first_content)
            logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), first_content[:200].replace("\n", " "))
            offset = 0
            while offset < len(body):
                chunk = body[offset : offset + chunk_limit]
                offset += len(chunk)
                if chunk.strip():
                    last_msg = await channel.send(chunk)
            if last_msg is None:
                async for msg in channel.history(limit=1):
                    last_msg = msg
                    break
            if last_msg:
                try:
                    await last_msg.add_reaction("✅")
                except Exception as re:
                    logger.debug("notify_council: не удалось поставить реакцию: %s", re)
            return "Уведомление в совет отправлено: передан полный запрос обратившегося (initial_content). Члены совета упомянуты, галочка поставлена."
        except Exception as e:
            logger.exception("notify_council")
            return f"Ошибка отправки в совет: {e!r}"

    async def publish_judicial_precedent(content: str) -> str:
        """Опубликовать судебный прецедент в канал закона (law_judicial_precedents). Вызывай когда старейшина по делу (разногласие судей или истечение срока) формирует прецедент и решает зафиксировать его в канале судебных прецедентов — если дело касается закона/права."""
        ch_id = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "channels", None)
        if callable(ch_id):
            ch_id = ch_id().get("law_judicial_precedents")
        if not ch_id:
            return "В конфиге не задан канал судебных прецедентов (channels.law_judicial_precedents). Используй get_channels и send_message_to_channel с ID канала судебных прецедентов."
        channel = ctx.bot.get_channel(int(ch_id))
        if not channel:
            return f"Канал судебных прецедентов {ch_id} не найден."
        try:
            prec_text = (content or "").strip()[:2000]
            await channel.send(prec_text)
            logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), prec_text[:150].replace("\n", " "))
            return "Прецедент опубликован в канал судебных прецедентов (law_judicial_precedents)."
        except Exception as e:
            logger.exception("publish_judicial_precedent")
            return f"Ошибка публикации прецедента: {e!r}"

    async def publish_decision(case_id: str = "", decision: str = "", reasoning: str = "", **kwargs: Any) -> str:
        """Опубликовать решение старейшин. reasoning — твоя причина. По первому обращению: только referendum_approved (→ notify_court, record_case_sent_to_court) или referendum_rejected; передавать в совет нельзя. send_to_council, confirm_process, return_to_court допустимы только по делу, возвращённому старейшинам (срок суда истёк или судьи разошлись). После send_to_council обязательно notify_council."""
        from src.roles.elder.logic import elder_may_decide, elder_may_decide_for_case
        case_id = case_id or kwargs.get("case_id") or kwargs.get("case_id=") or ""
        decision = decision or kwargs.get("decision") or kwargs.get("decision=") or ""
        reasoning = reasoning or kwargs.get("reasoning") or kwargs.get("reasoning=") or ""
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: case_id должен быть номером дела (число). Укажи число из текста обращения (Обращение №N) или из list_elder_cases, а не описание типа «референдум»."

        async with ctx.db_session_factory() as session:
            result = await session.execute(select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id))
            case = result.scalars().one_or_none()
            if not case:
                return f"Дело №{case_id} не найдено."
            if not elder_may_decide(decision):
                return f"Недопустимое решение для старейшин: {decision}"
            if not elder_may_decide_for_case(decision, case.case_type):
                return f"По делу типа «{case.case_type}» допустимы только: для референдума/законопроекта/гражданской инициативы — referendum_approved или referendum_rejected; для апелляции — confirm_process, send_to_council, return_to_court."
            # Передать в совет можно только по делу, возвращённому старейшинам (срок суда истёк или судьи разошлись). По первому обращению — только в суд или отклонение.
            if decision in ("send_to_council", "confirm_process", "return_to_court"):
                returned_at = getattr(case, "returned_to_elder_at", None)
                deadline_expired_at = getattr(case, "court_deadline_expired_at", None)
                if not returned_at and not deadline_expired_at:
                    return (
                        "Передать дело в совет (send_to_council), confirm_process или return_to_court можно только по делу, возвращённому старейшинам "
                        "(срок суда истёк или судьи разошлись). По первому обращению старейшина не передаёт дело в совет — предложите обратившемуся оформить запрос в суд (референдум, законопроект, гражданская инициатива)."
                    )
            if case.elder_already_decided:
                current_id = ctx.extra.get("current_case_id")
                try:
                    current_cid = int(current_id) if current_id is not None else None
                except (TypeError, ValueError):
                    current_cid = None
                if current_cid is not None and cid != current_cid:
                    return (
                        f"По делу №{cid} старейшины уже выносили решение. "
                        f"Текущее обращение в этой ветке — по делу №{current_cid}. "
                        f"Для принятия решения по текущему обращению используй case_id={current_cid} "
                        "(publish_decision, затем notify_court и record_case_sent_to_court)."
                    )
                return "По этому делу старейшины уже выносили решение; повторное вмешательство не допускается (Статья IV, п. 6)."

            ch_id = ctx.get_channel_id("decisions")
            if not ch_id and getattr(ctx.bot, "config", None) and getattr(ctx.bot, "role_key", None):
                ch_id = ctx.bot.config.channel_for_role(ctx.bot.role_key, "decisions")
            if ch_id:
                channel = ctx.bot.get_channel(ch_id)
                if channel:
                    decision_labels = {
                        "send_to_council": "передать на исполнение в совет",
                        "return_to_court": "вернуть в суд",
                        "confirm_process": "подтвердить процесс",
                        "referendum_approved": "одобрено, передано в суд",
                        "referendum_rejected": "отклонено (дело закрыто)",
                    }
                    label_ru = decision_labels.get(decision, decision)
                    text_msg = (
                        f"**По делу №{_case_display_number(case)}:** Принято решение: **{label_ru}** ({decision}). "
                        f"По причине: {reasoning}"
                    )
                    try:
                        await channel.send(text_msg[:2000])
                        logger.info("Старейшина → [#%s]: %s", getattr(channel, "name", "?"), text_msg[:150].replace("\n", " "))
                    except Exception as e:
                        logger.exception("publish_decision send")
                else:
                    logger.warning("publish_decision: канал decisions id=%s не найден ботом", ch_id)

            # По референдуму одобренное — оставляем open, чтобы после notify_court + record_case_sent_to_court дело попадало в list_cases_pending_court для отслеживания; остальное — закрываем
            new_status = "open" if decision == "referendum_approved" else "closed"
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == cid)
                .values(
                    status=new_status,
                    elder_decided_at=datetime.utcnow(),
                    elder_decision=decision,
                    elder_reasoning=reasoning,
                    elder_already_decided=True,
                )
            )
            out = "Решение опубликовано и зафиксировано в деле."
            if decision == "referendum_approved":
                out += " Обязательно вызови notify_court(content) по этому делу и затем record_case_sent_to_court(case_id, content_sent=content) — чтобы дело пошло в суд, начался отсчёт срока и текст обращения сохранился в деле; дело остаётся в отслеживании (list_cases_pending_court)."
            elif decision == "referendum_rejected":
                out += " Дело по референдуму закрыто; дальше по нему никаких действий не производится."
            return out

    async def get_case(case_id: str) -> str:
        """Получить данные дела по номеру. В ответе: court_votes, court_deadline_at, expired_ru, court_decided_at (суд вынес решение или нет), elder_decision, elder_decided_at, elder_reasoning, elder_already_decided — для ответов «кто проголосовал?», «истёк ли срок?», «какое решение старейшина принял?». Решения старейшины — только из elder_decision/elder_reasoning; если expired_ru=да и elder_decision пусто — старейшина ещё не вынес решение. Не придумывай — только по полям из ответа."""
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: укажи номер дела числом (из Обращение №N или list_elder_cases)."
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
            )
            case = result.scalars().one_or_none()
            if not case:
                return f"Дело №{case_id} не найдено."
            display_no = _case_display_number(case)
            meta = {}
            if case.meta:
                try:
                    meta = json.loads(case.meta)
                except Exception:
                    meta = {"raw": case.meta}
            sent_at = case.sent_to_court_at
            deadline = _deadline_from_case(case)
            deadline_info = _court_deadline_info(
                sent_at,
                deadline,
                getattr(case, "court_deadline_expired_at", None),
            )
            # Голоса судей по делу — единственный источник правды для ответов «кто проголосовал»
            votes_result = await session.execute(
                select(ElderCaseCourtVote)
                .where(ElderCaseCourtVote.case_id == cid, ElderCaseCourtVote.guild_id == ctx.guild_id)
                .order_by(ElderCaseCourtVote.id.asc())
            )
            court_votes = [
                {
                    "judge_id": r.judge_id,
                    "vote": r.vote,
                    "voted_at": str(r.voted_at) if r.voted_at else None,
                    "message_id": r.message_id,
                }
                for r in votes_result.scalars().all()
            ]
            _case_type_labels = {
                "referendum_request": "референдум",
                "civil_initiative": "гражданская инициатива",
                "bill": "законопроект",
                "appeal_procedure": "апелляция по процедуре",
                "not_established_by_court": "не установлено судом",
            }
            case_type_label_ru = _case_type_labels.get(case.case_type) or case.case_type
            return json.dumps({
                "id": case.id,
                "display_number": display_no,
                "case_type": case.case_type,
                "case_type_label_ru": case_type_label_ru,
                "status": case.status,
                "author_id": case.author_id,
                "initial_content": case.initial_content,
                "created_at": str(case.created_at),
                "elder_already_decided": case.elder_already_decided,
                "elder_decision": case.elder_decision,
                "elder_decided_at": str(case.elder_decided_at) if getattr(case, "elder_decided_at", None) else None,
                "elder_reasoning": getattr(case, "elder_reasoning", None),
                "sent_to_court_at": str(sent_at) if sent_at else None,
                "court_deadline_hours": case.court_deadline_hours,
                "court_deadline_minutes": getattr(case, "court_deadline_minutes", None),
                "court_deadline_at": deadline_info["court_deadline_at"],
                "court_deadline_passed": deadline_info["court_deadline_passed"],
                "court_deadline_status_label": deadline_info.get("court_deadline_status_label", "СРОК ИСТЁК" if deadline_info["court_deadline_passed"] else "ожидание"),
                "court_time_remaining_seconds": deadline_info["court_time_remaining_seconds"],
                "court_time_remaining_text": deadline_info["court_time_remaining_text"],
                "expired_ru": deadline_info.get("expired_ru", "Срок истёк: да" if deadline_info["court_deadline_passed"] else "Срок истёк: нет"),
                "court_decided_at": str(case.court_decided_at) if getattr(case, "court_decided_at", None) else None,
                "court_result": getattr(case, "court_result", None),
                "court_votes": court_votes,
                "sent_to_court_content": getattr(case, "sent_to_court_content", None),
                "returned_to_elder_at": str(case.returned_to_elder_at) if getattr(case, "returned_to_elder_at", None) else None,
                "returned_to_elder_reason": getattr(case, "returned_to_elder_reason", None),
                **meta,
            }, ensure_ascii=False, indent=0)

    # Текст для суда не должен быть только фразой согласия без сути
    _NON_SUBSTANTIVE_PHRASES = (
        "я готов", "я готова", "готов", "готова", "да", "давай", "ок", "помогай", "ниче не отправил",
        "ничего не отправил",
    )

    def _is_non_substantive_court_content(text: str) -> bool:
        t = (text or "").strip()
        t_norm = t.lower().replace("ё", "е")
        if "возвращен в суд" in t_norm:
            return False
        if len(t) < 30:
            return True
        t_lower = t.lower()
        # Короткий текст, состоящий по сути из согласия — не считать содержательным
        if len(t) < 80 and any(p in t_lower for p in _NON_SUBSTANTIVE_PHRASES):
            # Содержательное обращение обычно содержит суть: законопроект, референдум, от гражданина, роль и т.д.
            substantive = any(
                m in t_lower for m in ("законопроект", "референдум", "от гражданин", "суть", "роль", "правило", "подач", "иск", "обращен")
            )
            if not substantive:
                return True
        return False

    async def record_case_sent_to_court(case_id: str, content_sent: str = "") -> str:
        """Зафиксировать, что дело передано в суд — начать отсчёт срока и сохранить текст, отправленный в суд. Вызывай после notify_court: передай в content_sent тот же текст, что отправил в суд — он сохранится для сопоставления с сообщениями судей (если номер дела не указан) и для решений при истечении срока или разногласии судей. content_sent должен содержать содержательную суть обращения (не «я готов» или «дело №N» без сути)."""
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: укажи номер дела числом."
        content_clean = (content_sent or "").strip()
        if content_clean and _is_non_substantive_court_content(content_clean):
            return (
                "Ошибка: в content_sent передана не содержательная суть обращения (слишком короткий текст или только фраза согласия). "
                "В суд нужно передавать полную формулировку: «Дело №N. От гражданина [имя/id]: законопроект — [суть]» или референдум с сутью. "
                "Возьми суть из initial_content дела или из предыдущих сообщений обратившегося в этой ветке и вызови notify_court и record_case_sent_to_court с этим текстом."
            )
        deadline_hours = 24.0
        try:
            rcfg = ctx.bot.config.role_config("elder")
            if isinstance(rcfg, dict):
                val = rcfg.get("court_deadline_hours", 24)
                deadline_hours = float(val) if val is not None else 24.0
                if deadline_hours <= 0:
                    deadline_hours = 24.0
        except (TypeError, ValueError):
            pass
        content_normalized = (content_clean or "").lower().replace("ё", "е")
        is_return_to_court = "возвращен в суд" in content_normalized
        async with ctx.db_session_factory() as session:
            result = await session.execute(select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id))
            case = result.scalars().one_or_none()
            if not case:
                return f"Дело №{case_id} не найдено."
            if getattr(case, "elder_decision", None) == "referendum_rejected" or (
                getattr(case, "elder_already_decided", False) and getattr(case, "status", None) == "closed"
            ):
                return "Дело отклонено старейшиной (referendum_rejected). Передача в суд не производится; record_case_sent_to_court после отклонения не вызывай."
            display_no = _case_display_number(case)
            # Если дело уже зафиксировано при отправке (notify_court) — не сбрасываем срок, только обновляем текст при необходимости
            already_sent = getattr(case, "sent_to_court_at", None) is not None
            if already_sent:
                if content_clean:
                    for pat in (
                        re.compile(r"Дело\s*№\s*N\b", re.IGNORECASE),
                        re.compile(r"дело\s*№\s*n\b", re.IGNORECASE),
                        re.compile(r"Дело\s*№N\b", re.IGNORECASE),
                    ):
                        content_clean = pat.sub(f"Дело №{display_no}", content_clean)
                    await session.execute(
                        update(ElderCase)
                        .where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
                        .values(sent_to_court_content=content_clean[:8000])
                    )
                await session.commit()
                return f"Дело №{display_no} уже было зафиксировано при отправке в суд (notify_court). Отсчёт срока не сбрасывается." + (" Текст в деле обновлён." if content_clean else "")
            now = datetime.now(timezone.utc)
            if 0 < deadline_hours < 1:
                values = {"sent_to_court_at": now, "court_deadline_minutes": round(deadline_hours * 60), "court_deadline_hours": None}
            else:
                values = {"sent_to_court_at": now, "court_deadline_hours": round(deadline_hours), "court_deadline_minutes": None}
            if content_clean:
                # Подставить реальный номер дела вместо «Дело №N», если модель передала букву N
                for pat in (
                    re.compile(r"Дело\s*№\s*N\b", re.IGNORECASE),
                    re.compile(r"дело\s*№\s*n\b", re.IGNORECASE),
                    re.compile(r"Дело\s*№N\b", re.IGNORECASE),
                ):
                    content_clean = pat.sub(f"Дело №{display_no}", content_clean)
                values["sent_to_court_content"] = content_clean[:8000]
            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id)
                .values(**values)
            )
            if is_return_to_court:
                await session.execute(
                    delete(ElderCaseCourtVote).where(
                        ElderCaseCourtVote.case_id == cid,
                        ElderCaseCourtVote.guild_id == ctx.guild_id,
                    )
                )
        if 0 < deadline_hours < 1:
            out = f"Отсчёт срока для суда по делу №{display_no} начат. Срок: {round(deadline_hours * 60)} мин."
        else:
            out = f"Отсчёт срока для суда по делу №{display_no} начат. Срок: {round(deadline_hours)} ч."
        if content_clean:
            out += " Текст, отправленный в суд, сохранён в деле (для сопоставления и решений)."
        if is_return_to_court:
            out += " Дело возвращено в суд — голоса судей сброшены, переголосование по тому же делу."
        return out

    async def list_cases_pending_court() -> str:
        """Список дел, переданных в суд и ожидающих решения. В каждом деле: id, deadline_passed (true=срок истёк), court_deadline_status_label («СРОК ИСТЁК» или «ожидание»), expired_ru («Срок истёк: да» или «Срок истёк: нет»), court_time_remaining_text. Для ответа «истёк ли срок» используй expired_ru дословно: если там «Срок истёк: да» — говори что срок истёк; если «Срок истёк: нет» — не истёк."""
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == ctx.guild_id,
                    ElderCase.status == "open",
                    ElderCase.sent_to_court_at.isnot(None),
                    ElderCase.court_decided_at.is_(None),
                )
            )
            cases = result.scalars().all()
        if not cases:
            return "Нет дел, переданных в суд и ожидающих решения."
        out = []
        for c in cases:
            sent_at = c.sent_to_court_at
            deadline = _deadline_from_case(c)
            deadline_info = _court_deadline_info(
                sent_at,
                deadline,
                getattr(c, "court_deadline_expired_at", None),
            )
            content_preview = (c.initial_content or "").strip()[:500]
            if (c.initial_content or "").strip() and len((c.initial_content or "").strip()) > 500:
                content_preview += "..."
            sent_to_court_preview = (getattr(c, "sent_to_court_content", None) or "").strip()[:400]
            if (getattr(c, "sent_to_court_content", None) or "").strip() and len((getattr(c, "sent_to_court_content", None) or "").strip()) > 400:
                sent_to_court_preview += "..."
            out.append({
                "id": c.id,
                "display_number": _case_display_number(c),
                "case_type": c.case_type,
                "initial_content": content_preview or "(нет текста)",
                "sent_to_court_content": sent_to_court_preview or None,
                "sent_to_court_at": str(sent_at),
                "court_deadline_hours": c.court_deadline_hours,
                "court_deadline_minutes": getattr(c, "court_deadline_minutes", None),
                "court_deadline_at": deadline_info["court_deadline_at"],
                "deadline_passed": deadline_info["court_deadline_passed"],
                "court_deadline_status_label": deadline_info.get("court_deadline_status_label", "СРОК ИСТЁК" if deadline_info["court_deadline_passed"] else "ожидание"),
                "court_time_remaining_seconds": deadline_info["court_time_remaining_seconds"],
                "court_time_remaining_text": deadline_info["court_time_remaining_text"],
                "expired_ru": deadline_info.get("expired_ru", "Срок истёк: да" if deadline_info["court_deadline_passed"] else "Срок истёк: нет"),
                "returned_to_elder_reason": getattr(c, "returned_to_elder_reason", None),
            })
        return json.dumps(out, ensure_ascii=False, indent=0)

    async def list_cases_pending_elder_decision() -> str:
        """Дела, ожидающие решения старейшин: только те, что возвращены из суда (срок истёк или судьи разошлись) и по которым старейшина ещё не вынес решение. Не путай с list_elder_cases(open): открытые дела могут быть в суде — их рассматривает суд. Для вопроса «какие дела на рассмотрении у старейшин» вызывай этот инструмент."""
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == ctx.guild_id,
                    ElderCase.status == "open",
                    ElderCase.elder_already_decided == False,  # noqa: E712
                    or_(
                        ElderCase.returned_to_elder_at.isnot(None),
                        ElderCase.court_deadline_expired_at.isnot(None),
                    ),
                )
            )
            cases = result.scalars().all()
        if not cases:
            return "Нет дел, ожидающих решения старейшин. (Старейшины выносят решение при поступлении обращения; дела появляются здесь только когда суд не вынес решение в срок или судьи разошлись.)"
        out = []
        for c in cases:
            content_preview = (c.initial_content or "").strip()[:400]
            if (c.initial_content or "").strip() and len((c.initial_content or "").strip()) > 400:
                content_preview += "..."
            out.append({
                "id": c.id,
                "display_number": _case_display_number(c),
                "case_type": c.case_type,
                "returned_to_elder_reason": getattr(c, "returned_to_elder_reason", None),
                "initial_content": content_preview or "(нет текста)",
            })
        return json.dumps(out, ensure_ascii=False, indent=0)

    async def get_guild_emojis() -> str:
        """Список кастомных эмодзи сервера (name, id, reaction_string). Используй их для реакций на сообщения: add_reaction по имени эмодзи (name) или по Unicode (✅, 👎 и т.д.). Особенно на одобрение/неодобрение судей — ставь подходящую реакцию когда ровно двое судей проголосовали."""
        return get_guild_emojis_json(ctx.bot, ctx.guild_id)

    async def add_reaction(channel_id: int, message_id: int, emoji: str) -> str:
        """Поставить реакцию на сообщение. emoji: Unicode (✅, 👎, 👍) или имя кастомного эмодзи сервера (из get_guild_emojis). Канал — только тот, где лежит сообщение. Ставь реакции на голоса судей (одобрение/неодобрение), когда ровно двое судей проголосовали."""
        ch = ctx.bot.get_channel(int(channel_id))
        if not ch:
            return f"Канал {channel_id} не найден."
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception as e:
            return f"Сообщение не найдено: {e!r}"
        em_str = (emoji or "").strip() or "✅"
        emoji_obj = None
        guild = ctx.bot.get_guild(ctx.guild_id)
        if guild:
            if em_str.isdigit():
                emoji_obj = guild.get_emoji(int(em_str))
            else:
                em_lower = em_str.lower()
                for guild_emoji in guild.emojis:
                    if guild_emoji.name and guild_emoji.name.lower() == em_lower:
                        emoji_obj = guild_emoji
                        break
        if emoji_obj is None:
            emoji_obj = em_str
        try:
            await msg.add_reaction(emoji_obj)
            return "Реакция поставлена."
        except Exception as e:
            logger.exception("add_reaction: channel=%s message=%s emoji=%s", channel_id, message_id, emoji)
            return f"Ошибка реакции: {e!r}"

    async def get_court_report(limit: int = 30) -> str:
        """Отчёт о последних событиях в каналах надзора (суд, решения суда, судебные прецеденты, совет и т.д.): id записи, тип события, легитимность (approved/rejected/pending — галочка/крестик старейшины). Вызывай при «что в суде?», «кто проголосовал?», «что одобрено?». Чтобы пометить запись — mark_court_log_legitimacy(log_id, approved или rejected)."""
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCourtLog)
                .where(ElderCourtLog.guild_id == ctx.guild_id)
                .order_by(ElderCourtLog.created_at.desc())
                .limit(min(limit, 50))
            )
            rows = result.scalars().all()
        if not rows:
            return "Отчёт по каналам надзора пока пуст."
        lines = []
        for r in rows:
            leg = getattr(r, "legitimacy", None) or "pending"
            line = f"id={r.id} | {r.created_at} | ch={r.channel_id} | {r.event_type} | legitimacy={leg} | {r.summary or ''}"
            if r.meta:
                line += f" | {r.meta}"
            lines.append(line)
        return "\n".join(reversed(lines))

    async def mark_court_log_legitimacy(log_id: str, legitimacy: str) -> str:
        """Пометить в базе легитимность действия по записи отчёта надзора: approved (одобрено, галочка) или rejected (отклонено, крестик). log_id — id из get_court_report. Используй для событий, которые ещё не оценены при надзоре (legitimacy=pending), или чтобы исправить оценку."""
        try:
            lid = int(log_id)
        except (ValueError, TypeError):
            return "Ошибка: log_id должен быть числом (id из get_court_report)."
        leg = (legitimacy or "").strip().lower()
        if leg not in ("approved", "rejected"):
            return "legitimacy должен быть approved (одобрено) или rejected (отклонено)."
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCourtLog).where(
                    ElderCourtLog.id == lid,
                    ElderCourtLog.guild_id == ctx.guild_id,
                )
            )
            row = result.scalars().one_or_none()
            if not row:
                return f"Запись отчёта с id={log_id} не найдена."
            await session.execute(
                update(ElderCourtLog)
                .where(ElderCourtLog.id == lid, ElderCourtLog.guild_id == ctx.guild_id)
                .values(legitimacy=leg, legitimacy_at=datetime.now(timezone.utc))
            )
        label = "одобрено (✓)" if leg == "approved" else "отклонено (✗)"
        return f"Запись №{log_id} помечена в базе как {label}."

    async def list_elder_cases(status: str = "open") -> str:
        """Список дел старейшин по статусу (open или closed)."""
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(ElderCase)
                .where(
                    ElderCase.guild_id == ctx.guild_id,
                    ElderCase.case_type.in_(["appeal_procedure", "referendum_request", "civil_initiative", "bill", "not_established_by_court"]),
                    ElderCase.status == status,
                )
            )
            cases = result.scalars().all()
            if not cases:
                return "Нет дел с указанным статусом."
            return json.dumps(
                [{"id": c.id, "display_number": _case_display_number(c), "case_type": c.case_type, "status": c.status, "created_at": str(c.created_at)} for c in cases],
                ensure_ascii=False,
            )

    async def get_current_time() -> str:
        """Текущее время по UTC. Используй для ответов «сколько сейчас время?», «который час?» и для проверки истечения сроков по делам."""
        now = datetime.now(timezone.utc)
        months_ru = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
        text = f"{now.day} {months_ru[now.month - 1]} {now.year}, {now.hour:02d}:{now.minute:02d} UTC"
        return json.dumps({
            "current_time_utc": text,
            "iso": now.isoformat(),
            "hour": now.hour,
            "minute": now.minute,
            "day": now.day,
            "month": now.month,
            "year": now.year,
        }, ensure_ascii=False)

    return [
        Tool(
            name="get_channels",
            description="Получить список всех каналов сервера: id, name, category_name, topic, viewable_by_roles, denied_for_roles. Используй этот список, чтобы по названию и категории сам решить, какие каналы прочитать для ответа (суд, совет, право, решения и т.д.), затем вызывай get_channel_content(channel_id) для выбранных каналов.",
            parameters=build_parameters({}, required=[]),
            execute=get_channels,
        ),
        Tool(
            name="get_channels_in_category",
            description="Каналы по подстроке в названии категории (например «право»). Есть viewable_by_roles и denied_for_roles — не рекомендуй канал, к которому у обратившегося нет доступа.",
            parameters=build_parameters({"category_substring": ("string", "Подстрока в названии категории, например право")}, required=["category_substring"]),
            execute=get_channels_in_category,
        ),
        Tool(
            name="get_reference_channels",
            description="Каналы категории «право» (правила, прецеденты, закон) с полями доступа. Дальше get_channel_content(id) или get_all_law_channel_contents — прочитать всё сразу.",
            parameters=build_parameters({}, required=[]),
            execute=get_reference_channels,
        ),
        Tool(
            name="get_all_law_channel_contents",
            description="Прочитать содержимое всех текстовых каналов из категории «право» (в т.ч. «📜 право») за один вызов: правила, прецеденты, закон. Вызывай перед ответом по существу, чтобы опираться на закон.",
            parameters=build_parameters({
                "category_substring": ("string", "Подстрока в названии категории (по умолчанию «право»)"),
                "limit_per_channel": ("integer", "Макс. сообщений на канал (по умолчанию 40)"),
            }, required=[]),
            execute=get_all_law_channel_contents,
        ),
        Tool(
            name="get_roles_and_members",
            description="Получить все роли сервера и участников в каждой роли (id роли, название, список участников с id и display_name). Чтобы знать, кто судья, кто с ПМЖ, кто в совете, старейшины и т.д.",
            parameters=build_parameters({}, required=[]),
            execute=get_roles_and_members,
        ),
        Tool(
            name="get_member_roles",
            description="Получить роли участника по никнейму, имени или Discord ID. Для «какие у меня роли?» передай «me» или id автора из блока «КОМУ ТЫ ОТВЕЧАЕШЬ». Возвращает id, display_name, name, roles.",
            parameters=build_parameters({"member_query": ("string", "Никнейм, имя, Discord ID или «me»/«я» для автора сообщения")}, required=["member_query"]),
            execute=get_member_roles,
        ),
        Tool(
            name="get_current_time",
            description="Узнать текущее время (UTC). Вызывай при вопросах «сколько сейчас время?», «который час?», «какая дата?» — отвечай по данным из current_time_utc.",
            parameters=build_parameters({}, required=[]),
            execute=get_current_time,
        ),
        Tool(
            name="get_channel_content",
            description="Прочитать содержимое канала (закреплённые и последние сообщения). channel_id бери из списка каналов (get_channels) по выбранному name/id. Выбирай каналы по смыслу вопроса (суд, совет, право, решения и т.д.) и читай только их; ответ строй только по прочитанному.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала из get_channels / get_reference_channels"), "limit": ("integer", "Макс. сообщений (по умолчанию 40)")}, required=["channel_id"]),
            execute=get_channel_content,
        ),
        Tool(
            name="get_court_inbox_recent",
            description="Прочитать последние сообщения из канала суда. При вопросе о референдуме: только сообщения от пользователей с ролью судьи (см. get_roles_and_members) считаются голосом судьи; нужно два судьи «да» для одобрения. Сообщение от человека без роли судьи не является решением суда.",
            parameters=build_parameters({"limit": ("integer", "Макс. сообщений (по умолчанию 25)")}, required=[]),
            execute=get_court_inbox_recent,
        ),
        Tool(
            name="get_council_inbox_recent",
            description="Прочитать последние сообщения из канала совета. Обязательно вызывай при вопросах «что решил совет?», «одобрил ли совет?» — отвечай только на основе прочитанного; если решения по делу нет — говори «от совета ответа пока не поступало».",
            parameters=build_parameters({"limit": ("integer", "Макс. сообщений (по умолчанию 25)")}, required=[]),
            execute=get_council_inbox_recent,
        ),
        Tool(
            name="send_message_to_channel",
            description="Отправить сообщение в канал по его ID. ID бери из get_channels или из блока «Каналы старейшин» в контексте.",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала из get_channels"),
                "content": ("string", "Текст сообщения"),
            }),
            execute=send_message_to_channel,
        ),
        Tool(
            name="create_elder_case",
            description="Создать дело (при одобрении заявки). Вызывай только когда решил одобрить обращение и передать в суд. Делу присваивается номер; вернувшийся case_id используй в publish_decision, notify_court, record_case_sent_to_court. content — полный текст обращения пользователя целиком (все абзацы, пункты, положения), без сокращений — этот текст потом уходит в совет.",
            parameters=build_parameters({"content": ("string", "Полный текст обращения пользователя целиком: все абзацы, пункты (1), 2)...), положения закона, обоснования. Не сокращай — иначе в совет придёт неполная информация.")}, required=["content"]),
            execute=create_elder_case,
        ),
        Tool(
            name="notify_court",
            description="Уведомить суд: отправить сообщение в канал суда (судьи @). В тексте: номер дела — Дело №X (подставляй реальный номер из контекста «Текущее обращение по делу №X», никогда не пиши букву N), суть; в конце: «Проголосуйте ответом на это сообщение: за или против.» При первой передаче: «Дело №X. От гражданина [имя]: гражданская инициатива / законопроект — [суть]. Проголосуйте…» После вызова — record_case_sent_to_court(case_id, content_sent=этот_текст).",
            parameters=build_parameters({"content": ("string", "Текст для суда: Дело №X (реальный номер дела из контекста), суть обращения")}, required=["content"]),
            execute=notify_court,
        ),
        Tool(
            name="notify_council",
            description="Уведомить совет по делу: отправить в канал совета сообщение. Текст сути автоматически берётся из базы (sent_to_court_content или initial_content) — совет всегда получает полную суть дела. Вызывай с case_id после publish_decision(case_id, send_to_council, reasoning). Упоминания (@) и галочка — автоматически.",
            parameters=build_parameters({"case_id": ("string", "Номер дела (после send_to_council)")}, required=["case_id"]),
            execute=notify_council,
        ),
        Tool(
            name="publish_decision",
            description="Опубликовать решение старейшин в канал решений (elder_decisions). **Обязателен по каждому делу:** ты всегда принимаешь решение и выставляешь его в канал решений; без этого вызова решение не считается принятым. По референдуму/инициативе: referendum_approved (одобрено → затем notify_court, record_case_sent_to_court) или referendum_rejected (отклонено). По апелляции: confirm_process, send_to_council, return_to_court. Вызывай до notify_court при одобрении.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела"),
                "decision": ("string", "referendum_approved | referendum_rejected (референдум) или confirm_process | send_to_council | return_to_court (апелляция)"),
                "reasoning": ("string", "Твоя причина решения: почему отклонил или почему одобрил/передал в совет (по закону, по существу). Не «суд не вынес решение» — это повод забрать дело; причина — твоя мотивировка."),
            }),
            execute=publish_decision,
        ),
        Tool(
            name="publish_rejection_to_decisions",
            description="Опубликовать в канал решений (elder_decisions) решение об отклонении обращения. Вызывай при отклонении заявки (дело не создаётся): решение «Отклонено. По причине: …» появится в канале решений. Старейшина всегда публикует решение в elder_decisions — при отклонении используй этот вызов.",
            parameters=build_parameters({"reasoning": ("string", "Причина отклонения по закону")}, required=["reasoning"]),
            execute=publish_rejection_to_decisions,
        ),
        Tool(
            name="publish_judicial_precedent",
            description="Опубликовать судебный прецедент в канал law_judicial_precedents. Вызывай когда по делу (разногласие судей или истечение срока) старейшина сформировал прецедент и решает зафиксировать его — если дело касается закона/права.",
            parameters=build_parameters({"content": ("string", "Текст прецедента для канала судебных прецедентов")}, required=["content"]),
            execute=publish_judicial_precedent,
        ),
        Tool(
            name="get_case",
            description="Получить данные дела: сроки, expired_ru, court_votes, court_decided_at, elder_decision, elder_decided_at, elder_reasoning. Для «какое решение старейшина принял?» смотри elder_*; если срок истёк и elder_decision пусто — вынеси решение.",
            parameters=build_parameters({"case_id": ("string", "Номер дела")}),
            execute=get_case,
        ),
        Tool(
            name="list_elder_cases",
            description="Список всех дел по статусу (open или closed). Не путай с «делами на рассмотрении у старейшин»: открытые дела могут быть в суде — для «какие дела у старейшин на рассмотрении» вызывай list_cases_pending_elder_decision().",
            parameters=build_parameters({"status": ("string", "Статус дела: open или closed")}, required=[]),
            execute=list_elder_cases,
        ),
        Tool(
            name="list_cases_pending_elder_decision",
            description="Дела, ожидающие решения старейшин: только возвращённые из суда (срок истёк или судьи разошлись), по которым старейшина ещё не вынес решение. Вызывай при «какие дела на рассмотрении у старейшин» — не используй для этого list_elder_cases(open).",
            parameters=build_parameters({}, required=[]),
            execute=list_cases_pending_elder_decision,
        ),
        Tool(
            name="record_case_sent_to_court",
            description="Зафиксировать передачу дела в суд и сохранить текст, отправленный в суд. Вызывай после notify_court: передай content_sent — тот же текст, что отправил в суд. При возврате дела в суд (return_to_court) после notify_court с текстом «Суд возвращён в суд с указанием нарушения процедуры…» обязательно вызови record_case_sent_to_court(case_id, content_sent=тот же текст) — тогда у дела будет новый срок, оно останется в списке и в напоминаниях судьям; старые голоса судей по этому делу сбрасываются.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела (Обращение №N)"),
                "content_sent": ("string", "Текст, который ты отправил в суд (notify_court) — сохраняется в деле"),
            }, required=["case_id"]),
            execute=record_case_sent_to_court,
        ),
        Tool(
            name="list_cases_pending_court",
            description="Дела, переданные в суд и ожидающие решения: id, тип, суть обращения (initial_content), когда передано, срок (ч), истёк ли срок. Вызывай при «какие дела у суда?», «какого референдума?» — отвечай только по этим данным, не выдумывай содержание. При истёкшем сроке решай по закону.",
            parameters=build_parameters({}, required=[]),
            execute=list_cases_pending_court,
        ),
        Tool(
            name="get_guild_emojis",
            description="Список кастомных эмодзи сервера (name, id). Используй для реакций: add_reaction(channel_id, message_id, emoji_name или Unicode). Особенно на одобрение/неодобрение судей — когда ровно двое судей проголосовали.",
            parameters=build_parameters({}, required=[]),
            execute=get_guild_emojis,
        ),
        Tool(
            name="add_reaction",
            description="Поставить реакцию на сообщение. emoji — Unicode (✅, 👎) или имя эмодзи сервера из get_guild_emojis. Ставь реакции на голоса судей в канале суда.",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала, где сообщение"),
                "message_id": ("integer", "ID сообщения"),
                "emoji": ("string", "Имя эмодзи сервера или Unicode, например ✅ или thumbs_up"),
            }, required=["channel_id", "message_id", "emoji"]),
            execute=add_reaction,
        ),
        Tool(
            name="get_court_report",
            description="Отчёт по каналам надзора (суд, решения суда, судебные прецеденты): id, event_type, legitimacy (approved/rejected/pending). Для пометки легитимности используй mark_court_log_legitimacy(log_id, approved|rejected).",
            parameters=build_parameters({"limit": ("integer", "Макс. записей (по умолчанию 30)")}, required=[]),
            execute=get_court_report,
        ),
        Tool(
            name="mark_court_log_legitimacy",
            description="Пометить в базе легитимность действия по записи отчёта: approved (одобрено) или rejected (отклонено). log_id — id из get_court_report. Для записей с legitimacy=pending или для исправления оценки.",
            parameters=build_parameters({
                "log_id": ("string", "ID записи из get_court_report"),
                "legitimacy": ("string", "approved (одобрено) или rejected (отклонено)"),
            }, required=["log_id", "legitimacy"]),
            execute=mark_court_log_legitimacy,
        ),
    ]
