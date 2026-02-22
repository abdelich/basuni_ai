"""
Бот «Старейшина»: агент с памятью переписок. Читает все сообщения в канале и сам решает, кому и когда отвечать.
Ответ приходит как reply на сообщение пользователя. Характер — мудрый старейшина.
"""
from __future__ import annotations

import logging
from typing import Any

from discord import Message  # type: ignore[reportMissingImports]
from discord.ext import commands  # type: ignore[reportMissingImports]

from src.core.agent import Agent
from src.core.agent_ctx import AgentContext
from src.core.db import get_db
from src.core.models import ElderCase
from src.core.discord_guild import get_guild_channels_json, get_guild_roles_and_members_json
from src.core.conversation_memory import save_message, load_recent_messages
from src.roles.base import RoleBot, RoleDeps
from src.roles.elder.tools import make_elder_tools

logger = logging.getLogger("basuni.elder.bot")

# Если агент вернёт ровно это — ответ в Discord не отправляем (старейшина решил не отвечать)
SKIP_REPLY_MARKER = "НЕТ"


def _has_pmj_role(message: Message, pmj_role_id: int | None) -> bool:
    if not pmj_role_id or not message.guild:
        return True
    member = message.guild.get_member(message.author.id)
    if not member:
        return False
    return any(r.id == pmj_role_id for r in member.roles)


async def _create_elder_case(guild_id: int, author_id: int, channel_id: int, thread_id: int | None, content: str) -> int:
    async with get_db() as session:
        case = ElderCase(
            guild_id=guild_id,
            case_type="appeal_procedure",
            status="open",
            author_id=author_id,
            channel_id=channel_id,
            thread_id=thread_id,
            initial_content=content,
        )
        session.add(case)
        await session.flush()
        return case.id


class ElderBot(RoleBot):
    def __init__(self, deps: RoleDeps, **kwargs: object) -> None:
        super().__init__(role_key="elder", deps=deps, command_prefix="!", **kwargs)
        self._inbox_channel_id: int | None = None

    def _agent_context(self, guild_id: int, extra: dict[str, Any] | None = None) -> AgentContext:
        cfg = self.config
        channel_ids = {}
        for purpose in ("inbox", "decisions", "outbox", "notify_court", "notify_council", "referrals"):
            ch_id = cfg.channel_for_role(self.role_key, purpose)
            if ch_id:
                channel_ids[purpose] = ch_id
        return AgentContext(
            guild_id=guild_id,
            channel_ids=channel_ids,
            bot=self,
            db_session_factory=self.deps.db_session_factory,
            extra=extra or {},
        )

    def _build_agent(self, ctx: AgentContext) -> Agent:
        system_prompt = self.load_system_prompt()
        tools = make_elder_tools(ctx)
        return Agent(
            system_prompt=system_prompt,
            tools=tools,
            api_key=self.deps.openai_api_key,
            model=self.config.openai_model,
            max_tool_rounds=5,
        )

    async def setup_hook(self) -> None:
        await super().setup_hook()
        self._inbox_channel_id = self.config.channel_for_role(self.role_key, "inbox")
        if self._inbox_channel_id:
            logger.info("Старейшина: inbox канал %s (читает все сообщения, сам решает кому отвечать)", self._inbox_channel_id)

    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            await self.process_commands(message)
            return

        inbox_id = self._inbox_channel_id or self.config.channel_for_role(self.role_key, "inbox")
        if not inbox_id or message.channel.id != inbox_id:
            await self.process_commands(message)
            return

        guild = message.guild
        if not guild:
            await self.process_commands(message)
            return

        pmj_role_id = self.config.role_ids().get("pmj") or 0
        if not _has_pmj_role(message, pmj_role_id):
            try:
                await message.reply("К старейшинам могут обращаться только граждане с ПМЖ. У вас нет статуса ПМЖ.")
            except Exception as e:
                logger.exception("Не удалось отправить отказ ПМЖ")
            await self.process_commands(message)
            return

        content = (message.content or "").strip()
        if not content:
            await self.process_commands(message)
            return

        if getattr(message.channel, "parent_id", None):
            channel_id = message.channel.parent_id
            thread_id = message.channel.id
        else:
            channel_id = message.channel.id
            thread_id = None

        try:
            case_id = await _create_elder_case(
                guild_id=guild.id,
                author_id=message.author.id,
                channel_id=channel_id,
                thread_id=thread_id,
                content=content,
            )
        except Exception as e:
            logger.exception("Ошибка создания дела старейшин")
            try:
                await message.reply(f"Не удалось зарегистрировать обращение: {e!r}"[:500])
            except Exception:
                pass
            await self.process_commands(message)
            return

        history = await load_recent_messages(self.role_key, guild.id, channel_id, thread_id, limit=20)
        channels_json = get_guild_channels_json(self, guild.id)
        roles_json = get_guild_roles_and_members_json(self, guild.id)
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "Гражданин"

        context_block = (
            "Данные сервера: каналы (id, name, category_name, topic) и роли с участниками (id, name, members). "
            "Используй для выбора канала и понимания, кто в какой роли.\n"
            "Каналы:\n" + channels_json + "\n\nРоли и участники:\n" + roles_json + "\n\n---\n"
        )
        current_user_content = f"Обращение №{case_id}. Гражданин **{author_name}** пишет: {content}"

        messages_for_llm: list[dict[str, Any]] = []
        if history:
            messages_for_llm.extend(history)
        messages_for_llm.append({"role": "user", "content": context_block + current_user_content})

        agent_ctx = self._agent_context(guild.id, extra={"current_case_id": case_id})
        agent = self._build_agent(agent_ctx)

        try:
            reply = await agent.run(messages_for_llm)
        except Exception as e:
            logger.exception("Ошибка агента старейшины")
            reply = f"Произошла ошибка при обработке обращения: {e!r}"

        reply_clean = (reply or "").strip()
        if reply_clean and reply_clean.upper() != SKIP_REPLY_MARKER:
            try:
                await message.reply(reply[:2000])
            except Exception as e:
                logger.exception("Не удалось отправить ответ")
                try:
                    await message.channel.send(reply[:2000])
                except Exception:
                    pass
        await save_message(
            role_key=self.role_key,
            guild_id=guild.id,
            channel_id=channel_id,
            thread_id=thread_id,
            case_id=case_id,
            discord_message_id=message.id,
            author_id=message.author.id,
            author_display_name=author_name,
            role="user",
            content=current_user_content,
        )
        if reply_clean:
            await save_message(
                role_key=self.role_key,
                guild_id=guild.id,
                channel_id=channel_id,
                thread_id=thread_id,
                case_id=case_id,
                discord_message_id=None,
                author_id=None,
                author_display_name=None,
                role="assistant",
                content=reply,
            )

        await self.process_commands(message)


def create_elder_bot(deps: RoleDeps) -> RoleBot:
    return ElderBot(deps=deps)
