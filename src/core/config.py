"""
Единая загрузка конфигурации: .env (секреты) + YAML (структура).
Роли, каналы, ID — в YAML; токены и API-ключи — в .env.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Загружаем .env из корня проекта
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")


def _env(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None or val == "":
        raise RuntimeError(f"Не задана переменная окружения: {key}")
    return val


def _env_optional(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default) or default


class Config:
    """Единый объект конфигурации для всех ботов и оркестратора."""

    def __init__(self, raw: dict[str, Any], env_prefix: str = "BASUNI") -> None:
        self._raw = raw
        self._env_prefix = env_prefix

    # --- Секреты из .env ---
    @property
    def database_url(self) -> str:
        url = _env_optional("DATABASE_URL")
        if url:
            return url
        path = (_PROJECT_ROOT / "data" / "basuni.db").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    def token_for_role(self, role_key: str) -> str:
        """Токен Discord бота для роли. Ключ: elder, council, prosecutor, kpp, judge."""
        key = f"DISCORD_TOKEN_{role_key.upper()}"
        return _env(key)

    @property
    def openai_api_key(self) -> str | None:
        return _env_optional("OPENAI_API_KEY")

    @property
    def openai_model(self) -> str:
        """Модель OpenAI для агентов. По умолчанию gpt-4o-mini."""
        return _env_optional("OPENAI_MODEL") or self._raw.get("openai_model") or "gpt-4o-mini"

    # --- Структура из YAML ---
    @property
    def guild_id(self) -> int:
        return int(self._raw.get("guild_id", 0))

    def channels(self) -> dict[str, int]:
        """Каналы по ключам (elder_inbox, court_inbox, ...). ID в int."""
        ch = self._raw.get("channels", {})
        return {k: int(v) for k, v in ch.items()}

    def role_ids(self) -> dict[str, int]:
        """ID ролей Discord: pmj, vnj, elder, council, prosecutor, kpp, judge, ..."""
        r = self._raw.get("role_ids", {})
        return {k: int(v) for k, v in r.items()}

    def role_config(self, role_key: str) -> dict[str, Any]:
        """Конфиг конкретной роли (каналы, права и т.д.)."""
        roles = self._raw.get("roles", {})
        return roles.get(role_key, {})

    def channel_for_role(self, role_key: str, purpose: str) -> int | None:
        """
        ID канала для роли и назначения.
        purpose: inbox (заявки от граждан), decisions (публикация решений),
                 outbox (ответы по умолчанию), notify_court, notify_council, referrals, ...
        В YAML: roles.<role>.inbox_channel_key, decisions_channel_key и т.д.
        """
        rcfg = self.role_config(role_key)
        key_map = {
            "inbox": rcfg.get("inbox_channel_key") or f"{role_key}_inbox",
            "decisions": rcfg.get("decisions_channel_key") or f"{role_key}_decisions",
            "outbox": rcfg.get("outbox_channel_key") or rcfg.get("inbox_channel_key") or f"{role_key}_inbox",
            "notify_court": rcfg.get("notify_court_channel_key") or "court_inbox",
            "notify_council": rcfg.get("notify_council_channel_key") or "council_inbox",
            "referrals": rcfg.get("referrals_channel_key") or "referrals",
        }
        channel_key = key_map.get(purpose) or purpose
        channels = self.channels()
        return channels.get(channel_key) if isinstance(channel_key, str) else None

    def watch_channel_ids(self, role_key: str) -> list[int]:
        """ID каналов, которые роль отслеживает для надзора за легитимностью (например суд, совет). В YAML: roles.<role>.watch_channel_keys."""
        rcfg = self.role_config(role_key)
        keys = rcfg.get("watch_channel_keys") or []
        ch = self.channels()
        return [ch[k] for k in keys if isinstance(k, str) and ch.get(k)]

    @property
    def reference_category_name(self) -> str:
        """Название категории Discord, в которой лежат прецеденты и закон (подканалы). Все агенты читают её и действуют только по закону."""
        return self._raw.get("reference_category_name") or "право"

    def law_channel_ids(self) -> list[int]:
        """
        ID каналов закона в порядке приоритета: [база (гос-образующие прецеденты), судебные прецеденты].
        Ключи задаются в law_channels.base_precedents_key и law_channels.judicial_precedents_key;
        значения — ключи из channels. Если ID = 0, канал пропускается. Все агенты должны опираться на эти каналы.
        """
        law = self._raw.get("law_channels") or {}
        ch = self.channels()
        out: list[int] = []
        for key in ("base_precedents_key", "judicial_precedents_key"):
            k = law.get(key)
            if k and ch.get(k):
                cid = ch[k]
                if cid and int(cid) != 0:
                    out.append(int(cid))
        return out

    @property
    def enabled_roles(self) -> list[str]:
        """Список ролей, которые оркестратор должен запустить."""
        return self._raw.get("enabled_roles", ["elder"])

    def get(self, key: str, default: Any = None) -> Any:
        return self._raw.get(key, default)


def load_config(config_path: Path | None = None) -> Config:
    """Загружает YAML и возвращает Config. Путь по умолчанию: config/default.yaml."""
    path = config_path or (_PROJECT_ROOT / "config" / "default.yaml")
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    return Config(raw)
