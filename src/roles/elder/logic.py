"""
Логика бота «Старейшина»: проверка полномочий, типы дел, допустимые действия.
Используется из bot.py и когов. Конституция: Статья IV, XV.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ElderCaseType(str, Enum):
    """Типы дел, которые вправе рассматривать старейшины."""
    APPEAL_PROCEDURE = "appeal_procedure"
    REFERENDUM_REQUEST = "referendum_request"
    NOT_ESTABLISHED_BY_COURT = "not_established_by_court"


class ElderDecision(str, Enum):
    """Допустимые решения старейшин (Статья IV, п. 5; по референдуму — только одобрить/отклонить)."""
    CONFIRM_PROCESS = "confirm_process"
    SEND_TO_COUNCIL = "send_to_council"
    RETURN_TO_COURT = "return_to_court"
    # По делу о референдуме — только два исхода: одобрить (идёт в суд по закону) или отклонить (дело закрыто навсегда)
    REFERENDUM_APPROVED = "referendum_approved"
    REFERENDUM_REJECTED = "referendum_rejected"


def elder_may_consider(case_type: str) -> bool:
    """Старейшины рассматривают только три типа дел."""
    return case_type in (e.value for e in ElderCaseType)


def elder_may_decide(decision: str) -> bool:
    """Проверка: решение входит в перечень допустимых для старейшин."""
    return decision in (d.value for d in ElderDecision)


def elder_may_decide_for_case(decision: str, case_type: str) -> bool:
    """Проверка: решение допустимо для данного типа дела. По референдуму — только одобрить или отклонить; по апелляции — только confirm/send_to_council/return_to_court."""
    if case_type == ElderCaseType.REFERENDUM_REQUEST.value:
        return decision in (ElderDecision.REFERENDUM_APPROVED.value, ElderDecision.REFERENDUM_REJECTED.value)
    # По апелляции и «не установлено судом» — только классические решения, не референдумные
    if decision in (ElderDecision.REFERENDUM_APPROVED.value, ElderDecision.REFERENDUM_REJECTED.value):
        return False
    return elder_may_decide(decision)


def get_elder_prompt_context(case_data: dict[str, Any]) -> str:
    """Формирует контекст для GPT из данных дела (суд, присяжные, сроки и т.д.)."""
    parts = [
        f"Тип дела: {case_data.get('case_type', 'unknown')}",
        f"Статус: {case_data.get('status', 'unknown')}",
    ]
    if "appeal_reason" in case_data:
        parts.append(f"Причина апелляции: {case_data['appeal_reason']}")
    if "court_deadline_hours" in case_data:
        parts.append(f"Срок рассмотрения судом (часы): {case_data['court_deadline_hours']}")
    if "jury_quorum_percent" in case_data:
        parts.append(f"Кворум присяжных (%): {case_data['jury_quorum_percent']}")
    return "\n".join(parts)
