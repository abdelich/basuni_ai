"""
Данные гильдии Discord для агентов: каналы (с информацией о доступе), роли, содержимое каналов.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# discord.py: Channel.overwrites -> (target, PermissionOverwrite); .read_messages / .view_channel


def _channel_access(ch) -> tuple[list[str], list[str]]:
    """По overwrites канала возвращает (роли с доступом на просмотр, роли с запретом)."""
    viewable, denied = [], []
    try:
        items = ch.overwrites.items() if hasattr(ch.overwrites, "items") else (ch.overwrites or [])
        for target, overwrite in items:
            role_name = getattr(target, "name", None) if target else None
            if not role_name:
                continue
            allow = getattr(overwrite, "allow", None)
            deny = getattr(overwrite, "deny", None)
            if allow is not None:
                if getattr(allow, "read_messages", None) or getattr(allow, "view_channel", None):
                    viewable.append(role_name)
            if deny is not None:
                if getattr(deny, "read_messages", None) or getattr(deny, "view_channel", None):
                    denied.append(role_name)
    except Exception:
        pass
    return (viewable, denied)


def get_guild_channels_json(bot: Any, guild_id: int) -> str:
    """
    Все текстовые каналы: id, name, category_name, topic, viewable_by_roles, denied_for_roles.
    Сначала смотри доступ: не рекомендуй канал, если у обратившегося (по его ролям) нет доступа.
    Названия как на сервере (могут быть с эмодзи).
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена (бот не на сервере или нет кэша)."})
    channels = []
    for ch in guild.text_channels:
        cat_name = ch.category.name if ch.category else None
        viewable, denied = _channel_access(ch)
        channels.append({
            "id": ch.id,
            "name": ch.name,
            "category_name": cat_name,
            "topic": (ch.topic or "")[:200],
            "viewable_by_roles": viewable,
            "denied_for_roles": denied,
        })
    return json.dumps(channels, ensure_ascii=False, indent=0)


def get_channels_where_category_contains(bot: Any, guild_id: int, category_substring: str) -> str:
    """
    Каналы, у которых в названии категории содержится подстрока (без учёта регистра).
    Для каждого канала добавлены viewable_by_roles и denied_for_roles — не рекомендуй канал, если у человека нет доступа.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    sub = (category_substring or "").strip().lower()
    out = []
    for ch in guild.text_channels:
        cat_name = (ch.category.name if ch.category else "") or ""
        if sub in cat_name.lower():
            viewable, denied = _channel_access(ch)
            out.append({
                "id": ch.id,
                "name": ch.name,
                "category_name": cat_name,
                "viewable_by_roles": viewable,
                "denied_for_roles": denied,
            })
    return json.dumps(out, ensure_ascii=False, indent=0)


def get_guild_emojis_json(bot: Any, guild_id: int) -> str:
    """
    Список кастомных эмодзи сервера: name, id, строка для реакции.
    Старейшина может ставить реакции любым из этих эмодзи (особенно на одобрение/неодобрение судей).
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    emojis = []
    for em in guild.emojis:
        # Для реакции в discord.py передаётся объект Emoji или строка :name:id
        emojis.append({
            "name": em.name,
            "id": em.id,
            "reaction_string": str(em),  # <:name:id> для кастомных
            "animated": getattr(em, "animated", False),
        })
    return json.dumps(emojis, ensure_ascii=False, indent=0)


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


