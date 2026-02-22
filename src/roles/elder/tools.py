"""
Инструменты агента «Старейшина»: каналы и роли сервера, отправка в любой канал по ID, БД.
Агент сам решает, куда и что писать, на основе get_channels и get_roles_and_members.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update  # type: ignore[reportMissingImports]

from src.core.tools import Tool, build_parameters
from src.core.agent_ctx import AgentContext
from src.core.models import ElderCase
from src.core.discord_guild import (
    get_guild_channels_json,
    get_guild_roles_and_members_json,
    get_member_roles_json_async,
    get_channel_content_async,
    get_channels_where_category_contains,
    get_all_reference_channel_contents_async,
)

logger = logging.getLogger("basuni.elder.tools")


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

    async def get_all_law_channel_contents(category_substring: str = "право", limit_per_channel: int = 40) -> str:
        """Получить и прочитать содержимое всех текстовых каналов из категории «право» (название может быть с эмодзи, например «📜 право»). Один вызов — все каналы: правила, прецеденты, закон. Используй перед ответом по существу, чтобы опираться на закон."""
        sub = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "reference_category_name", None) or category_substring
        return await get_all_reference_channel_contents_async(ctx.bot, ctx.guild_id, sub, min(limit_per_channel, 50))

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
        """Уведомить суд: отправить сообщение в канал суда (court_inbox). Используй после принятия обращения по делу о референдуме или апелляции — суд должен знать о деле."""
        ch_id = ctx.get_channel_id("notify_court")
        if not ch_id:
            return "В конфиге не задан канал суда (notify_court). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал суда {ch_id} не найден."
        try:
            await channel.send(content[:2000])
            return "Уведомление в суд отправлено."
        except Exception as e:
            logger.exception("notify_court")
            return f"Ошибка отправки в суд: {e!r}"

    async def notify_council(content: str) -> str:
        """Уведомить совет: отправить сообщение в канал совета (council_inbox). Используй когда решение старейшин передаётся на исполнение в совет."""
        ch_id = ctx.get_channel_id("notify_council")
        if not ch_id:
            return "В конфиге не задан канал совета (notify_council). Используй send_message_to_channel с ID из контекста."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return f"Канал совета {ch_id} не найден."
        try:
            await channel.send(content[:2000])
            return "Уведомление в совет отправлено."
        except Exception as e:
            logger.exception("notify_council")
            return f"Ошибка отправки в совет: {e!r}"

    async def publish_decision(case_id: str, decision: str, reasoning: str) -> str:
        """Опубликовать решение старейшин: confirm_process, send_to_council или return_to_court. По одному делу — только один раз. Сообщение уходит в канал решений и дело закрывается в БД. case_id — именно номер дела (число из «Обращение №N» или из list_elder_cases), не описание."""
        from src.roles.elder.logic import elder_may_decide
        if not elder_may_decide(decision):
            return f"Недопустимое решение для старейшин: {decision}"

        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: case_id должен быть номером дела (число). Укажи число из текста обращения (Обращение №N) или из list_elder_cases, а не описание типа «референдум»."

        async with ctx.db_session_factory() as session:
            result = await session.execute(select(ElderCase).where(ElderCase.id == cid, ElderCase.guild_id == ctx.guild_id))
            case = result.scalars().one_or_none()
            if not case:
                return f"Дело №{case_id} не найдено."
            if case.elder_already_decided:
                return "По этому делу старейшины уже выносили решение; повторное вмешательство не допускается (Статья IV, п. 6)."

            ch_id = ctx.get_channel_id("decisions")
            if ch_id:
                channel = ctx.bot.get_channel(ch_id)
                if channel:
                    text_msg = f"**Решение по делу №{case_id}**\nРешение: {decision}\nОбоснование: {reasoning}"
                    try:
                        await channel.send(text_msg[:2000])
                    except Exception as e:
                        logger.exception("publish_decision send")
                        return f"Ошибка публикации в канал: {e!r}"
            else:
                return "В конфиге не задан канал для решений (decisions). Используй get_channels и send_message_to_channel для публикации в нужный канал."

            await session.execute(
                update(ElderCase)
                .where(ElderCase.id == cid)
                .values(
                    status="closed",
                    elder_decided_at=datetime.utcnow(),
                    elder_decision=decision,
                    elder_reasoning=reasoning,
                    elder_already_decided=True,
                )
            )
            return "Решение опубликовано и зафиксировано в деле."

    async def get_case(case_id: str) -> str:
        """Получить данные дела старейшин по номеру (число из «Обращение №N» или list_elder_cases)."""
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
            return json.dumps({
                "id": case.id,
                "case_type": case.case_type,
                "status": case.status,
                "author_id": case.author_id,
                "initial_content": case.initial_content,
                "created_at": str(case.created_at),
                "elder_already_decided": case.elder_already_decided,
                "elder_decision": case.elder_decision,
                **meta,
            }, ensure_ascii=False, indent=0)

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
            description="Уведомить суд: отправить сообщение в канал суда. Вызывай после принятия обращения (референдум, апелляция), чтобы суд знал о деле — перед ответом гражданину.",
            parameters=build_parameters({"content": ("string", "Текст уведомления для суда (суть дела, номер обращения)")}, required=["content"]),
            execute=notify_court,
        ),
        Tool(
            name="notify_council",
            description="Уведомить совет: отправить сообщение в канал совета. Вызывай когда решение старейшин передаётся на исполнение в совет (send_to_council).",
            parameters=build_parameters({"content": ("string", "Текст уведомления для совета")}, required=["content"]),
            execute=notify_council,
        ),
        Tool(
            name="publish_decision",
            description="Опубликовать решение старейшин по делу: confirm_process (подтвердить процесс), send_to_council (на исполнение в совет), return_to_court (вернуть в суд с указанием нарушения). По одному делу допускается только один раз. Сообщение уходит в канал решений, дело закрывается в БД.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела"),
                "decision": ("string", "confirm_process | send_to_council | return_to_court"),
                "reasoning": ("string", "Краткое обоснование"),
            }),
            execute=publish_decision,
        ),
        Tool(
            name="get_case",
            description="Получить данные дела по номеру (сроки, кворум, тип — для проверки процедуры).",
            parameters=build_parameters({"case_id": ("string", "Номер дела")}),
            execute=get_case,
        ),
        Tool(
            name="list_elder_cases",
            description="Список дел у старейшин по статусу (open или closed).",
            parameters=build_parameters({"status": ("string", "Статус дела: open или closed")}, required=[]),
            execute=list_elder_cases,
        ),
    ]
