"""
Данные гильдии Discord для агентов: каналы, роли, содержимое каналов (прецеденты, закон).
Любой агент может получить список каналов, прочитать категорию «право» и ссылаться на закон.
"""
from __future__ import annotations

import json
from typing import Any

# discord.py: Bot.get_guild(id) -> Guild; Guild.text_channels; Channel.history(), Channel.pins()


def get_guild_channels_json(bot: Any, guild_id: int) -> str:
    """
    Возвращает JSON со всеми текстовыми каналами гильдии: id, name, category_name, topic.
    Названия — как на сервере (в т.ч. с эмодзи). Агент сам решает по названиям, в какие каналы заходить.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена (бот не на сервере или нет кэша)."})
    channels = []
    for ch in guild.text_channels:
        cat_name = ch.category.name if ch.category else None
        channels.append({
            "id": ch.id,
            "name": ch.name,
            "category_name": cat_name,
            "topic": (ch.topic or "")[:200],
        })
    return json.dumps(channels, ensure_ascii=False, indent=0)


def get_channels_where_category_contains(bot: Any, guild_id: int, category_substring: str) -> str:
    """
    Каналы, у которых в названии категории содержится подстрока (без учёта регистра).
    Например категория «📜 право» совпадёт с подстрокой «право». Для всех агентов.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    sub = (category_substring or "").strip().lower()
    out = []
    for ch in guild.text_channels:
        cat_name = (ch.category.name if ch.category else "") or ""
        if sub in cat_name.lower():
            out.append({"id": ch.id, "name": ch.name, "category_name": cat_name})
    return json.dumps(out, ensure_ascii=False, indent=0)


def get_guild_roles_and_members_json(bot: Any, guild_id: int) -> str:
    """
    Возвращает JSON со всеми ролями гильдии и участниками в каждой роли: id, name, members (id, display_name).
    Агент использует это, чтобы знать, кто судья, кто с ПМЖ, кто в совете и т.д.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена (бот не на сервере или нет кэша)."})
    roles_data = []
    for role in guild.roles:
        if role.is_default():
            continue
        members = []
        for m in role.members:
            members.append({"id": m.id, "display_name": m.display_name, "name": m.name})
        roles_data.append({
            "id": role.id,
            "name": role.name,
            "member_count": len(role.members),
            "members": members,
        })
    return json.dumps(roles_data, ensure_ascii=False, indent=0)


async def get_channel_content_async(bot: Any, channel_id: int, limit: int = 40) -> str:
    """
    Читает сообщения из канала (сначала закреплённые, затем последние). Для каналов категории «право» — прецеденты и закон.
    Возвращает текст для ссылки агентом на закон. Общая логика для всех агентов.
    """
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return json.dumps({"error": f"Канал {channel_id} не найден."})
    lines = []
    try:
        pins = await channel.pins()
        for msg in pins[:15]:
            author = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "?")
            content = (msg.content or "").strip()
            if content:
                lines.append(f"[Закреплено | {author}]: {content[:800]}")
        count = 0
        async for msg in channel.history(limit=limit, oldest_first=True):
            if msg.pinned:
                continue
            author = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "?")
            content = (msg.content or "").strip()
            if not content:
                continue
            lines.append(f"[{author}]: {content[:800]}")
            count += 1
            if count >= 35:
                break
    except Exception as e:
        return json.dumps({"error": f"Не удалось прочитать канал: {e!r}"})
    if not lines:
        return "В канале нет сообщений или нет доступа на чтение."
    return "\n---\n".join(lines)
