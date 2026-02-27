"""
Инструменты совета: закон, каналы, голосование и полное управление сервером (роли, каналы, участники, сообщения, треды, инвайты и т.д.).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord  # type: ignore[reportMissingImports]
from discord import Object, PermissionOverwrite  # type: ignore[reportMissingImports]
from sqlalchemy import select  # type: ignore[reportMissingImports]

from src.core.tools import Tool, build_parameters
from src.core.agent_ctx import AgentContext
from src.core.models import CouncilCase, CouncilVote
from src.core.discord_guild import (
    get_channel_content_async,
    get_law_block_async,
    get_guild_roles_and_members_json,
)

logger = logging.getLogger("basuni.council.tools")


def make_council_tools(ctx: AgentContext, member_index: int, *, execution_mode: bool = False) -> list[Tool]:
    """Создаёт инструменты для члена совета (member_index 1, 2 или 3).

    execution_mode=False (deliberation): только чтение + голосование.
    execution_mode=True (исполнение): полный набор серверных инструментов.
    """

    async def get_law(max_chars: int = 12000) -> str:
        """Получить текст закона из двух каналов права (база и судебные прецеденты). Ориентируйся только на этот текст."""
        return await get_law_block_async(
            ctx.bot,
            ctx.guild_id,
            max_chars=max_chars,
            reference_category_name=getattr(ctx.bot.config, "reference_category_name", None) or "право",
            config=ctx.bot.config,
        )

    async def get_council_inbox_recent(limit: int = 25) -> str:
        """Последние сообщения из канала совета (council_inbox) — поручения от старейшин."""
        ch_id = ctx.get_channel_id("inbox")
        if not ch_id:
            return "Канал council_inbox не настроен."
        return await get_channel_content_async(ctx.bot, ch_id, limit=min(limit, 50))

    async def get_court_decisions_recent(limit: int = 25) -> str:
        """Последние сообщения из канала решений суда (court_decisions). Если нужно исполнить — совет голосует и исполняет."""
        ch_id = ctx.get_channel_id("court_decisions")
        if not ch_id:
            return "Канал решений суда (court_decisions) не настроен."
        return await get_channel_content_async(ctx.bot, ch_id, limit=min(limit, 50))

    async def get_council_case(case_id: str) -> str:
        """Получить дело совета по номеру: содержание, статус, уже отданные голоса (кто за/против)."""
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: case_id — число."
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(CouncilCase).where(CouncilCase.id == cid, CouncilCase.guild_id == ctx.guild_id)
            )
            case = result.scalars().one_or_none()
            if not case:
                return f"Дело совета №{case_id} не найдено."
            result_v = await session.execute(
                select(CouncilVote).where(CouncilVote.case_id == cid).order_by(CouncilVote.created_at.asc())
            )
            votes = result_v.scalars().all()
        votes_text = "; ".join(
            f"Член {v.member_index}: {'За' if v.vote == 'yes' else 'Против'}" + (f" — {v.deliberation_text[:80]}…" if v.deliberation_text and len(v.deliberation_text) > 80 else (f" — {v.deliberation_text}" if v.deliberation_text else ""))
            for v in votes
        ) or "Голосов пока нет."
        return (
            f"Дело №{case.id}: source={case.source}, status={case.status}, content={case.content[:500] if case.content else ''}…\n"
            f"Голоса: {votes_text}"
        )

    async def post_my_deliberation(case_id: str, thoughts: str, vote: str) -> str:
        """Опубликовать свою позицию и голос по делу в канал обсуждений совета. vote — только «yes» (За) или «no» (Против); воздержаний нет. Вызови один раз по делу."""
        vote_clean = (vote or "").strip().lower()
        if vote_clean not in ("yes", "no"):
            return "Ошибка: vote должен быть «yes» (За) или «no» (Против). Воздержаний нет."
        try:
            cid = int(case_id)
        except (ValueError, TypeError):
            return "Ошибка: case_id — число."
        ch_id = ctx.get_channel_id("deliberations")
        if not ch_id:
            return "Канал обсуждений совета (council_deliberations) не настроен."
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(CouncilVote).where(
                    CouncilVote.case_id == cid,
                    CouncilVote.member_index == member_index,
                )
            )
            if result.scalars().first():
                return "Ты уже проголосовал по этому делу; повторный голос не допускается."
            result_c = await session.execute(
                select(CouncilCase).where(CouncilCase.id == cid, CouncilCase.guild_id == ctx.guild_id)
            )
            case = result_c.scalars().one_or_none()
            if not case:
                return f"Дело совета №{case_id} не найдено."
            vote_label = "За" if vote_clean == "yes" else "Против"
            session.add(CouncilVote(
                case_id=cid,
                guild_id=ctx.guild_id,
                member_index=member_index,
                vote=vote_clean,
                deliberation_text=(thoughts or "")[:2000],
            ))
            await session.commit()
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return "Голос записан в БД, но канал обсуждений не найден для поста."
        text = f"**Член совета {member_index}:** {thoughts[:1500] if thoughts else '(без комментария)'}\n**Голос: {vote_label}**"
        try:
            await channel.send(text[:2000])
            logger.info("Совет → [#%s]: %s", getattr(channel, "name", "?"), text[:150].replace("\n", " "))
        except Exception as e:
            logger.exception("post_my_deliberation send")
            return f"Голос записан в БД. Ошибка отправки в канал: {e!r}"
        return "Позиция и голос опубликованы в канал обсуждений и записаны в БД."

    async def list_council_cases(status: str = "open") -> str:
        """Список дел совета по статусу: open, voting_done, approved, rejected, executed."""
        async with ctx.db_session_factory() as session:
            result = await session.execute(
                select(CouncilCase)
                .where(CouncilCase.guild_id == ctx.guild_id, CouncilCase.status == status)
                .order_by(CouncilCase.created_at.desc())
                .limit(30)
            )
            cases = result.scalars().all()
        if not cases:
            return f"Дел со статусом «{status}» нет."
        lines = [f"№{c.id} source={c.source} status={c.status} — {(c.content or '')[:120]}…" for c in cases]
        return "\n".join(lines)

    async def get_roles_and_members() -> str:
        """Роли сервера и участники в каждой роли (для исполнения: кому выдать роль, кого кикнуть и т.д.)."""
        return get_guild_roles_and_members_json(ctx.bot, ctx.guild_id)

    async def send_message_to_channel(channel_id: int, content: str) -> str:
        """Отправить сообщение в канал по ID."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            text = (content or "")[:2000]
            await channel.send(text)
            logger.info("Совет → [#%s]: %s", getattr(channel, "name", "?"), text[:150].replace("\n", " "))
            return "Сообщение отправлено."
        except Exception as e:
            return f"Ошибка: {e!r}"

    # Исполнение: только после одобрения советом (проверка в коде бота или через статус дела)
    async def add_role_to_member(member_id: int, role_id: int) -> str:
        """Выдать участнику роль по Discord ID участника и ID роли."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        role = guild.get_role(int(role_id))
        if not role:
            return f"Роль {role_id} не найдена."
        try:
            await member.add_roles(role)
            return f"Роль {role.name} выдана участнику {member.display_name}."
        except Exception as e:
            return f"Ошибка выдачи роли: {e!r}"

    async def remove_role_from_member(member_id: int, role_id: int) -> str:
        """Снять роль у участника."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        role = guild.get_role(int(role_id))
        if not role:
            return f"Роль {role_id} не найдена."
        try:
            await member.remove_roles(role)
            return f"Роль {role.name} снята у участника {member.display_name}."
        except Exception as e:
            return f"Ошибка снятия роли: {e!r}"

    async def timeout_member(member_id: int, duration_minutes: int) -> str:
        """Таймаут (mute) участника на duration_minutes минут."""
        from datetime import timedelta
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        try:
            duration = timedelta(minutes=max(1, min(duration_minutes, 40320)))  # max 28 days
            await member.timeout(duration, reason="По решению совета")
            return f"Таймаут участнику {member.display_name} на {duration_minutes} мин."
        except Exception as e:
            return f"Ошибка таймаута: {e!r}"

    async def kick_member(member_id: int, reason: str = "") -> str:
        """Исключить участника с сервера (kick, не бан)."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        try:
            await member.kick(reason=(reason or "По решению совета")[:512])
            return f"Участник {member.display_name} исключён (kick)."
        except Exception as e:
            return f"Ошибка kick: {e!r}"

    async def ban_member(member_id: int, reason: str = "", delete_message_days: int = 0) -> str:
        """Забанить участника по Discord ID. delete_message_days — удалить сообщения за последние 0–7 дней."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            reason_str = (reason or "По решению совета")[:512]
            days = min(7, max(0, int(delete_message_days))) if delete_message_days else 0
            kwargs = {"reason": reason_str}
            if days > 0:
                kwargs["delete_message_seconds"] = days * 86400
            await guild.ban(Object(id=int(member_id)), **kwargs)
            return f"Участник {member_id} забанен."
        except Exception as e:
            return f"Ошибка бана: {e!r}"

    async def unban_member(user_id: int, reason: str = "") -> str:
        """Разбанить пользователя по Discord ID."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            await guild.unban(Object(id=int(user_id)), reason=(reason or "По решению совета")[:512])
            return f"Пользователь {user_id} разбанен."
        except Exception as e:
            return f"Ошибка разбана: {e!r}"

    async def remove_timeout(member_id: int) -> str:
        """Снять таймаут (mute) с участника."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        try:
            await member.timeout(None, reason="По решению совета")
            return f"Таймаут снят с {member.display_name}."
        except Exception as e:
            return f"Ошибка снятия таймаута: {e!r}"

    async def set_member_nick(member_id: int, nick: str) -> str:
        """Изменить никнейм участника на сервере. Пустая строка — сбросить на дефолтный."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        try:
            await member.edit(nick=(nick or None)[:32] if nick else None)
            return f"Ник участника установлен: {nick or '(сброшен)'}."
        except Exception as e:
            return f"Ошибка смены ника: {e!r}"

    _created_roles: set[str] = set()

    async def create_role(name: str, color: int = 0, hoist: bool = False, mentionable: bool = False, permissions_value: int = 0) -> str:
        """Создать роль. color — десятичное число (0xRRGGBB), permissions_value — битовая маска прав (0 = без прав)."""
        logger.info("create_role: вызов с name=%r, color=%r, hoist=%r, mentionable=%r, permissions_value=%r", name, color, hoist, mentionable, permissions_value)
        norm_name = (name or "").strip().lower()
        if norm_name in _created_roles:
            logger.info("create_role: пропуск — роль «%s» уже создана в этом исполнении", name)
            return f"Роль «{name}» уже создана в этом исполнении. Повторное создание пропущено."
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            logger.error("create_role: гильдия %s не найдена", ctx.guild_id)
            return "Гильдия не найдена."
        try:
            perms = discord.Permissions(permissions=int(permissions_value or 0))
            role = await guild.create_role(name=(name or "Новая роль")[:100], color=discord.Color(color) if color else discord.Color.default(), hoist=hoist, mentionable=mentionable, permissions=perms)
            _created_roles.add(norm_name)
            logger.info("create_role: УСПЕХ — роль «%s» (id=%s) создана на сервере", role.name, role.id)
            return f"Роль создана: {role.name} (id={role.id})."
        except Exception as e:
            logger.error("create_role: ОШИБКА при создании роли «%s»: %s", name, e)
            return f"Ошибка создания роли: {e!r}"

    async def delete_role(role_id: int, reason: str = "") -> str:
        """Удалить роль по ID."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        role = guild.get_role(int(role_id))
        if not role:
            return f"Роль {role_id} не найдена."
        try:
            await role.delete(reason=(reason or "По решению совета")[:512])
            return f"Роль {role.name} удалена."
        except Exception as e:
            return f"Ошибка удаления роли: {e!r}"

    async def edit_role(role_id: int, name: str = "", color: int = -1, hoist: bool = False, mentionable: bool = False) -> str:
        """Изменить роль: name, color (0xRRGGBB или -1 не менять), hoist, mentionable. Пустые/дефолты — не менять."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        role = guild.get_role(int(role_id))
        if not role:
            return f"Роль {role_id} не найдена."
        try:
            kwargs = {}
            if name:
                kwargs["name"] = name[:100]
            if color >= 0:
                kwargs["color"] = discord.Color(color)
            kwargs["hoist"] = hoist
            kwargs["mentionable"] = mentionable
            await role.edit(reason="По решению совета", **kwargs)
            return f"Роль {role.name} обновлена."
        except Exception as e:
            return f"Ошибка редактирования роли: {e!r}"

    async def create_text_channel(name: str, category_id: int = 0, topic: str = "", slowmode_seconds: int = 0, nsfw: bool = False) -> str:
        """Создать текстовый канал. category_id — ID категории (0 — без категории), slowmode_seconds — задержка между сообщениями (0–21600)."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            kwargs = {"name": (name or "канал")[:100], "topic": (topic or "")[:1024], "slowmode_delay": min(21600, max(0, slowmode_seconds)), "nsfw": nsfw}
            if category_id:
                cat = guild.get_channel(int(category_id))
                if cat:
                    kwargs["category"] = cat
            ch = await guild.create_text_channel(**kwargs)
            return f"Текстовый канал создан: {ch.name} (id={ch.id})."
        except Exception as e:
            return f"Ошибка создания канала: {e!r}"

    async def create_voice_channel(name: str, category_id: int = 0, user_limit: int = 0, bitrate: int = 64000) -> str:
        """Создать голосовой канал. user_limit — макс. участников (0 = без лимита), bitrate — битрейт в битах/с."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            kwargs = {"name": (name or "голос")[:100], "user_limit": min(99, max(0, user_limit)), "bitrate": min(384000, max(8000, bitrate))}
            if category_id:
                cat = guild.get_channel(int(category_id))
                if cat:
                    kwargs["category"] = cat
            ch = await guild.create_voice_channel(**kwargs)
            return f"Голосовой канал создан: {ch.name} (id={ch.id})."
        except Exception as e:
            return f"Ошибка создания голосового канала: {e!r}"

    async def create_category(name: str, position: int = 0) -> str:
        """Создать категорию каналов."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            cat = await guild.create_category(name=(name or "Категория")[:100], position=position)
            return f"Категория создана: {cat.name} (id={cat.id})."
        except Exception as e:
            return f"Ошибка создания категории: {e!r}"

    async def delete_channel(channel_id: int, reason: str = "") -> str:
        """Удалить канал или категорию по ID."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            name = getattr(channel, "name", str(channel_id))
            await channel.delete(reason=(reason or "По решению совета")[:512])
            return f"Канал {name} удалён."
        except Exception as e:
            return f"Ошибка удаления канала: {e!r}"

    async def edit_channel(channel_id: int, name: str = "", topic: str = "", slowmode_seconds: int = -1, nsfw: bool = False, category_id: int = 0) -> str:
        """Изменить канал: name, topic, slowmode_seconds (-1 не менять), nsfw, category_id (0 не менять)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            kwargs = {}
            if name:
                kwargs["name"] = name[:100]
            if topic is not None:
                kwargs["topic"] = topic[:1024]
            if slowmode_seconds >= 0:
                kwargs["slowmode_delay"] = min(21600, slowmode_seconds)
            kwargs["nsfw"] = nsfw
            if category_id:
                guild = ctx.bot.get_guild(ctx.guild_id)
                if guild:
                    cat = guild.get_channel(int(category_id))
                    if cat:
                        kwargs["category"] = cat
            await channel.edit(reason="По решению совета", **kwargs)
            return f"Канал обновлён."
        except Exception as e:
            return f"Ошибка редактирования канала: {e!r}"

    async def set_channel_permission(channel_id: int, target_id: int, allow_view: bool = True, deny_view: bool = False, allow_send: bool = True, deny_send: bool = False, target_type: str = "role") -> str:  # noqa: E501
        """Настроить права доступа канала для роли или участника. target_type: role или member. allow_* / deny_* — разрешить/запретить просмотр и отправку сообщений."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        guild = getattr(channel, "guild", None) or ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        try:
            target = guild.get_role(int(target_id)) if target_type == "role" else guild.get_member(int(target_id))
            if not target:
                if target_type == "role":
                    target = Object(id=int(target_id))
                else:
                    try:
                        target = await guild.fetch_member(int(target_id))
                    except Exception:
                        target = Object(id=int(target_id))
            overwrite = PermissionOverwrite(view_channel=allow_view if allow_view and not deny_view else (False if deny_view else None), send_messages=allow_send if allow_send and not deny_send else (False if deny_send else None))
            await channel.set_permissions(target, overwrite=overwrite, reason="По решению совета")
            return "Права канала обновлены."
        except Exception as e:
            return f"Ошибка настройки прав: {e!r}"

    async def move_member_voice(member_id: int, voice_channel_id: int) -> str:
        """Переместить участника в другой голосовой канал (если он в войсе). voice_channel_id=0 — отключить из канала."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        if not member.voice or not member.voice.channel:
            return "Участник не в голосовом канале."
        if voice_channel_id == 0:
            try:
                await member.move_to(None)
                return f"Участник {member.display_name} отключён от голосового канала."
            except Exception as e:
                return f"Ошибка: {e!r}"
        ch = guild.get_channel(int(voice_channel_id))
        if not ch:
            return f"Голосовой канал {voice_channel_id} не найден."
        try:
            await member.move_to(ch)
            return f"Участник {member.display_name} перемещён в {ch.name}."
        except Exception as e:
            return f"Ошибка перемещения: {e!r}"

    async def delete_message(channel_id: int, message_id: int) -> str:
        """Удалить сообщение по ID канала и ID сообщения."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete(reason="По решению совета")
            return "Сообщение удалено."
        except Exception as e:
            return f"Ошибка удаления сообщения: {e!r}"

    async def edit_message(channel_id: int, message_id: int, new_content: str) -> str:
        """Изменить текст сообщения (от имени бота)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=(new_content or "")[:2000])
            return "Сообщение отредактировано."
        except Exception as e:
            return f"Ошибка редактирования: {e!r}"

    async def pin_message(channel_id: int, message_id: int) -> str:
        """Закрепить сообщение в канале."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.pin()
            return "Сообщение закреплено."
        except Exception as e:
            return f"Ошибка закрепления: {e!r}"

    async def unpin_message(channel_id: int, message_id: int) -> str:
        """Открепить сообщение."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.unpin()
            return "Сообщение откреплено."
        except Exception as e:
            return f"Ошибка открепления: {e!r}"

    async def add_reaction(channel_id: int, message_id: int, emoji: str) -> str:
        """Поставить реакцию на сообщение. emoji — Unicode (👍) или имя кастомного эмодзи сервера."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.add_reaction(emoji.strip() or "✅")
            return "Реакция добавлена."
        except Exception as e:
            return f"Ошибка реакции: {e!r}"

    async def create_thread(channel_id: int, name: str, message_id: int = 0, auto_archive_minutes: int = 60) -> str:
        """Создать тред в канале. message_id — от какого сообщения (0 — тред без привязки), auto_archive_minutes: 60, 1440, 43200, 10080."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            archive = min(10080, max(60, auto_archive_minutes))
            if archive not in (60, 1440, 43200, 10080):
                archive = 60
            if message_id:
                msg = await channel.fetch_message(int(message_id))
                thread = await msg.create_thread(name=(name or "Тред")[:100], auto_archive_duration=archive)
            else:
                thread = await channel.create_thread(name=(name or "Тред")[:100], type=discord.ChannelType.public_thread, auto_archive_duration=archive, reason="По решению совета")
            return f"Тред создан: {thread.name} (id={thread.id})."
        except Exception as e:
            return f"Ошибка создания треда: {e!r}"

    async def create_invite(channel_id: int, max_age_seconds: int = 0, max_uses: int = 0, temporary: bool = False) -> str:
        """Создать приглашение в канал. max_age_seconds — время жизни в секундах (0 = бессрочно), max_uses — макс. использований (0 = без лимита)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            inv = await channel.create_invite(max_age=max_age_seconds or 0, max_uses=max_uses or 0, temporary=temporary, reason="По решению совета")
            return f"Приглашение создано: {inv.url} (идёт в канал {channel.name})."
        except Exception as e:
            return f"Ошибка создания приглашения: {e!r}"

    async def get_channels_list() -> str:
        """Список всех каналов и категорий сервера: id, name, type, category. Для выбора channel_id в других инструментах."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        out = []
        for ch in guild.channels:
            cat = ch.category.name if ch.category else None
            out.append({"id": ch.id, "name": ch.name, "type": str(getattr(ch, "type", "unknown")), "category": cat})
        import json
        return json.dumps(out, ensure_ascii=False, indent=0)

    async def purge_channel_messages(channel_id: int, limit: int = 100) -> str:
        """Удалить последние сообщения в канале (до 100 за раз). limit — сколько удалить (1–100)."""
        channel = ctx.bot.get_channel(int(channel_id))
        if not channel:
            return f"Канал {channel_id} не найден."
        try:
            deleted = await channel.purge(limit=min(100, max(1, limit)), reason="По решению совета")
            return f"Удалено сообщений: {len(deleted)}."
        except Exception as e:
            return f"Ошибка очистки: {e!r}"

    async def get_member_info(member_id: int) -> str:
        """Получить информацию об участнике: id, имя, ник, роли, дата присоединения. member_id — Discord ID."""
        guild = ctx.bot.get_guild(ctx.guild_id)
        if not guild:
            return "Гильдия не найдена."
        member = guild.get_member(int(member_id))
        if not member:
            try:
                member = await guild.fetch_member(int(member_id))
            except Exception:
                return f"Участник {member_id} не найден."
        roles = [r.name for r in member.roles if not getattr(r, "is_default", False)]
        joined = getattr(member, "joined_at", None)
        joined_str = str(joined) if joined else "—"
        import json
        return json.dumps({
            "id": member.id,
            "name": member.name,
            "display_name": getattr(member, "display_name", None),
            "roles": roles,
            "joined_at": joined_str,
            "bot": getattr(member, "bot", False),
        }, ensure_ascii=False, indent=0)

    # ── Флаги одноразовых вызовов (в рамках одного запуска агента) ──
    _once_flags: dict[str, bool] = {}

    async def _find_last_article_number() -> int:
        """Сканирует канал прецедентов (newest→oldest) и возвращает последний номер статьи (0 если нет)."""
        import re as _re
        ch_id = ctx.get_channel_id("law_judicial_precedents")
        if not ch_id:
            return 0
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return 0
        _patterns = [
            _re.compile(r"\*{0,2}(?:статья|ст\.?)\s*№?\s*(\d+)", _re.IGNORECASE),
            _re.compile(r"(?:^|\n)\s*(?:#+\s*)?(\d+)\s*[\.\)]\s", _re.MULTILINE),
        ]
        found: list[int] = []
        try:
            async for msg in channel.history(limit=60, oldest_first=False):
                text = (msg.content or "").strip()
                if not text:
                    continue
                for pat in _patterns:
                    for m in pat.finditer(text):
                        found.append(int(m.group(1)))
                if found:
                    break
        except Exception:
            pass
        return max(found) if found else 0

    async def get_last_law_article_number() -> str:
        """Получить номер последней статьи закона из канала судебных прецедентов. Следующую статью нумеруй как последняя + 1."""
        last = await _find_last_article_number()
        if last > 0:
            return f"Последний номер статьи в канале прецедентов: {last}. Следующая статья должна быть №{last + 1}."
        return "Статей с номерами не найдено. Начни с номера 1."

    async def publish_new_law_article(title: str, text: str) -> str:
        """Опубликовать новую статью закона в канал судебных прецедентов. Номер определяется автоматически (последний + 1). Вызывай ОДИН раз."""
        if _once_flags.get("publish_new_law_article"):
            return "Закон уже опубликован в этом исполнении."
        ch_id = ctx.get_channel_id("law_judicial_precedents")
        if not ch_id:
            return "Канал судебных прецедентов не настроен."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return "Канал судебных прецедентов не найден."
        last = await _find_last_article_number()
        article_number = last + 1
        msg = f"**Статья №{article_number}. {title}**\n\n{text[:3800]}"
        try:
            await channel.send(msg[:4000])
            _once_flags["publish_new_law_article"] = True
            logger.info("Совет → [#%s]: Статья №%s «%s» (%d симв.)", getattr(channel, "name", "?"), article_number, title[:80], len(msg))
            return f"Статья №{article_number} «{title}» опубликована в канал судебных прецедентов."
        except Exception as e:
            return f"Ошибка публикации: {e!r}"

    async def post_council_outcome_to_deliberations(case_id: str, outcome_text: str) -> str:
        """Опубликовать итог решения совета в канал обсуждений (deliberations). Один раз на дело."""
        if _once_flags.get("post_council_outcome"):
            return "Итог уже опубликован в этом исполнении."
        ch_id = ctx.get_channel_id("deliberations")
        if not ch_id:
            return "Канал обсуждений совета не настроен."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return "Канал обсуждений не найден."
        try:
            out_msg = f"**Итог по делу №{case_id}:**\n{outcome_text[:1800]}"[:2000]
            await channel.send(out_msg)
            _once_flags["post_council_outcome"] = True
            logger.info("Совет → [#%s]: %s", getattr(channel, "name", "?"), out_msg[:150].replace("\n", " "))
            return "Итог опубликован в канал обсуждений."
        except Exception as e:
            return f"Ошибка: {e!r}"

    async def post_to_execution_blog(case_id: str, summary: str) -> str:
        """Записать краткий отчёт об исполнении в блог совета (execution_blog). Один раз на дело."""
        if _once_flags.get("post_to_execution_blog"):
            return "Отчёт уже записан в блог."
        ch_id = ctx.get_channel_id("execution_blog")
        if not ch_id:
            return "Канал блога исполнения не настроен."
        channel = ctx.bot.get_channel(ch_id)
        if not channel:
            return "Канал блога не найден."
        try:
            blog_msg = f"**Исполнение дела №{case_id}:**\n{summary[:1800]}"[:2000]
            await channel.send(blog_msg)
            _once_flags["post_to_execution_blog"] = True
            logger.info("Совет → [#%s]: %s", getattr(channel, "name", "?"), blog_msg[:150].replace("\n", " "))
            return "Отчёт записан в блог исполнения."
        except Exception as e:
            return f"Ошибка: {e!r}"

    # Build tools list
    tools: list[Tool] = []

    tools.append(Tool(
        name="get_law",
        description="Получить текст закона из двух каналов права. Ориентируйся только на этот текст.",
        parameters=build_parameters({"max_chars": ("integer", "Макс. символов (по умолчанию 12000)")}, required=[]),
        execute=lambda max_chars=12000: get_law(max_chars),
    ))
    tools.append(Tool(
        name="get_council_inbox_recent",
        description="Последние сообщения из council_inbox (поручения от старейшин).",
        parameters=build_parameters({"limit": ("integer", "Число сообщений")}, required=[]),
        execute=lambda limit=25: get_council_inbox_recent(limit),
    ))
    tools.append(Tool(
        name="get_court_decisions_recent",
        description="Последние сообщения из канала решений суда (court_decisions).",
        parameters=build_parameters({"limit": ("integer", "Число сообщений")}, required=[]),
        execute=lambda limit=25: get_court_decisions_recent(limit),
    ))
    tools.append(Tool(
        name="get_council_case",
        description="Дело совета по номеру: содержание, статус, голоса.",
        parameters=build_parameters({"case_id": ("string", "Номер дела совета")}),
        execute=lambda case_id: get_council_case(case_id),
    ))
    tools.append(Tool(
        name="post_my_deliberation",
        description="Опубликовать свою позицию и голос в канал обсуждений. vote — только yes (За) или no (Против).",
        parameters=build_parameters({
            "case_id": ("string", "Номер дела совета"),
            "thoughts": ("string", "Твои мысли/обоснование"),
            "vote": ("string", "yes или no"),
        }),
        execute=lambda case_id, thoughts="", vote="no": post_my_deliberation(case_id, thoughts or "", vote or "no"),
    ))
    tools.append(Tool(
        name="list_council_cases",
        description="Список дел совета по статусу (open, voting_done, approved, rejected, executed).",
        parameters=build_parameters({"status": ("string", "Статус дел")}, required=[]),
        execute=lambda status="open": list_council_cases(status),
    ))
    tools.append(Tool(
        name="get_roles_and_members",
        description="Роли сервера и участники — для решений кого наказать/кому выдать роль.",
        parameters={},
        execute=get_roles_and_members,
    ))
    # ── Guard: если задан целевой участник, блокировать операции над другими ──
    _target_mid = ctx.extra.get("target_member_id") if ctx.extra else None
    _target_mname = ctx.extra.get("target_member_name", "") if ctx.extra else ""

    async def _guarded_add_role(member_id: int, role_id: int) -> str:
        mid = int(member_id)
        if _target_mid and mid != int(_target_mid):
            return (
                f"ЗАБЛОКИРОВАНО: целевой участник = {_target_mname} (member_id={_target_mid}). "
                f"Ты указал member_id={member_id} — НЕВЕРНО. Используй member_id={_target_mid}."
            )
        return await add_role_to_member(mid, role_id)

    async def _guarded_remove_role(member_id: int, role_id: int) -> str:
        mid = int(member_id)
        if _target_mid and mid != int(_target_mid):
            return (
                f"ЗАБЛОКИРОВАНО: целевой участник = {_target_mname} (member_id={_target_mid}). "
                f"Ты указал member_id={member_id} — НЕВЕРНО. Используй member_id={_target_mid}."
            )
        return await remove_role_from_member(mid, role_id)

    # ── Инструменты, доступные только при исполнении (execution_mode=True) ──
    if execution_mode:
        tools.append(Tool(
            name="send_message_to_channel",
            description="Отправить сообщение в канал по ID.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "content": ("string", "Текст")}),
            execute=lambda channel_id, content="": send_message_to_channel(channel_id, content or ""),
        ))
        tools.append(Tool(
            name="add_role_to_member",
            description="Выдать участнику роль (member_id, role_id из get_roles_and_members).",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "role_id": ("integer", "Discord ID роли")}),
            execute=lambda member_id, role_id: _guarded_add_role(member_id, role_id),
        ))
        tools.append(Tool(
            name="remove_role_from_member",
            description="Снять роль у участника.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "role_id": ("integer", "Discord ID роли")}),
            execute=lambda member_id, role_id: _guarded_remove_role(member_id, role_id),
        ))
        tools.append(Tool(
            name="timeout_member",
            description="Таймаут (mute) участника на N минут. ОПАСНО: вызывай ТОЛЬКО если в тексте решения ЯВНО указан конкретный участник и действие «таймаут/мут». Никогда не мьюти по догадке.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "duration_minutes": ("integer", "Минут")}),
            execute=lambda member_id, duration_minutes: timeout_member(member_id, duration_minutes or 60),
        ))
        tools.append(Tool(
            name="kick_member",
            description="Исключить участника с сервера (kick). ОПАСНО: вызывай ТОЛЬКО если в тексте решения ЯВНО указан конкретный участник и действие «исключить/кикнуть». Никогда не кикай по догадке.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "reason": ("string", "Причина")}, required=["member_id"]),
            execute=lambda member_id, reason="": kick_member(member_id, reason or ""),
        ))
        tools.append(Tool(
            name="ban_member",
            description="Забанить участника. ОПАСНО: вызывай ТОЛЬКО если в тексте решения ЯВНО указан конкретный участник и действие «забанить». Никогда не баннь по догадке или без прямого указания.",
            parameters=build_parameters({
                "member_id": ("integer", "Discord ID участника"),
                "reason": ("string", "Причина"),
                "delete_message_days": ("integer", "Удалить сообщения за последние N дней (0–7)"),
            }, required=["member_id"]),
            execute=lambda member_id, reason="", delete_message_days=0: ban_member(member_id, reason or "", delete_message_days or 0),
        ))
        tools.append(Tool(
            name="unban_member",
            description="Разбанить пользователя по Discord ID.",
            parameters=build_parameters({"user_id": ("integer", "Discord ID пользователя"), "reason": ("string", "Причина")}, required=["user_id"]),
            execute=lambda user_id, reason="": unban_member(user_id, reason or ""),
        ))
        tools.append(Tool(
            name="remove_timeout",
            description="Снять таймаут (mute) с участника.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника")}),
            execute=remove_timeout,
        ))
        tools.append(Tool(
            name="set_member_nick",
            description="Изменить никнейм участника на сервере. Пустая строка — сбросить на дефолтный.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "nick": ("string", "Новый ник (до 32 символов)")}),
            execute=lambda member_id, nick="": set_member_nick(member_id, nick or ""),
        ))
        tools.append(Tool(
            name="create_role",
            description="Создать роль. color — число (например 0xRRGGBB в десятичном виде), permissions_value — битовая маска прав (0 = без прав).",
            parameters=build_parameters({
                "name": ("string", "Название роли"),
                "color": ("integer", "Цвет (десятичное число, 0 = дефолт)"),
                "hoist": ("boolean", "Показывать отдельно в списке участников"),
                "mentionable": ("boolean", "Разрешить упоминание роли"),
                "permissions_value": ("integer", "Битовая маска прав (0 = без прав)"),
            }, required=["name"]),
            execute=lambda name, color=0, hoist=False, mentionable=False, permissions_value=0: create_role(name or "", color or 0, hoist, mentionable, permissions_value or 0),
        ))
        tools.append(Tool(
            name="delete_role",
            description="Удалить роль по ID.",
            parameters=build_parameters({"role_id": ("integer", "Discord ID роли"), "reason": ("string", "Причина")}, required=["role_id"]),
            execute=lambda role_id, reason="": delete_role(role_id, reason or ""),
        ))
        tools.append(Tool(
            name="edit_role",
            description="Изменить роль: name, color (0xRRGGBB в десятичном, -1 не менять), hoist, mentionable.",
            parameters=build_parameters({
                "role_id": ("integer", "Discord ID роли"),
                "name": ("string", "Новое название"),
                "color": ("integer", "Цвет (-1 не менять)"),
                "hoist": ("boolean", "Показывать отдельно"),
                "mentionable": ("boolean", "Упоминаемая"),
            }, required=["role_id"]),
            execute=lambda role_id, name="", color=-1, hoist=False, mentionable=False: edit_role(role_id, name or "", color, hoist, mentionable),
        ))
        tools.append(Tool(
            name="create_text_channel",
            description="Создать текстовый канал. category_id — ID категории (0 — без категории), slowmode_seconds — задержка между сообщениями (0–21600).",
            parameters=build_parameters({
                "name": ("string", "Название канала"),
                "category_id": ("integer", "ID категории (0 — без категории)"),
                "topic": ("string", "Тема канала"),
                "slowmode_seconds": ("integer", "Задержка между сообщениями (0–21600)"),
                "nsfw": ("boolean", "NSFW канал"),
            }, required=["name"]),
            execute=lambda name, category_id=0, topic="", slowmode_seconds=0, nsfw=False: create_text_channel(name or "", category_id or 0, topic or "", slowmode_seconds or 0, nsfw),
        ))
        tools.append(Tool(
            name="create_voice_channel",
            description="Создать голосовой канал. user_limit — макс. участников (0 = без лимита), bitrate — битрейт в битах/с.",
            parameters=build_parameters({
                "name": ("string", "Название канала"),
                "category_id": ("integer", "ID категории (0 — без)"),
                "user_limit": ("integer", "Макс. участников (0 = без лимита)"),
                "bitrate": ("integer", "Битрейт в битах/с (8000–384000)"),
            }, required=["name"]),
            execute=lambda name, category_id=0, user_limit=0, bitrate=64000: create_voice_channel(name or "", category_id or 0, user_limit or 0, bitrate or 64000),
        ))
        tools.append(Tool(
            name="create_category",
            description="Создать категорию каналов.",
            parameters=build_parameters({"name": ("string", "Название категории"), "position": ("integer", "Позиция в списке")}, required=["name"]),
            execute=lambda name, position=0: create_category(name or "", position or 0),
        ))
        tools.append(Tool(
            name="delete_channel",
            description="Удалить канал или категорию по ID. ОПАСНО: вызывай ТОЛЬКО если в решении ЯВНО сказано удалить конкретный канал.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала/категории"), "reason": ("string", "Причина")}, required=["channel_id"]),
            execute=lambda channel_id, reason="": delete_channel(channel_id, reason or ""),
        ))
        tools.append(Tool(
            name="edit_channel",
            description="Изменить канал: name, topic, slowmode_seconds (-1 не менять), nsfw, category_id (0 не менять).",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала"),
                "name": ("string", "Новое название"),
                "topic": ("string", "Тема"),
                "slowmode_seconds": ("integer", "Задержка сообщений (-1 не менять)"),
                "nsfw": ("boolean", "NSFW"),
                "category_id": ("integer", "ID категории (0 не менять)"),
            }, required=["channel_id"]),
            execute=lambda channel_id, name="", topic="", slowmode_seconds=-1, nsfw=False, category_id=0: edit_channel(channel_id, name or "", topic or "", slowmode_seconds, nsfw, category_id or 0),
        ))
        tools.append(Tool(
            name="set_channel_permission",
            description="Настроить права доступа канала для роли или участника. target_type: role или member. allow_view/deny_view, allow_send/deny_send — просмотр канала и отправка сообщений.",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала"),
                "target_id": ("integer", "ID роли или участника"),
                "target_type": ("string", "role или member"),
                "allow_view": ("boolean", "Разрешить просмотр"),
                "deny_view": ("boolean", "Запретить просмотр"),
                "allow_send": ("boolean", "Разрешить отправку сообщений"),
                "deny_send": ("boolean", "Запретить отправку"),
            }, required=["channel_id", "target_id"]),
            execute=lambda channel_id, target_id, target_type="role", allow_view=True, deny_view=False, allow_send=True, deny_send=False: set_channel_permission(channel_id, target_id, allow_view, deny_view, allow_send, deny_send, target_type),
        ))
        tools.append(Tool(
            name="move_member_voice",
            description="Переместить участника в другой голосовой канал. voice_channel_id=0 — отключить из канала.",
            parameters=build_parameters({"member_id": ("integer", "Discord ID участника"), "voice_channel_id": ("integer", "ID голосового канала (0 — отключить)")}),
            execute=lambda member_id, voice_channel_id: move_member_voice(member_id, voice_channel_id),
        ))
        tools.append(Tool(
            name="delete_message",
            description="Удалить сообщение по ID канала и ID сообщения.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "message_id": ("integer", "ID сообщения")}),
            execute=lambda channel_id, message_id: delete_message(channel_id, message_id),
        ))
        tools.append(Tool(
            name="edit_message",
            description="Изменить текст сообщения (от имени бота).",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "message_id": ("integer", "ID сообщения"), "new_content": ("string", "Новый текст")}),
            execute=lambda channel_id, message_id, new_content="": edit_message(channel_id, message_id, new_content or ""),
        ))
        tools.append(Tool(
            name="pin_message",
            description="Закрепить сообщение в канале.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "message_id": ("integer", "ID сообщения")}),
            execute=lambda channel_id, message_id: pin_message(channel_id, message_id),
        ))
        tools.append(Tool(
            name="unpin_message",
            description="Открепить сообщение.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "message_id": ("integer", "ID сообщения")}),
            execute=lambda channel_id, message_id: unpin_message(channel_id, message_id),
        ))
        tools.append(Tool(
            name="add_reaction",
            description="Поставить реакцию на сообщение. emoji — Unicode (👍) или имя кастомного эмодзи сервера.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "message_id": ("integer", "ID сообщения"), "emoji": ("string", "Эмодзи (Unicode или имя)")}),
            execute=lambda channel_id, message_id, emoji="✅": add_reaction(channel_id, message_id, emoji or "✅"),
        ))
        tools.append(Tool(
            name="create_thread",
            description="Создать тред в канале. message_id — от какого сообщения (0 — без привязки), auto_archive_minutes: 60, 1440, 43200, 10080.",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала"),
                "name": ("string", "Название треда"),
                "message_id": ("integer", "ID сообщения (0 — без привязки)"),
                "auto_archive_minutes": ("integer", "Через сколько минут архивировать (60–10080)"),
            }, required=["channel_id", "name"]),
            execute=lambda channel_id, name, message_id=0, auto_archive_minutes=60: create_thread(channel_id, name or "", message_id or 0, auto_archive_minutes or 60),
        ))
        tools.append(Tool(
            name="create_invite",
            description="Создать приглашение в канал. max_age_seconds — время жизни (0 = бессрочно), max_uses — макс. использований (0 = без лимита).",
            parameters=build_parameters({
                "channel_id": ("integer", "ID канала"),
                "max_age_seconds": ("integer", "Время жизни в секундах (0 = бессрочно)"),
                "max_uses": ("integer", "Макс. использований (0 = без лимита)"),
                "temporary": ("boolean", "Временное членство (при выходе теряет роли)"),
            }, required=["channel_id"]),
            execute=lambda channel_id, max_age_seconds=0, max_uses=0, temporary=False: create_invite(channel_id, max_age_seconds or 0, max_uses or 0, temporary),
        ))
        tools.append(Tool(
            name="purge_channel_messages",
            description="Удалить последние сообщения в канале (до 100 за раз). ОПАСНО: вызывай ТОЛЬКО если в решении ЯВНО сказано очистить канал.",
            parameters=build_parameters({"channel_id": ("integer", "ID канала"), "limit": ("integer", "Сколько удалить (1–100)")}, required=["channel_id"]),
            execute=lambda channel_id, limit=100: purge_channel_messages(channel_id, limit or 100),
        ))
        # ── Законотворчество и отчётность ──
        tools.append(Tool(
            name="get_last_law_article_number",
            description="Узнать номер последней статьи закона в канале прецедентов (чтобы определить следующий номер).",
            parameters={},
            execute=get_last_law_article_number,
        ))
        tools.append(Tool(
            name="publish_new_law_article",
            description="Опубликовать новую статью закона в канал судебных прецедентов. Номер определяется автоматически (последний в канале + 1). Вызывай ОДИН раз.",
            parameters=build_parameters({
                "title": ("string", "Название статьи"),
                "text": ("string", "Полный текст статьи"),
            }),
            execute=lambda title="", text="": publish_new_law_article(title or "", text or ""),
        ))
        tools.append(Tool(
            name="post_council_outcome_to_deliberations",
            description="Опубликовать итог решения совета в канал обсуждений. Вызывай ОДИН раз после исполнения.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела"),
                "outcome_text": ("string", "Текст итога"),
            }),
            execute=lambda case_id, outcome_text="": post_council_outcome_to_deliberations(case_id, outcome_text or ""),
        ))
        tools.append(Tool(
            name="post_to_execution_blog",
            description="Записать краткий отчёт об исполнении в блог совета. Вызывай ОДИН раз после исполнения.",
            parameters=build_parameters({
                "case_id": ("string", "Номер дела"),
                "summary": ("string", "Краткий отчёт"),
            }),
            execute=lambda case_id, summary="": post_to_execution_blog(case_id, summary or ""),
        ))

    # ── Read-only инструменты (всегда доступны) ──
    tools.append(Tool(
        name="get_channels_list",
        description="Список всех каналов и категорий сервера: id, name, type, category. Для выбора channel_id в других инструментах.",
        parameters={},
        execute=get_channels_list,
    ))
    tools.append(Tool(
        name="get_member_info",
        description="Получить информацию об участнике по Discord ID: имя, ник, роли, дата присоединения.",
        parameters=build_parameters({"member_id": ("integer", "Discord ID участника")}),
        execute=get_member_info,
    ))

    return tools
