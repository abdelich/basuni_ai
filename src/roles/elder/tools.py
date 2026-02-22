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
    get_channel_content_async,
    get_channels_where_category_contains,
)

logger = logging.getLogger("basuni.elder.tools")


def make_elder_tools(ctx: AgentContext) -> list[Tool]:
    """Создаёт список инструментов старейшины с привязкой к контексту (бот, гильдия, БД)."""

    async def get_channels() -> str:
        """Получить все текстовые каналы сервера: id, name, category_name, topic. Названия как на сервере (могут быть с эмодзи). Сам решай, в какие заходить и что читать."""
        return get_guild_channels_json(ctx.bot, ctx.guild_id)

    async def get_channels_in_category(category_substring: str) -> str:
        """Найти каналы, у которых в названии категории содержится подстрока (без учёта регистра). Например «право» найдёт категорию «📜 право». Возвращает id, name, category_name — потом используй get_channel_content(id) чтобы прочитать."""
        return get_channels_where_category_contains(ctx.bot, ctx.guild_id, category_substring)

    async def get_reference_channels() -> str:
        """Получить каналы для прецедентов и закона (категория из конфига, например «право»). Названия могут быть с эмодзи. Дальше вызывай get_channel_content(id) по нужным id."""
        sub = getattr(ctx.bot, "config", None) and getattr(ctx.bot.config, "reference_category_name", None) or "право"
        return get_channels_where_category_contains(ctx.bot, ctx.guild_id, sub)

    async def get_roles_and_members() -> str:
        """Получить все роли сервера и участников в каждой роли: id, name, members (id, display_name). Чтобы знать, кто судья, кто с ПМЖ, кто в совете и т.д."""
        return get_guild_roles_and_members_json(ctx.bot, ctx.guild_id)

    async def get_channel_content(channel_id: int, limit: int = 40) -> str:
        """Прочитать содержимое канала (закреплённые и последние сообщения). channel_id бери из get_channels или get_reference_channels — заходи в те каналы, которые тебе нужны по смыслу (прецеденты, закон)."""
        return await get_channel_content_async(ctx.bot, int(channel_id), limit=min(limit, 50))

    async def send_message_to_channel(channel_id: int, content: str) -> str:
        """Отправить сообщение в канал по его ID (id бери из get_channels)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал с ID {channel_id} не найден."
        try:
            await channel.send(content[:2000])
            return "Сообщение отправлено."
        except Exception as e:
            logger.exception("send_message_to_channel")
            return f"Ошибка отправки: {e!r}"

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
            description="Получить все текстовые каналы сервера (id, name, category_name, topic). Названия как на сервере, могут быть с эмодзи. Сам смотри названия и решай, в какие заходить и куда писать.",
            parameters=build_parameters({}, required=[]),
            execute=get_channels,
        ),
        Tool(
            name="get_channels_in_category",
            description="Найти каналы по подстроке в названии категории (без учёта регистра). Например category_substring «право» найдёт категорию «📜 право» или «Право». Возвращает id, name, category_name — потом get_channel_content(id) чтобы прочитать.",
            parameters=build_parameters({"category_substring": ("string", "Подстрока в названии категории, например право")}, required=["category_substring"]),
            execute=get_channels_in_category,
        ),
        Tool(
            name="get_reference_channels",
            description="Каналы для прецедентов и закона (категория из конфига, например «право»). Названия категорий и каналов как на сервере (могут быть с эмодзи). Дальше get_channel_content(id) по нужным id.",
            parameters=build_parameters({}, required=[]),
            execute=get_reference_channels,
        ),
        Tool(
            name="get_roles_and_members",
            description="Получить все роли сервера и участников в каждой роли (id роли, название, список участников с id и display_name). Чтобы знать, кто судья, кто с ПМЖ, кто в совете, старейшины и т.д.",
            parameters=build_parameters({}, required=[]),
            execute=get_roles_and_members,
        ),
        Tool(
            name="get_channel_content",
            description="Прочитать содержимое канала (закреплённые и последние сообщения). ID бери из get_channels или get_reference_channels — заходи в те каналы, которые нужны по смыслу (прецеденты, закон), и ссылайся на них в ответе.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала из get_channels / get_reference_channels"), "limit": ("integer", "Макс. сообщений (по умолчанию 40)")}, required=["channel_id"]),
            execute=get_channel_content,
        ),
        Tool(
            name="send_message_to_channel",
            description="Отправить сообщение в канал по его ID. ID канала бери из get_channels. Используй для ответа заявителю, уведомления суда/совета, публикации в любой канал.",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала из get_channels"),
                "content": ("string", "Текст сообщения"),
            }),
            execute=send_message_to_channel,
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
