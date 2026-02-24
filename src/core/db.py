"""
Единое подключение к БД. Схема создаётся при старте оркестратора.
Роли работают через общие таблицы, но только с разрешёнными операциями (на уровне логики роли).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# Двигатель и фабрика сессий — инициализируются в init_db
_engine = None
_session_factory = None


def _parse_database_url(url: str) -> str:
    """sqlite:///path -> sqlite+aiosqlite:///path для async."""
    if url.startswith("sqlite"):
        if "+aiosqlite" not in url:
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        # для SQLite создаём директорию
        if "///" in url:
            path = url.split("///")[-1]
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return url


def init_db(database_url: str) -> None:
    """Вызывается оркестратором при старте. Создаёт движок и таблицы."""
    global _engine, _session_factory
    url = _parse_database_url(database_url)
    _engine = create_async_engine(url, echo=False)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    # Модели импортируются при вызове async_init_db, чтобы Base.metadata содержал таблицы
    return None


async def async_init_db() -> None:
    """Создание таблиц. Вызывать после init_db в async-контексте."""
    if _engine is None:
        raise RuntimeError("Сначала вызовите init_db(database_url)")
    import src.core.models  # noqa: F401 — регистрируем таблицы в Base.metadata
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Миграция: поля учёта срока суда (если таблица elder_cases уже существовала)
        for col, typ in [("sent_to_court_at", "DATETIME"), ("court_deadline_hours", "INTEGER")]:
            try:
                await conn.execute(text(f"ALTER TABLE elder_cases ADD COLUMN {col} {typ}"))
            except Exception:
                pass


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Сессия БД для использования в логике ботов."""
    if _session_factory is None:
        raise RuntimeError("Сначала вызовите init_db(database_url)")
    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
