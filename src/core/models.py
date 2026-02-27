"""
Модели БД. У каждого бота — своя локальная таблица; общие сущности (если понадобятся) — в отдельных таблицах.
"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from .db import Base


# --- Старейшина: локальная таблица дел ---

class ElderCase(Base):
    """Дела старейшин: апелляции, запросы референдума, дела не установленные судом."""
    __tablename__ = "elder_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_case_number = Column(Integer, nullable=True)  # порядковый номер в гильдии (1, 2, 3…) для отображения «дело №N»; id — для БД/API
    guild_id = Column(BigInteger, nullable=False, index=True)
    case_type = Column(String(64), nullable=False)  # referendum_request, civil_initiative, bill, appeal_procedure, not_established_by_court
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
    sent_to_court_content = Column(Text, nullable=True)  # текст обращения, который старейшина отправил в суд (для сопоставления с сообщениями судей и решений)
    court_deadline_hours = Column(Integer, nullable=True)  # срок по закону (часов) на решение суда; при дробных часах (напр. 0.25) хранится в court_deadline_minutes
    court_deadline_minutes = Column(Integer, nullable=True)  # срок в минутах (напр. 15 для 0.25 ч); приоритет над court_deadline_hours если задан
    court_deadline_expired_at = Column(DateTime, nullable=True)  # когда зафиксировано, что срок суда истёк (старейшина/бот смотрит в БД)
    deadline_escalation_at = Column(DateTime, nullable=True)  # когда старейшина отреагировал на истечение срока (наблюдение)

    # Решение суда (двое судей согласны)
    court_decided_at = Column(DateTime, nullable=True)
    court_result = Column(String(32), nullable=True)  # approved | rejected

    # Дело возвращено старейшине (разногласие судей, срок истёк, нарушение)
    returned_to_elder_at = Column(DateTime, nullable=True)
    returned_to_elder_reason = Column(Text, nullable=True)


class ElderCaseCourtVote(Base):
    """Голоса судей по делу: один голос на судью на дело. Источник правды для ответов «кто проголосовал»."""
    __tablename__ = "elder_case_court_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, nullable=False, index=True)  # ElderCase.id
    guild_id = Column(BigInteger, nullable=False, index=True)
    judge_id = Column(BigInteger, nullable=False, index=True)  # Discord user id судьи
    vote = Column(String(16), nullable=False)  # yes | no
    message_id = Column(BigInteger, nullable=True)
    voted_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("case_id", "judge_id", name="uq_elder_case_judge"),)


# --- Отчёт старейшины: события в судейских/надзорных каналах ---

class ElderCourtLog(Base):
    """Журнал событий в каналах надзора (суд, совет, решения суда, судебные прецеденты и т.д.) для отчёта старейшины."""
    __tablename__ = "elder_court_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    channel_id = Column(BigInteger, nullable=False, index=True)
    message_id = Column(BigInteger, nullable=True)
    author_id = Column(BigInteger, nullable=True)
    event_type = Column(String(64), nullable=False)  # judge_vote_yes, judge_vote_no, council_decision, interrupt, ...
    summary = Column(Text, nullable=True)  # краткое описание для отчёта
    meta = Column(Text, nullable=True)  # JSON: число судей, результат голосования и т.д.
    # Легитимность действия: одобрено старейшиной (approved), отклонено (rejected) или ещё не оценено (NULL)
    legitimacy = Column(String(16), nullable=True)  # approved | rejected
    legitimacy_at = Column(DateTime, nullable=True)  # когда старейшина проставил легитимность
    created_at = Column(DateTime, server_default=func.now())


# --- Совет: дела и голоса ---

class CouncilCase(Base):
    """Дело совета: задача от старейшин или решение суда для исполнения."""
    __tablename__ = "council_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    source = Column(String(32), nullable=False)  # elder | court
    source_channel_id = Column(BigInteger, nullable=False, index=True)
    source_message_id = Column(BigInteger, nullable=False, index=True)
    content = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="open")  # open, voting_done, approved, rejected, executed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    result_at = Column(DateTime, nullable=True)  # когда подсчитаны голоса
    result_announced_at = Column(DateTime, nullable=True)  # итог уже объявлён в канале (только один бот постит)
    execution_at = Column(DateTime, nullable=True)  # когда исполнено (если approved)
    nudge_1vote_sent_at = Column(DateTime, nullable=True)
    nudge_2votes_sent_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("guild_id", "source_channel_id", "source_message_id", name="uq_council_case_source"),)


class CouncilVote(Base):
    """Голос члена совета по делу: только За или Против."""
    __tablename__ = "council_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, nullable=False, index=True)  # CouncilCase.id
    guild_id = Column(BigInteger, nullable=False, index=True)
    member_index = Column(Integer, nullable=False)  # 1, 2 или 3
    vote = Column(String(16), nullable=False)  # yes | no
    deliberation_text = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("case_id", "member_index", name="uq_council_vote_member"),)


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


# --- Ветки разговоров и контекст (память: о чём речь в каждой ветке и в каналах) ---

class AgentConversationBranch(Base):
    """Память по ветке разговора: канал/тред/автор — краткий контекст «о чём речь», текущее дело, время последней активности."""
    __tablename__ = "agent_conversation_branches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    role_key = Column(String(32), nullable=False, index=True)
    channel_id = Column(BigInteger, nullable=False, index=True)
    thread_id = Column(BigInteger, nullable=True, index=True)
    author_id = Column(BigInteger, nullable=True, index=True)  # None = контекст всего канала/треда

    summary = Column(Text, nullable=True)  # краткое «о чём эта ветка»
    current_case_id = Column(Integer, nullable=True)
    last_activity_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
