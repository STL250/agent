from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from .errors import ModelError
from .types import JsonObject, Message, ModelReply, ToolCall


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleClient:
    """Small HTTP client for the OpenAI-compatible Chat Completions protocol."""

    def __init__(self, config: Config, *, retries: int = 3) -> None:
        self.config = config
        self.retries = retries
        self.endpoint = config.base_url + "/chat/completions"

    def complete(self, messages: list[Message], tools: list[JsonObject]) -> ModelReply:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "rivet-code-agent/1.0",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                self.endpoint, data=encoded, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.request_timeout
                ) as response:
                    raw = response.read().decode("utf-8")
                return self._parse_response(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read(4_000).decode("utf-8", errors="replace")
                if exc.code in RETRYABLE_STATUS and attempt < self.retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise ModelError(
                    f"model endpoint returned HTTP {exc.code}: {self._sanitize(detail)}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise ModelError(f"cannot reach model endpoint: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
        raise ModelError("model request failed after retries")

    def _parse_response(self, raw: str) -> ModelReply:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelError("model endpoint returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise ModelError("model response must be a JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            error = payload.get("error")
            raise ModelError(f"model response has no choices: {self._sanitize(str(error or 'unknown error'))}")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ModelError("model response has no assistant message")

        content = self._content_text(message.get("content"))
        parsed_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelError("assistant tool_calls must be a list")
        for index, call in enumerate(raw_calls):
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ModelError("only function tool calls are supported")
            function = call.get("function")
            if not isinstance(function, dict):
                raise ModelError("tool call is missing function data")
            call_id = call.get("id")
            name = function.get("name")
            arguments = function.get("arguments", "{}")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"generated_call_{index}"
            if not isinstance(name, str) or not name:
                raise ModelError("tool call is missing a function name")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            parsed_calls.append(ToolCall(call_id, name, arguments))

        normalized: Message = {"role": "assistant", "content": content or None}
        if raw_calls:
            normalized["tool_calls"] = raw_calls
        return ModelReply(content, tuple(parsed_calls), normalized)

    @staticmethod
    def _content_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        raise ModelError("assistant content has an unsupported shape")

    def _sanitize(self, text: str) -> str:
        sanitized = text.replace("\r", " ").replace("\n", " ")[:1_500]
        if self.config.api_key:
            sanitized = sanitized.replace(self.config.api_key, "[REDACTED]")
        return sanitized

