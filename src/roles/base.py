"""
Базовый класс и контракт для роли. Каждая роль — отдельный модуль со своей логикой и промптом.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from discord import Intents
from discord.ext import commands


@dataclass
class RoleDeps:
    """Общие зависимости, которые оркестратор передаёт каждой роли."""
    config: Any  # Config
    db_session_factory: Any  # async context manager get_db
    prompts_dir: Path
    openai_api_key: str | None = None


class RoleBot(commands.Bot, ABC):
    """
    Бот с привязкой к одной конституционной роли.
    Оркестратор создаёт экземпляр через фабрику роли; интенты и префикс задаёт подкласс.
    """

    def __init__(
        self,
        role_key: str,
        deps: RoleDeps,
        command_prefix: str = "!",
        intents: Intents | None = None,
        **kwargs: Any,
    ) -> None:
        intents = intents or Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix=command_prefix, intents=intents, **kwargs)
        self.role_key = role_key
        self.deps = deps

    @property
    def config(self) -> Any:
        return self.deps.config

    @property
    def prompts_dir(self) -> Path:
        return self.deps.prompts_dir

    def load_system_prompt(self) -> str:
        """Загружает системный промпт роли из файла prompts/{role_key}_system.md."""
        path = self.prompts_dir / f"{self.role_key}_system.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def setup_hook(self) -> None:
        """Регистрация когов и т.д. Переопределять в подклассе."""
        await super().setup_hook()


@dataclass
class RoleDescriptor:
    """
    Описание роли для реестра. Оркестратор по enabled_roles загружает дескрипторы
    и создаёт ботов через create_bot.
    """
    role_key: str
    create_bot: Callable[[RoleDeps], RoleBot]
    """Фабрика: по зависимостям создаёт экземпляр бота (токен бот получает при запуске снаружи)."""


def descriptor(role_key: str) -> Callable[[Callable[[RoleDeps], RoleBot]], RoleDescriptor]:
    """Декоратор для регистрации фабрики бота как роли."""
    def wrap(fn: Callable[[RoleDeps], RoleBot]) -> RoleDescriptor:
        return RoleDescriptor(role_key=role_key, create_bot=fn)
    return wrap


# Реестр заполняется при импорте модулей ролей; оркестратор читает из roles.ROLE_REGISTRY
_REGISTRY: dict[str, RoleDescriptor] = {}


def register(d: RoleDescriptor) -> None:
    _REGISTRY[d.role_key] = d


def get_registry() -> dict[str, RoleDescriptor]:
    return _REGISTRY
