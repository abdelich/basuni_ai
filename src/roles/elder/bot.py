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
from src.core.discord_guild import (
    get_guild_channels_json,
    get_guild_roles_and_members_json,
    get_author_roles_block_async,
    get_law_block_async,
)
from src.core.conversation_memory import save_message, load_recent_messages
from src.roles.base import RoleBot, RoleDeps
from src.roles.elder.tools import make_elder_tools

logger = logging.getLogger("basuni.elder.bot")

# Если агент вернёт ровно это — ответ в Discord не отправляем (старейшина решил не отвечать)
SKIP_REPLY_MARKER = "НЕТ"
# В режиме надзора: если агент вернёт это — действие легитимно, в канал ничего не постим
LEGITIMATE_MARKER = "ЛЕГИТИМНО"


def _has_pmj_role(message: Message, pmj_role_id: int | None) -> bool:
    if not pmj_role_id or not message.guild:
        return True
    member = message.guild.get_member(message.author.id)
    if not member:
        return False
    return any(r.id == pmj_role_id for r in member.roles)


def _detect_case_type(content: str) -> str:
    """По тексту обращения определяем тип дела: референдум или апелляция по процедуре."""
    text = (content or "").lower()
    ref_markers = (
        "референдум", "референдума", "референдуму", "проведени", "провести референдум",
        "прошу рассмотреть возможность проведения референдума", "запрос на референдум",
    )
    if any(m in text for m in ref_markers):
        return "referendum_request"
    return "appeal_procedure"


