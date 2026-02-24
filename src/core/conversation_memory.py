"""
Память переписок агентов: сохранение и загрузка по контексту (канал, тред, роль).
"""
from __future__ import annotations

from sqlalchemy import select

from .db import get_db
from .models import AgentConversationMessage


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
    """Сохранить одно сообщение в историю переписки."""
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
