"""
Инструменты агента: контракт и схема для LLM (OpenAI function calling).
Каждая роль регистрирует свой набор инструментов; агент передаёт их в LLM и выполняет вызовы.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable

# Схема инструмента в формате OpenAI (function)
# https://platform.openai.com/docs/guides/function-calling
ToolSchema = dict[str, Any]  # {"type": "function", "function": {"name", "description", "parameters": {...}}}


@dataclass
class Tool:
    """Один инструмент: имя, описание для LLM, JSON-schema параметров, асинхронный исполнитель."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema: {"properties": {...}, "required": [...]}
    execute: Callable[..., Awaitable[str]]
    """execute(**kwargs) -> str; результат возвращается в LLM как tool result."""

    def to_openai_function(self) -> dict[str, Any]:
        params = self.parameters if "type" in self.parameters else {"type": "object", "properties": self.parameters, "required": list(self.parameters.keys())}
        if "required" not in params and "properties" in params:
            params["required"] = list(params.get("properties", {}).keys())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }


def build_parameters(properties: dict[str, tuple[str, str]], required: list[str] | None = None) -> dict[str, Any]:
    """properties: { "case_id": ("integer", "Номер дела"), "reason": ("string", "Причина") } -> JSON Schema object."""
    props = {name: {"type": t, "description": desc} for name, (t, desc) in properties.items()}
    return {"type": "object", "properties": props, "required": required or list(props.keys())}