async def get_member_roles_json_async(bot: Any, guild_id: int, query: str) -> str:
    """
    То же, что get_member_roles_json, но при поиске по ID подгружает участника через API (fetch_member),
    если его нет в кэше — чтобы роли всегда были актуальны.
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "Укажи никнейм, имя или ID участника."})
    if q.isdigit():
        uid = int(q)
        member = None
        try:
            member = await guild.fetch_member(uid)
        except Exception as e:
            logger.warning(
                "fetch_member(%s) не удался: %s. Включи «Server Members Intent» у бота в Discord Developer Portal → Bot.", uid, e
            )
            member = guild.get_member(uid)
        if member and not getattr(member, "bot", False):
            role_names = [r.name for r in member.roles if not getattr(r, "is_default", False)]
            return json.dumps([{
                "id": member.id,
                "display_name": getattr(member, "display_name", None) or member.name,
                "name": member.name,
                "roles": role_names,
            }], ensure_ascii=False, indent=0)
        return json.dumps({"error": f"Участник с ID {q} не найден или это бот."})
    return get_member_roles_json(bot, guild_id, q)


def get_member_roles_json(bot: Any, guild_id: int, query: str) -> str:
    """
    Найти участника по запросу (никнейм, display_name, имя пользователя или Discord ID) и вернуть его роли.
    query: число — поиск по ID; иначе — подстрока в display_name или name (без учёта регистра).
    Возвращает список совпадений: id, display_name, name, roles (названия ролей, без @everyone).
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "Укажи никнейм, имя или ID участника."})

    matches = []
    # Поиск по числовому ID
    if q.isdigit():
        member = guild.get_member(int(q))
        if member and not getattr(member, "bot", False):
            role_names = [r.name for r in member.roles if not getattr(r, "is_default", False)]
            matches.append({
                "id": member.id,
                "display_name": getattr(member, "display_name", None) or member.name,
                "name": member.name,
                "roles": role_names,
            })
        if not matches:
            return json.dumps({"error": f"Участник с ID {q} не найден или это бот."})
        return json.dumps(matches, ensure_ascii=False, indent=0)

    # Поиск по подстроке в display_name и name
    q_lower = q.lower()
    for member in guild.members:
        if getattr(member, "bot", False):
            continue
        dn = (getattr(member, "display_name", None) or "") or ""
        nm = (getattr(member, "name", None) or "") or ""
        if q_lower in dn.lower() or q_lower in nm.lower():
            role_names = [r.name for r in member.roles if not getattr(r, "is_default", False)]
            matches.append({
                "id": member.id,
                "display_name": dn or nm,
                "name": nm,
                "roles": role_names,
            })
    if not matches:
        return json.dumps({"error": f"Никто не найден по запросу «{q}» (никнейм или имя)."})
    return json.dumps(matches, ensure_ascii=False, indent=0)


async def get_channel_content_async(
    bot: Any,
    channel_id: int,
    limit: int = 40,
    as_law_document: bool = False,
) -> str:
    """
    Читает сообщения из канала (сначала закреплённые, затем последние).
    as_law_document=True: для каналов закона — без префиксов автора, сообщения склеиваются в один текст по порядку,
    чтобы «Статья 5» и «Часть 3» в разных сообщениях читались как один документ. Увеличивается лимит сообщений.
    """
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return json.dumps({"error": f"Канал {channel_id} не найден."})
    if as_law_document:
        limit = min(limit, 120)
    lines = []
    try:
        pins = await channel.pins()
        for msg in pins[:20] if as_law_document else pins[:15]:
            content = (msg.content or "").strip()
            if content:
                if as_law_document:
                    lines.append(content[:1200])
                else:
                    author = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "?")
                    lines.append(f"[Закреплено | {author}]: {content[:800]}")
        count = 0
        async for msg in channel.history(limit=limit, oldest_first=True):
            if msg.pinned:
                continue
            content = (msg.content or "").strip()
            if not content:
                continue
            if as_law_document:
                lines.append(content[:1200])
            else:
                author = getattr(msg.author, "display_name", None) or getattr(msg.author, "name", "?")
                lines.append(f"[{author}]: {content[:800]}")
            count += 1
            if not as_law_document and count >= 35:
                break
    except Exception as e:
        return json.dumps({"error": f"Не удалось прочитать канал: {e!r}"})
    if not lines:
        return "В канале нет сообщений или нет доступа на чтение."
    if as_law_document:
        return "\n\n".join(lines)
    return "\n---\n".join(lines)


