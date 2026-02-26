"""
Агент: вызов LLM с инструментами (function calling), выполнение tool_calls, цикл до финального ответа.
Один бот = один агент: системный промпт роли + инструменты роли; сообщения пользователя → ответ в канал.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from src.core.tools import Tool

logger = logging.getLogger("basuni.agent")

# Резервная модель при 403 (нет доступа к модели из конфига)
FALLBACK_MODEL = "gpt-4o-mini"

# Сообщения в формате OpenAI: {"role": "user"|"assistant"|"system", "content": ...} или с "tool_calls"
Message = dict[str, Any]


class Agent:
    """Агент с набором инструментов; общается с LLM и выполняет вызовы инструментов."""

    def __init__(
        self,
        system_prompt: str,
        tools: list[Tool],
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        max_tool_rounds: int = 5,
        base_url: str | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.api_key = api_key
        self.model = model
        self.max_tool_rounds = max_tool_rounds
        self.base_url = base_url
        self._tool_by_name = {t.name: t for t in tools}

    def _openai_client(self):
        from openai import AsyncOpenAI
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY не задан")
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return AsyncOpenAI(**kwargs)

    def _messages_for_api(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Системный промпт + диалог в формате API."""
        out = [{"role": "system", "content": self.system_prompt}]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content") or ""
            if m.get("tool_calls"):
                out.append({"role": "assistant", "content": content or None, "tool_calls": m["tool_calls"]})
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content", "")})
            else:
                out.append({"role": role, "content": content})
        return out

    async def run(self, messages: list[Message]) -> tuple[str, list[str]]:
        """
        Запуск агента: отправка в LLM, при tool_calls — выполнение инструментов и повтор.
        Возвращает (итоговый текстовый ответ, список имён вызванных инструментов).
        """
        if not self.api_key:
            return ("Не настроен API ключ для языковой модели. Ответы недоступны.", [])

        client = self._openai_client()
        openai_tools = [t.to_openai_function() for t in self.tools]
        api_messages = self._messages_for_api(messages)
        round_ = 0
        model_used = self.model
        tools_called: list[str] = []

        while round_ < self.max_tool_rounds:
            round_ += 1
            kwargs = {"model": model_used, "messages": api_messages}
            if openai_tools:
                kwargs["tools"] = openai_tools
                kwargs["tool_choice"] = "auto"

            try:
                resp = await client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                err_msg = str(e).lower()
                try:
                    from openai import PermissionDeniedError as OpenAIPermissionDenied
                except ImportError:
                    OpenAIPermissionDenied = type("PermissionDeniedError", (Exception,), {})
                is_model_access_denied = (
                    isinstance(e, OpenAIPermissionDenied)
                    or "permissiondenied" in type(e).__name__.lower()
                    or "403" in err_msg
                    or "does not have access to model" in err_msg
                    or "model_not_found" in err_msg
                )
                if is_model_access_denied and model_used == self.model:
                    print(
                        f"[basuni] Ошибка доступа к модели '{self.model}': {e!s}. "
                        f"Используем резервную модель: {FALLBACK_MODEL}.",
                        file=sys.stderr,
                    )
                    logger.warning(
                        "Ошибка доступа к модели %s: %s. Используем резервную модель: %s",
                        self.model,
                        e,
                        FALLBACK_MODEL,
                    )
                    model_used = FALLBACK_MODEL
                    kwargs["model"] = model_used
                    resp = await client.chat.completions.create(**kwargs)
                else:
                    raise

            model_for_response = getattr(resp, "model", None) or model_used
            print(f"[basuni] Для ответа использована модель: {model_for_response}", file=sys.stderr)
            logger.info("Для ответа использована модель: %s", model_for_response)

            choice = resp.choices[0] if resp.choices else None
            if not choice:
                return ("Нет ответа от модели.", tools_called)

            msg = choice.message
            if getattr(msg, "tool_calls", None):
                # Добавляем ответ ассистента в историю
                tool_calls = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in msg.tool_calls
                ]
                api_messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": tool_calls,
                })
                # Выполняем каждый tool_call и добавляем результат
                for tc in msg.tool_calls:
                    name = tc.function.name
                    tools_called.append(name)
                    args_str = tc.function.arguments or "{}"
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {}
                    tool = self._tool_by_name.get(name)
                    if not tool:
                        result = f"Неизвестный инструмент: {name}"
                    else:
                        try:
                            result = await tool.execute(**args)
                        except Exception as e:
                            logger.exception("Ошибка выполнения инструмента %s", name)
                            result = f"Ошибка: {e!r}"
                    api_messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
                continue

            return ((msg.content or "").strip(), tools_called)

        return ("Превышено число шагов; ответ не сформирован.", tools_called)
