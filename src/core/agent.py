"""
Агент: вызов LLM с инструментами (function calling), выполнение tool_calls, цикл до финального ответа.
Один бот = один агент: системный промпт роли + инструменты роли; сообщения пользователя → ответ в канал.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Any

from src.core.tools import Tool

logger = logging.getLogger("basuni.agent")

# Резервная модель при 403 (нет доступа к модели из конфига)
FALLBACK_MODEL = "gpt-4o-mini"


def _parse_retry_after(error_text: str) -> float | None:
    """Extract retry-after seconds from OpenAI rate limit error message."""
    m = re.search(r"try again in (\d+(?:\.\d+)?)\s*s", error_text, re.IGNORECASE)
    if m:
        return float(m.group(1)) + 0.5
    m = re.search(r"try again in (\d+)\s*ms", error_text, re.IGNORECASE)
    if m:
        return int(m.group(1)) / 1000.0 + 0.5
    return None

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
        stop_after_tools: set[str] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.api_key = api_key
        self.model = model
        self.max_tool_rounds = max_tool_rounds
        self.base_url = base_url
        self.stop_after_tools = stop_after_tools or set()
        self._tool_by_name = {t.name: t for t in tools}

    def _openai_client(self):
        from openai import AsyncOpenAI
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY не задан")
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return AsyncOpenAI(**kwargs)

    async def _call_with_retry(self, client: Any, kwargs: dict, model_used: str, max_retries: int = 5) -> Any:
        """Call OpenAI API with exponential backoff on rate limit (429) and model access fallback."""
        try:
            from openai import RateLimitError as OpenAIRateLimit
            from openai import PermissionDeniedError as OpenAIPermissionDenied
        except ImportError:
            OpenAIRateLimit = type("RateLimitError", (Exception,), {})
            OpenAIPermissionDenied = type("PermissionDeniedError", (Exception,), {})

        for attempt in range(max_retries):
            try:
                return await client.chat.completions.create(**kwargs)
            except OpenAIRateLimit as e:
                wait = _parse_retry_after(str(e)) or min(2 ** attempt, 30)
                logger.warning("Rate limit (429), retry %d/%d через %.1fs", attempt + 1, max_retries, wait)
                await asyncio.sleep(wait)
            except Exception as e:
                err_msg = str(e).lower()
                is_model_access_denied = (
                    isinstance(e, OpenAIPermissionDenied)
                    or "permissiondenied" in type(e).__name__.lower()
                    or "403" in err_msg
                    or "does not have access to model" in err_msg
                    or "model_not_found" in err_msg
                )
                if is_model_access_denied and model_used == self.model:
                    logger.warning("Ошибка доступа к модели %s: %s. Переключаемся на %s", self.model, e, FALLBACK_MODEL)
                    return None
                raise
        logger.error("Rate limit: исчерпаны %d попыток", max_retries)
        return None

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

            resp = await self._call_with_retry(client, kwargs, model_used)
            if resp is None and model_used == self.model:
                model_used = FALLBACK_MODEL
                kwargs["model"] = model_used
                resp = await self._call_with_retry(client, kwargs, model_used)
            if resp is None:
                return ("Ошибка: не удалось получить ответ от модели после повторных попыток.", tools_called)

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
                # Нормализуем ключи аргументов (модель иногда отдаёт "reasoning=" вместо "reasoning")
                def _normalize_args(a: dict) -> dict:
                    return {k.rstrip("="): v for k, v in a.items()}

                # Порядок: elder pipeline → remove roles → add roles (сначала забрать, потом выдать)
                _ORDER = ("create_elder_case", "publish_decision", "notify_court", "record_case_sent_to_court",
                          "remove_role_from_member", "add_role_to_member")
                def _tool_sort_key(tc: Any) -> tuple[int, str]:
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", None) or ""
                    try:
                        return (_ORDER.index(name), name) if name in _ORDER else (len(_ORDER), name)
                    except ValueError:
                        return (len(_ORDER), name)

                ordered_calls = sorted(msg.tool_calls, key=_tool_sort_key)
                tool_names_this_round = [tc.function.name for tc in ordered_calls]
                logger.info("Раунд %d — вызовы: %s", round_, tool_names_this_round)
                for tc in ordered_calls:
                    name = tc.function.name
                    tools_called.append(name)
                    args_str = tc.function.arguments or "{}"
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {}
                    args = _normalize_args(args)
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
                if self.stop_after_tools and self.stop_after_tools & set(tool_names_this_round):
                    logger.info("Стоп-сигнал: вызван %s — завершаем агент", self.stop_after_tools & set(tool_names_this_round))
                    return ("Исполнение завершено.", tools_called)
                continue

            if tools_called:
                logger.info("Агент завершён, вызваны инструменты: %s", tools_called)
            return ((msg.content or "").strip(), tools_called)

        logger.warning("Агент: превышено %d раундов, вызваны: %s", self.max_tool_rounds, tools_called)
        return ("Превышено число шагов; ответ не сформирован.", tools_called)