async def get_all_reference_channel_contents_async(
    bot: Any,
    guild_id: int,
    category_substring: str = "право",
    limit_per_channel: int = 40,
    as_law_document: bool = False,
) -> str:
    """
    Получить и прочитать содержимое всех текстовых каналов из категории, в названии которой есть
    category_substring. as_law_document=True: сообщения без префикса автора, склеены как один документ
    (статьи и части в разных сообщениях читаются подряд) — для точного поиска «статья N часть M».
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        return json.dumps({"error": "Гильдия не найдена."})
    sub = (category_substring or "право").strip().lower()
    channels = []
    for ch in guild.text_channels:
        cat_name = (ch.category.name if ch.category else "") or ""
        if sub in cat_name.lower():
            channels.append({"id": ch.id, "name": ch.name})
    if not channels:
        return "В категории с подстрокой «право» в названии нет текстовых каналов."
    lim = min(limit_per_channel, 120) if as_law_document else min(limit_per_channel, 50)
    parts = []
    for c in channels:
        content = await get_channel_content_async(bot, c["id"], limit=lim, as_law_document=as_law_document)
        if content.startswith("{") and "error" in content:
            parts.append(f"=== {c['name']} (id: {c['id']}) ===\n[не удалось прочитать: {content}]")
        else:
            parts.append(f"=== {c['name']} (id: {c['id']}) ===\n{content}")
    return "\n\n".join(parts)


def _member_roles_to_block(
    author_id: int,
    author_display_name: str,
    role_names: list[str],
    raw_json: str,
) -> tuple[str, list[str]]:
    """Собирает блок «КОМУ ТЫ ОТВЕЧАЕШЬ» из уже известных ролей и сырого JSON."""
    name = author_display_name or str(author_id)
    roles_str = ", ".join(role_names) if role_names else "нет ролей (только @everyone)"
    block = (
        f"[ КОМУ ТЫ ОТВЕЧАЕШЬ: {name} (id: {author_id}). "
        f"Роли на сервере: **{roles_str}**. "
        f"По этим ролям определяются полномочия обратившегося — формируй ответ с учётом того, с кем общаешься. ]\n"
        f"Ответ сервера get_member_roles(id={author_id}): {raw_json}\n"
    )
    return (block, role_names)


async def get_author_roles_block_async(
    bot: Any,
    guild_id: int,
    author_id: int,
    author_display_name: str = "",
    member: Any = None,
) -> tuple[str, list[str]]:
    """
    Для каждого агента: роли автора сообщения и блок для контекста.
    Если передан member (discord.Member), роли берутся из него — так данные всегда точны и не зависят от fetch_member/Intent.
    Иначе запрашиваются через get_member_roles_json_async.
    Возвращает (текст_блока, список_названий_ролей).
    """
    role_names: list[str] = []
    raw_json: str
    if member is not None and hasattr(member, "roles"):
        def _skip_role(role: Any) -> bool:
            v = getattr(role, "is_default", None)
            if callable(v):
                return v()
            return getattr(role, "name", "") == "@everyone"
        role_names = [r.name for r in member.roles if not _skip_role(r)]
        raw_json = json.dumps([{
            "id": member.id,
            "display_name": getattr(member, "display_name", None) or getattr(member, "name", ""),
            "name": getattr(member, "name", ""),
            "roles": role_names,
        }], ensure_ascii=False, indent=0)
        return _member_roles_to_block(author_id, author_display_name or (getattr(member, "display_name", None) or getattr(member, "name", "") or str(author_id)), role_names, raw_json)
    raw_json = await get_member_roles_json_async(bot, guild_id, str(author_id))
    try:
        data = json.loads(raw_json)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            rec = data[0]
            role_names = list(rec.get("roles") or [])
        elif isinstance(data, dict) and data.get("error"):
            logger.warning("Роли автора %s: %s", author_id, data.get("error"))
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return _member_roles_to_block(author_id, author_display_name or str(author_id), role_names, raw_json)


# Подписи для приоритетных каналов закона (база и судебные прецеденты) — в конфиге задаются ID
_LAW_PRIORITY_LABELS = ("База (гос-образующие прецеденты)", "Судебные прецеденты")


def _law_block_hint() -> str:
    return (
        "Закон и прецеденты (действуй только по закону). "
        "Блок собран из двух каналов закона (конфиг: law_base_precedents, law_judicial_precedents) и остальных каналов категории «право». "
        "На запрос про статью N (или часть M) ищи в тексте ниже именно эту статью/часть и отвечай только по этому тексту. "
        "Если не нашёл — используй инструменты get_all_law_channel_contents() или get_channel_content(channel_id). "
        "Не сообщай гражданину о сбоях загрузки каналов («не удалось получить доступ», «не удалось прочитать») — действуй по процедуре по имеющимся данным.\n\n"
    )


async def get_law_block_async(
    bot: Any,
    guild_id: int,
    max_chars: int = 10000,
    reference_category_name: str = "право",
    config: Any = None,
) -> str:
    """
    Текст закона/прецедентов для контекста агента. Все агенты должны всегда смотреть на закон и действовать только по нему.
    Если передан config с law_channel_ids() — сначала загружаются эти каналы (база, судебные прецеденты), затем остальные из категории «право».
    """
    hint = _law_block_hint()
    parts: list[str] = []
    priority_ids: list[int] = []
    if config is not None and hasattr(config, "law_channel_ids") and callable(getattr(config, "law_channel_ids")):
        priority_ids = list(config.law_channel_ids())
    law_limit = 100
    for i, ch_id in enumerate(priority_ids):
        label = _LAW_PRIORITY_LABELS[i] if i < len(_LAW_PRIORITY_LABELS) else f"Канал {ch_id}"
        ch_content = await get_channel_content_async(bot, ch_id, limit=law_limit, as_law_document=True)
        if ch_content.startswith("{") and "error" in ch_content or ch_content.strip() == "В канале нет сообщений или нет доступа на чтение.":
            parts.append(f"=== {label} (id: {ch_id}) ===\n[по каналу данных нет — действуй по процедуре по остальнему контексту]")
        else:
            ch_name = ""
            ch = bot.get_channel(ch_id) if hasattr(bot, "get_channel") else None
            if ch and getattr(ch, "name", None):
                ch_name = getattr(ch, "name", "")
            parts.append(f"=== {label}" + (f" — {ch_name}" if ch_name else "") + f" (id: {ch_id}) ===\n{ch_content}")
    rest = await get_all_reference_channel_contents_async(
        bot, guild_id, reference_category_name, limit_per_channel=law_limit, as_law_document=True
    )
    if rest.startswith("{") and "error" in rest:
        if not parts:
            content = "По каналам права данных нет. Действуй по процедуре: при заявке на законопроект/референдум и согласии гражданина сразу вызывай publish_decision, notify_court, record_case_sent_to_court. В ответе гражданину не упоминай недоступность каналов."
            return hint + content
    else:
        if priority_ids:
            rest_ids = set(priority_ids)
            guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else None
            if guild:
                sub = (reference_category_name or "право").strip().lower()
                other_parts = []
                for ch in guild.text_channels:
                    if not ch.category or sub not in (ch.category.name or "").lower():
                        continue
                    if ch.id in rest_ids:
                        continue
                    cnt = await get_channel_content_async(bot, ch.id, limit=law_limit, as_law_document=True)
                    if cnt.startswith("{") and "error" in cnt or (cnt.strip() == "В канале нет сообщений или нет доступа на чтение."):
                        other_parts.append(f"=== {ch.name} (id: {ch.id}) ===\n[по каналу данных нет — действуй по процедуре]")
                    else:
                        other_parts.append(f"=== {ch.name} (id: {ch.id}) ===\n{cnt}")
                if other_parts:
                    parts.append("=== Остальные каналы категории «право» ===\n" + "\n\n".join(other_parts))
        else:
            parts.append(rest)
    content = "\n\n".join(parts)
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[... обрезано ...]"
    return hint + content
