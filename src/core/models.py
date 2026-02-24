"""
Модели БД. У каждого бота — своя локальная таблица; общие сущности (если понадобятся) — в отдельных таблицах.
"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from .db import Base


# --- Старейшина: локальная таблица дел ---

class ElderCase(Base):
    """Дела старейшин: апелляции, запросы референдума, дела не установленные судом."""
    __tablename__ = "elder_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    case_type = Column(String(64), nullable=False)  # appeal_procedure, referendum_request, not_established_by_court
    status = Column(String(64), nullable=False, default="open")  # open, closed

    author_id = Column(BigInteger, nullable=False, index=True)
    channel_id = Column(BigInteger, nullable=False)
    thread_id = Column(BigInteger, nullable=True)
    initial_content = Column(Text, nullable=True)

    meta = Column(Text, nullable=True)  # JSON: сроки, кворум, ссылка на судебное дело и т.д.

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    elder_decided_at = Column(DateTime, nullable=True)
    elder_decision = Column(String(64), nullable=True)  # confirm_process, send_to_council, return_to_court
    elder_reasoning = Column(Text, nullable=True)
    elder_already_decided = Column(Boolean, nullable=False, default=False)  # запрет повторного вмешательства

    # Отсчёт срока для суда: когда дело передано в суд и до когда суд должен принять решение (по закону)
    sent_to_court_at = Column(DateTime, nullable=True)  # когда старейшина уведомил суд (начало отсчёта)
    court_deadline_hours = Column(Integer, nullable=True)  # срок по закону (часов) на решение суда


# --- Память переписок агентов (общая для всех ролей по role_key) ---

class AgentConversationMessage(Base):
    """Сообщения в переписке с агентом: контекст по каналу/треду, история для памяти."""
    __tablename__ = "agent_conversation_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    role_key = Column(String(32), nullable=False, index=True)  # elder, council, ...
    channel_id = Column(BigInteger, nullable=False, index=True)
    thread_id = Column(BigInteger, nullable=True, index=True)  # если в треде — id треда
    case_id = Column(Integer, nullable=True, index=True)  # привязка к делу, если есть

    discord_message_id = Column(BigInteger, nullable=True)
    author_id = Column(BigInteger, nullable=True)
    author_display_name = Column(String(255), nullable=True)

    role = Column(String(16), nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
