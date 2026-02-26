"""
Оркестратор: загрузка конфига, инициализация БД, запуск всех включённых ролей.
Каждый бот — отдельный процесс/таск с собственным токеном.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from src.core.config import load_config
from src.core.db import get_db, init_db, async_init_db
from src.roles import ROLE_REGISTRY, load_all_roles
from src.roles.base import RoleDeps

logger = logging.getLogger("basuni.orchestrator")


def _mask_api_key(key: str | None) -> str:
    """Показать ключ для проверки: начало и конец, середина скрыта."""
    if not key or not key.strip():
        return "(не задан)"
    k = key.strip()
    if len(k) <= 12:
        return f"{k[:4]}... (всего {len(k)} символов)"
    return f"{k[:7]}...{k[-4:]}"


def run(config_path: Path | None = None) -> None:
    """Синхронная точка входа: загружает конфиг, инициализирует БД, запускает asyncio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    config = load_config(config_path)
    # Ключ OpenAI берётся из конфига (читает .env: OPENAI_API_KEY)
    api_key = config.openai_api_key
    print(f"[basuni] OpenAI API key (из конфига/.env): {_mask_api_key(api_key)}")
    logger.info("OpenAI API key (из конфига): %s", _mask_api_key(api_key))
    init_db(config.database_url)
    load_all_roles()

    asyncio.run(_run_bots(config))


async def _run_bots(config) -> None:
    """Запуск всех ботов из enabled_roles в одном event loop."""
    await async_init_db()

    project_root = Path(__file__).resolve().parents[2]
    prompts_dir = project_root / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    deps = RoleDeps(
        config=config,
        db_session_factory=get_db,
        prompts_dir=prompts_dir,
        openai_api_key=config.openai_api_key,
    )

    tasks = []
    for role_key in config.enabled_roles:
        descriptor = ROLE_REGISTRY.get(role_key)
        if not descriptor:
            logger.warning("Роль %s не найдена в реестре, пропуск.", role_key)
            continue
        try:
            token = config.token_for_role(role_key)
        except RuntimeError as e:
            logger.error("Роль %s: %s", role_key, e)
            continue
        bot = descriptor.create_bot(deps)
        logger.info("Запуск бота: %s", role_key)
        tasks.append(bot.start(token))

    if not tasks:
        logger.error("Нет ботов для запуска. Проверьте enabled_roles и токены в .env")
        return

    await asyncio.gather(*tasks)
