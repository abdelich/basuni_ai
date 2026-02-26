"""
Контекст выполнения инструментов агента: бот (Discord), конфиг (каналы), БД.
Передаётся в инструменты при вызове execute(ctx, **kwargs).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Тип для сессии БД (async context manager или инжектированная сессия)
# Инструменты получают ctx и при необходимости вызывают ctx.get_db() или принимают сессию снаружи.


@dataclass
class AgentContext:
    """Контекст, в котором выполняются инструменты роли."""

    guild_id: int
    """ID гильдии Discord."""
    channel_ids: dict[str, int]
    """Назначение -> ID канала: inbox, decisions, notify_court, ..."""
    bot: Any
    """Экземпляр RoleBot (discord.ext.commands.Bot) для отправки сообщений и т.д."""
    db_session_factory: Any
    """async context manager get_db() для доступа к БД."""
    extra: dict[str, Any]
    """Дополнительные данные (author_id, thread_id, ...)."""

    def get_channel_id(self, purpose: str) -> int | None:
        return self.channel_ids.get(purpose)
