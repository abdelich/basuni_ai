"""
Память переписок агентов: сохранение и загрузка по контексту (канал, тред, роль).
Ветки разговоров: контекст «о чём речь» по каждой ветке (канал/тред/автор) и общий обзор по каналам.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .db import get_db
from .models import AgentConversationMessage, AgentConversationBranch


def _branch_key(guild_id: int, role_key: str, channel_id: int, thread_id: int | None, author_id: int | None) -> tuple:
    return (guild_id, role_key, channel_id, thread_id or 0, author_id or 0)


async def save_message(
    role_key: str,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    case_id: int | None,
    discord_message_id: int | None,
    author_id: int | None,
    author_display_name: str | None,
    role: str,
    content: str,
) -> None:
    """Сохранить одно сообщение в историю переписки и обновить last_activity_at ветки."""
    async with get_db() as session:
        msg = AgentConversationMessage(
            guild_id=guild_id,
            role_key=role_key,
            channel_id=channel_id,
            thread_id=thread_id,
            case_id=case_id,
            discord_message_id=discord_message_id,
            author_id=author_id,
            author_display_name=author_display_name,
            role=role,
            content=content,
        )
        session.add(msg)
        now = datetime.now(timezone.utc)
        await _upsert_branch_activity(session, role_key, guild_id, channel_id, thread_id, author_id, case_id, now)


async def load_recent_messages(
    role_key: str,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    limit: int = 24,
    author_id: int | None = None,
) -> list[dict[str, str]]:
    """
    Загрузить последние сообщения переписки для контекста (канал/тред).
    Если задан author_id — только диалог с этим пользователем (его реплики и ответы старейшины ему),
    чтобы контекст был разный для разных людей.
    Возвращает список {"role": "user"|"assistant", "content": "..."} для передачи в LLM.
    """
    async with get_db() as session:
        q = (
            select(AgentConversationMessage)
            .where(
                AgentConversationMessage.guild_id == guild_id,
                AgentConversationMessage.role_key == role_key,
                AgentConversationMessage.channel_id == channel_id,
                AgentConversationMessage.thread_id == thread_id,
            )
            .order_by(AgentConversationMessage.created_at.asc())
        )
        result = await session.execute(q)
        rows = result.scalars().all()
    if author_id is not None:
        # Оставляем только диалог с этим автором: его реплики и ответы ассистента сразу после них
        filtered: list[AgentConversationMessage] = []
        for i, r in enumerate(rows):
            if r.role == "user" and r.author_id == author_id:
                filtered.append(r)
            elif r.role == "assistant" and i > 0 and rows[i - 1].role == "user" and rows[i - 1].author_id == author_id:
                filtered.append(r)
        rows = filtered[-limit * 2 :] if len(filtered) > limit * 2 else filtered
    else:
        rows = rows[-limit:]
    out = []
    for r in rows:
        out.append({"role": r.role, "content": r.content or ""})
    return out


async def _upsert_branch_activity(
    session, role_key: str, guild_id: int, channel_id: int, thread_id: int | None,
    author_id: int | None, case_id: int | None, at: datetime,
) -> None:
    """Обновить или создать запись ветки и сдвинуть last_activity_at."""
    q = select(AgentConversationBranch).where(
        AgentConversationBranch.guild_id == guild_id,
        AgentConversationBranch.role_key == role_key,
        AgentConversationBranch.channel_id == channel_id,
        AgentConversationBranch.thread_id == (thread_id if thread_id else None),
        AgentConversationBranch.author_id == (author_id if author_id else None),
    )
    result = await session.execute(q)
    branch = result.scalars().one_or_none()
    if branch:
        branch.last_activity_at = at
        branch.updated_at = at
        if case_id is not None:
            branch.current_case_id = case_id
    else:
        session.add(AgentConversationBranch(
            guild_id=guild_id,
            role_key=role_key,
            channel_id=channel_id,
            thread_id=thread_id,
            author_id=author_id,
            current_case_id=case_id,
            last_activity_at=at,
            updated_at=at,
        ))


async def save_branch_summary(
    role_key: str,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    author_id: int | None,
    summary: str,
    case_id: int | None = None,
) -> None:
    """Сохранить краткий контекст ветки «о чём речь». Вызывать при разборе ответа агента (строка КОНТЕКСТ:)."""
    async with get_db() as session:
        q = select(AgentConversationBranch).where(
            AgentConversationBranch.guild_id == guild_id,
            AgentConversationBranch.role_key == role_key,
            AgentConversationBranch.channel_id == channel_id,
            AgentConversationBranch.thread_id == (thread_id if thread_id else None),
            AgentConversationBranch.author_id == (author_id if author_id else None),
        )
        result = await session.execute(q)
        branch = result.scalars().one_or_none()
        now = datetime.now(timezone.utc)
        if branch:
            branch.summary = (summary or "").strip()[:2000]
            branch.updated_at = now
            if case_id is not None:
                branch.current_case_id = case_id
        else:
            session.add(AgentConversationBranch(
                guild_id=guild_id,
                role_key=role_key,
                channel_id=channel_id,
                thread_id=thread_id,
                author_id=author_id,
                summary=(summary or "").strip()[:2000],
                current_case_id=case_id,
                last_activity_at=now,
                updated_at=now,
            ))


async def load_branch_summary(
    role_key: str,
    guild_id: int,
    channel_id: int,
    thread_id: int | None,
    author_id: int | None,
) -> tuple[str | None, int | None]:
    """Загрузить сохранённый контекст ветки: (summary, current_case_id)."""
    async with get_db() as session:
        q = select(AgentConversationBranch).where(
            AgentConversationBranch.guild_id == guild_id,
            AgentConversationBranch.role_key == role_key,
            AgentConversationBranch.channel_id == channel_id,
            AgentConversationBranch.thread_id == (thread_id if thread_id else None),
            AgentConversationBranch.author_id == (author_id if author_id else None),
        )
        result = await session.execute(q)
        branch = result.scalars().one_or_none()
        if not branch:
            return (None, None)
        return (branch.summary, branch.current_case_id)


async def load_all_branch_summaries(
    role_key: str,
    guild_id: int,
    limit: int = 15,
    channel_names: dict[int, str] | None = None,
) -> list[dict]:
    """
    Загрузить последние активные ветки (канал/тред/автор) с их контекстом для общего обзора.
    channel_names: опционально {channel_id: "название"} для читаемого вывода.
    Возвращает список {"channel_id", "thread_id", "author_id", "author_display_name", "summary", "current_case_id", "last_activity_at"}.
    """
    async with get_db() as session:
        q = (
            select(AgentConversationBranch)
            .where(
                AgentConversationBranch.guild_id == guild_id,
                AgentConversationBranch.role_key == role_key,
            )
            .order_by(AgentConversationBranch.last_activity_at.desc())
            .limit(limit * 2)
        )
        result = await session.execute(q)
        rows = result.scalars().all()
    out = []
    for r in rows[:limit]:
        ch_name = (channel_names or {}).get(r.channel_id) if channel_names else None
        out.append({
            "channel_id": r.channel_id,
            "channel_name": ch_name or str(r.channel_id),
            "thread_id": r.thread_id,
            "author_id": r.author_id,
            "summary": r.summary or "(нет контекста)",
            "current_case_id": r.current_case_id,
            "last_activity_at": r.last_activity_at,
        })
    return out
