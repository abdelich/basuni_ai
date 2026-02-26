"""
Реестр ролей. Добавление новой роли:
1. Создать папку src/roles/<role_key>/ с bot.py и logic.py
2. Зарегистрировать дескриптор в ROLE_REGISTRY ниже (или через автоматический импорт).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import RoleDescriptor, RoleDeps, RoleBot, register, get_registry

if TYPE_CHECKING:
    pass

# Удобный доступ к реестру (заполняется при load_all_roles()).
ROLE_REGISTRY: dict[str, RoleDescriptor] = {}


def get_role(role_key: str) -> RoleDescriptor | None:
    return ROLE_REGISTRY.get(role_key)


def load_all_roles() -> None:
    """Импортирует все модули ролей, чтобы они зарегистрировались в ROLE_REGISTRY."""
    from . import elder  # noqa: F401
    from . import council  # noqa: F401  # council_1, council_2, council_3
    # В будущем: from . import prosecutor, kpp, judge
    ROLE_REGISTRY.update(get_registry())