async def _create_elder_case(guild_id: int, author_id: int, channel_id: int, thread_id: int | None, content: str) -> int:
    case_type = _detect_case_type(content)
    async with get_db() as session:
        case = ElderCase(
            guild_id=guild_id,
            case_type=case_type,
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
        self._watch_channel_ids: list[int] = []

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
        self._watch_channel_ids = self.config.watch_channel_ids(self.role_key)
        if self._inbox_channel_id:
            logger.info("Старейшина: inbox канал %s (читает все сообщения, сам решает кому отвечать)", self._inbox_channel_id)
        if self._watch_channel_ids:
            logger.info("Старейшина: надзор за каналами %s (проверка легитимности действий)", self._watch_channel_ids)

    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            await self.process_commands(message)
            return

        guild = message.guild
        channel_id = message.channel.id

        # Надзор: сообщение в отслеживаемом канале (суд, совет) — проверяем легитимность действия
        if guild and self._watch_channel_ids and channel_id in self._watch_channel_ids:
            await self._handle_oversight(message)
            await self.process_commands(message)
            return

        inbox_id = self._inbox_channel_id or self.config.channel_for_role(self.role_key, "inbox")
        if not inbox_id or channel_id != inbox_id:
            await self.process_commands(message)
            return
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

        history = await load_recent_messages(
            self.role_key, guild.id, channel_id, thread_id, limit=20, author_id=message.author.id
        )
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "Гражданин"
        # Роли обратившегося: передаём member из сообщения, чтобы данные были точными (не зависят от fetch_member/Intent)
        author_block, author_role_names = await get_author_roles_block_async(
            self, guild.id, message.author.id, author_name, member=getattr(message.author, "roles", None) and message.author or None
        )
        # Закон в контексте при каждом сообщении — агент действует только по закону (каналы из конфига: база, судебные прецеденты)
        law_block = await get_law_block_async(
            self, guild.id, max_chars=12000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        channels_json = get_guild_channels_json(self, guild.id)
        roles_json = get_guild_roles_and_members_json(self, guild.id)
        ch_decisions = self.config.channel_for_role(self.role_key, "decisions")
        ch_court = self.config.channel_for_role(self.role_key, "notify_court")
        ch_council = self.config.channel_for_role(self.role_key, "notify_council")
        elder_channels_line = (
            f"Каналы старейшин (используй для публикации решений и уведомлений): "
            f"decisions={ch_decisions or '—'}, notify_court={ch_court or '—'}, notify_council={ch_council or '—'}. "
            f"Для отправки в суд/совет вызывай send_message_to_channel(channel_id, текст).\n"
        )
        context_block = (
            elder_channels_line
            + "Данные сервера: каналы (id, name, category_name, topic, viewable_by_roles, denied_for_roles) и роли с участниками. "
            "Перед рекомендацией канала проверь доступ обратившегося (его роли — см. блок «КОМУ ТЫ ОТВЕЧАЕШЬ»).\n"
            "Каналы:\n" + channels_json + "\n\nРоли и участники:\n" + roles_json + "\n\n---\n"
        )
        current_user_content = (
            author_block
            + f"Обращение №{case_id}. Сообщение: {content}"
        )
        # В начало контекста — закон, чтобы агент всегда опирался на него
        law_prefix = law_block + "\n\n---\n"

        messages_for_llm: list[dict[str, Any]] = []
        if history:
            messages_for_llm.extend(history)
        full_user_content = law_prefix + context_block + current_user_content
        messages_for_llm.append({"role": "user", "content": full_user_content})

        agent_ctx = self._agent_context(
            guild.id,
            extra={"current_case_id": case_id, "author_id": message.author.id, "author_display_name": author_name},
        )
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

    async def _handle_oversight(self, message: Message) -> None:
        """Проверка легитимности действия в канале надзора (суд, совет). При нелегитимности — пост прерывания в канал."""
        guild = message.guild
        if not guild:
            return
        content = (message.content or "").strip()
        if not content:
            return
        author_name = getattr(message.author, "display_name", None) or getattr(message.author, "name", "") or "?"
        channel_name = getattr(message.channel, "name", str(message.channel.id))
        author_block, author_role_names = await get_author_roles_block_async(
            self, guild.id, message.author.id, author_name,
            member=getattr(message.author, "roles", None) and message.author or None,
        )
        law_block = await get_law_block_async(
            self, guild.id, max_chars=10000,
            reference_category_name=getattr(self.config, "reference_category_name", None) or "право",
            config=self.config,
        )
        author_id = message.author.id
        oversight_user = (
            law_block + "\n\n---\n\n"
            + "[ РЕЖИМ НАДЗОРА ]\n"
            + f"Канал: {channel_name} (id={message.channel.id}). author_id для упоминания: {author_id} (в ответе пиши <@{author_id}> чтобы упомянуть автора).\n"
            + author_block
            + f"\nСообщение участника: {content[:1500]}\n\n"
            "1) Проверь по закону: соответствие закону, право автора на действие (роли), легитимность процедуры. "
            "2) Если это обращение к старейшине (суд вернул дело, задал вопрос, просит разъяснение и т.д.) — ты можешь ответить по делу. Тогда ответь ровно: ОТВЕТ: и далее твой текст; в тексте упомяни автора: <@"
            + str(author_id) + ">. "
            "3) Если только проверка и отвечать не нужно — ответь ровно: ЛЕГИТИМНО. "
            "4) Если нелегитимно — ответь одним сообщением для постинга (без ОТВЕТ:): прерывание, нарушение, ссылка на закон."
        )
        agent_ctx = self._agent_context(guild.id, extra={"author_id": author_id})
        agent = self._build_agent(agent_ctx)
        try:
            reply = await agent.run([{"role": "user", "content": oversight_user}])
        except Exception as e:
            logger.exception("Ошибка агента надзора старейшины")
            return
        reply_clean = (reply or "").strip()
        if reply_clean.upper() == LEGITIMATE_MARKER:
            return
        if not reply_clean:
            return
        # Ответ старейшины по существу (суд вернул дело, вопрос и т.д.) — постим с упоминанием
        if reply_clean.upper().startswith("ОТВЕТ:"):
            to_send = reply_clean[6:].strip()[:2000]
            if not to_send:
                return
            try:
                await message.reply(to_send)
            except Exception as e:
                logger.exception("Не удалось отправить ответ старейшины в канал надзора")
                try:
                    await message.channel.send(to_send)
                except Exception:
                    pass
            return
        # Прерывание нелегитимного
        try:
            await message.reply(reply_clean[:2000])
        except Exception as e:
            logger.exception("Не удалось отправить прерывание надзора")
            try:
                await message.channel.send(reply_clean[:2000])
            except Exception:
                pass


def create_elder_bot(deps: RoleDeps) -> RoleBot:
    return ElderBot(deps=deps)
