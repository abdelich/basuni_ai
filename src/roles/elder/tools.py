"""
Инструменты агента «Старейшина»: каналы и роли сервера, отправка в любой канал по ID, БД.
Агент сам решает, куда и что писать, на основе get_channels и get_roles_and_members.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select, update  # type: ignore[reportMissingImports]

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
            await channel.send(content[:2000])
            return "Сообщение отправлено."
        except Exception as e:
            logger.exception("send_message_to_channel")
            return f"Ошибка отправки: {e!r}"

    async def notify_court(content: str) -> str:
        """Уведомить суд: отправить сообщение в канал суда (court_inbox), с упоминанием судей (@). В тексте обязательно: номер дела (Дело №N), суть; в конце добавь: «Проголосуйте ответом на это сообщение: за или против.» (судьи могут голосовать ответом на сообщение или на напоминание). После вызова обязательно record_case_sent_to_court(case_id, content_sent=этот_текст)."""
        ch_id = ctx.get_channel_id("notify_court")
        if not ch_id:
            return "В конфиге не задан канал суда (notify_court). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал суда {ch_id} не найден."
        mentions = _mentions_for_role(ctx.bot, ctx.guild_id, "judge")
        full = (f"{mentions}\n\n{content}" if mentions else content).strip()[:2000]
        try:
            await channel.send(full)
            return "Уведомление в суд отправлено (судьи упомянуты)."
        except Exception as e:
            logger.exception("notify_court")
            return f"Ошибка отправки в суд: {e!r}"

    async def notify_council(content: str) -> str:
        """Уведомить совет: отправить сообщение в канал совета (council_inbox), с упоминанием всех членов совета (@). В content обязательно: (1) номер дела (Дело №N), (2) суть дела **полностью** — передавай полное содержание из get_case(N).sent_to_court_content или initial_content (не сокращай), чтобы члены совета видели, что обсуждают, и могли обдумать и вынести решение. Формат: «Дело №N. Решение старейшин: передаётся на исполнение в совет. Суть (полностью): [содержание дела целиком]». После отправки ставится галочка (✅)."""
        ch_id = ctx.get_channel_id("notify_council")
        if not ch_id:
            return "В конфиге не задан канал совета (notify_council). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал совета {ch_id} не найден."
        mentions = _mentions_for_role(ctx.bot, ctx.guild_id, "council")
        full = (f"{mentions}\n\n{content}" if mentions else content).strip()[:2000]
        try:
            msg = await channel.send(full)
            try:
                await msg.add_reaction("✅")
            except Exception as re:
                logger.debug("notify_council: не удалось поставить реакцию на своё сообщение: %s", re)
            return "Уведомление в совет отправлено (члены совета упомянуты). На сообщение поставлена галочка."
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
            await channel.send((content or "").strip()[:2000])
            return "Прецедент опубликован в канал судебных прецедентов (law_judicial_precedents)."
        except Exception as e:
            logger.exception("publish_judicial_precedent")
            return f"Ошибка публикации прецедента: {e!r}"

    async def publish_decision(case_id: str, decision: str, reasoning: str) -> str:
        """Опубликовать решение старейшин. reasoning — твоя причина. По первому обращению: только referendum_approved (→ notify_court, record_case_sent_to_court) или referendum_rejected; передавать в совет нельзя. send_to_council, confirm_process, return_to_court допустимы только по делу, возвращённому старейшинам (срок суда истёк или судьи разошлись). После send_to_council обязательно notify_council."""
        from src.roles.elder.logic import elder_may_decide, elder_may_decide_for_case
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
                return f"По делу типа «{case.case_type}» допустимы только: для референдума — referendum_approved или referendum_rejected; для апелляции — confirm_process, send_to_council, return_to_court."
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
                return "По этому делу старейшины уже выносили решение; повторное вмешательство не допускается (Статья IV, п. 6)."

            ch_id = ctx.get_channel_id("decisions")
            if ch_id:
                channel = ctx.bot.get_channel(ch_id)
                if channel:
                    text_msg = f"**По делу №{case_id}:** Принято решение: {decision}. По причине: {reasoning}"
                    try:
                        await channel.send(text_msg[:2000])
                    except Exception as e:
                        logger.exception("publish_decision send")
                        return f"Ошибка публикации в канал: {e!r}"
            else:
                return "В конфиге не задан канал для решений (decisions). Используй get_channels и send_message_to_channel для публикации в нужный канал."

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
            return json.dumps({
                "id": case.id,
                "case_type": case.case_type,
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
            now = datetime.now(timezone.utc)
            if 0 < deadline_hours < 1:
                values = {"sent_to_court_at": now, "court_deadline_minutes": round(deadline_hours * 60), "court_deadline_hours": None}
            else:
                values = {"sent_to_court_at": now, "court_deadline_hours": round(deadline_hours), "court_deadline_minutes": None}
            if content_clean:
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
            out = f"Отсчёт срока для суда по делу №{case_id} начат. Срок: {round(deadline_hours * 60)} мин."
        else:
            out = f"Отсчёт срока для суда по делу №{case_id} начат. Срок: {round(deadline_hours)} ч."
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
                    ElderCase.case_type.in_(["appeal_procedure", "referendum_request", "not_established_by_court"]),
                    ElderCase.status == status,
                )
            )
            cases = result.scalars().all()
            if not cases:
                return "Нет дел с указанным статусом."
            return json.dumps(
                [{"id": c.id, "case_type": c.case_type, "status": c.status, "created_at": str(c.created_at)} for c in cases],
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
            name="notify_court",
            description="Уведомить суд: отправить сообщение в канал суда (судьи @). В тексте: номер дела (Дело №N), суть; в конце обязательно добавь: «Проголосуйте ответом на это сообщение: за или против.» (судьи голосуют ответом на сообщение или на напоминание). При первой передаче: «Дело №N. От гражданина X: законопроект — [суть]. Проголосуйте ответом на это сообщение: за или против.» При возврате в суд — суть, причина возврата, и ту же фразу про голос ответом. После вызова — record_case_sent_to_court(case_id, content_sent=этот_текст).",
            parameters=build_parameters({"content": ("string", "Текст для суда: Дело №N, суть (при возврате в суд — кратко суть + причина возврата)")}, required=["content"]),
            execute=notify_court,
        ),
        Tool(
            name="notify_council",
            description="Уведомить совет: отправить сообщение в канал совета. В content: (1) номер дела (Дело №N), (2) суть дела **полностью** — полное содержание из get_case(N).sent_to_court_content или initial_content (не сокращай), чтобы члены совета видели, что обсуждают. Формат: «Дело №N. Решение старейшин: передаётся на исполнение в совет. Суть (полностью): [содержание целиком]». Упоминания (@) подставляются автоматически. Вызывай при send_to_council.",
            parameters=build_parameters({"content": ("string", "Текст для совета: Дело №N и полная суть дела (содержание целиком)")}, required=["content"]),
            execute=notify_council,
        ),
        Tool(
            name="publish_decision",
            description="Опубликовать решение старейшин. По референдуму — только referendum_approved (одобрено, дальше в суд; затем notify_court и record_case_sent_to_court) или referendum_rejected (отклонено, дело закрыто навсегда). По апелляции: confirm_process, send_to_council, return_to_court. По одному делу — один раз.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела"),
                "decision": ("string", "referendum_approved | referendum_rejected (референдум) или confirm_process | send_to_council | return_to_court (апелляция)"),
                "reasoning": ("string", "Твоя причина решения: почему отклонил или почему одобрил/передал в совет (по закону, по существу). Не «суд не вынес решение» — это повод забрать дело; причина — твоя мотивировка."),
            }),
            execute=publish_decision,
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
