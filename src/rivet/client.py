from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, BinaryIO

from .config import Config
from .errors import ModelError
from .types import JsonObject, Message, ModelReply, TextDeltaHandler, ToolCall


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
STREAM_FALLBACK_STATUS = {400, 404, 405, 415, 422, 501}


class OpenAICompatibleClient:
    """Small HTTP client for the OpenAI-compatible Chat Completions protocol."""

    def __init__(self, config: Config, *, retries: int = 3) -> None:
        self.config = config
        self.retries = retries
        self.endpoint = config.base_url + "/chat/completions"

    def complete(self, messages: list[Message], tools: list[JsonObject]) -> ModelReply:
        payload = self._request_payload(messages, tools, stream=False)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(stream=False)

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
                if self._retry_http_error(exc, attempt):
                    continue
                detail = exc.read(4_000).decode("utf-8", errors="replace")
                raise ModelError(
                    f"model endpoint returned HTTP {exc.code}: {self._sanitize(detail)}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                detail = exc.reason if hasattr(exc, "reason") else exc
                raise ModelError(f"cannot reach model endpoint: {detail}") from exc
        raise ModelError("model request failed after retries")

    def complete_stream(
        self,
        messages: list[Message],
        tools: list[JsonObject],
        on_text_delta: TextDeltaHandler,
    ) -> ModelReply:
        """Consume Chat Completions SSE and rebuild text plus indexed tool calls."""
        payload = self._request_payload(messages, tools, stream=True)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(stream=True)

        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                self.endpoint, data=encoded, headers=headers, method="POST"
            )
            emitted = False

            def emit(delta: str) -> None:
                nonlocal emitted
                emitted = True
                on_text_delta(delta)

            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.request_timeout
                ) as response:
                    content_type = str(response.headers.get("Content-Type") or "")
                    if "text/event-stream" not in content_type.lower():
                        raw = response.read().decode("utf-8")
                        return self._parse_response(raw)
                    return self._parse_event_stream(response, emit)
            except urllib.error.HTTPError as exc:
                if not emitted and exc.code in STREAM_FALLBACK_STATUS:
                    exc.read(4_000)
                    return self.complete(messages, tools)
                if not emitted and self._retry_http_error(exc, attempt):
                    continue
                detail = exc.read(4_000).decode("utf-8", errors="replace")
                raise ModelError(
                    f"model endpoint returned HTTP {exc.code}: {self._sanitize(detail)}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if not emitted and attempt < self.retries:
                    self._backoff(attempt)
                    continue
                detail = exc.reason if hasattr(exc, "reason") else exc
                raise ModelError(f"model stream failed: {detail}") from exc
        raise ModelError("model stream failed after retries")

    def _request_payload(
        self, messages: list[Message], tools: list[JsonObject], *, stream: bool
    ) -> JsonObject:
        payload: JsonObject = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "rivet-code-agent/1.4",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _retry_http_error(self, exc: urllib.error.HTTPError, attempt: int) -> bool:
        if exc.code not in RETRYABLE_STATUS or attempt >= self.retries:
            return False
        exc.read(4_000)
        self._backoff(attempt)
        return True

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 4))

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

        return self._parse_message(message)

    def _parse_message(self, message: JsonObject) -> ModelReply:
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

    def _parse_event_stream(
        self, response: BinaryIO, on_text_delta: TextDeltaHandler
    ) -> ModelReply:
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        data_lines: list[str] = []
        saw_choice = False
        finished = False

        def consume_event() -> bool:
            nonlocal saw_choice, finished
            if not data_lines:
                return False
            data = "\n".join(data_lines)
            data_lines.clear()
            if data.strip() == "[DONE]":
                finished = True
                return True
            try:
                payload: Any = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ModelError("model stream contained invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ModelError("model stream event must be a JSON object")
            if payload.get("error") is not None:
                raise ModelError(
                    f"model stream returned an error: {self._sanitize(str(payload['error']))}"
                )
            choices = payload.get("choices")
            if not isinstance(choices, list):
                raise ModelError("model stream event has invalid choices")
            if not choices:
                return False
            choice = next(
                (
                    item
                    for item in choices
                    if isinstance(item, dict) and item.get("index", 0) == 0
                ),
                None,
            )
            if not isinstance(choice, dict):
                return False
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise ModelError("model stream choice has no delta object")
            saw_choice = True
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ModelError("model stream content delta must be text")
                if content:
                    content_parts.append(content)
                    on_text_delta(content)
            self._merge_tool_deltas(tool_parts, delta.get("tool_calls"))
            if choice.get("finish_reason") is not None:
                finished = True
            return False

        while True:
            raw_line = response.readline()
            if raw_line == b"":
                consume_event()
                break
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise ModelError("model stream was not valid UTF-8") from exc
            if not line:
                if consume_event():
                    break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)

        if not saw_choice:
            raise ModelError("model stream ended without a completion choice")
        if not finished:
            raise ModelError("model stream ended before completion")

        raw_calls: list[JsonObject] = []
        for index in sorted(tool_parts):
            part = tool_parts[index]
            raw_calls.append(
                {
                    "id": part["id"] or f"generated_call_{index}",
                    "type": part["type"] or "function",
                    "function": {
                        "name": part["name"],
                        "arguments": part["arguments"] or "{}",
                    },
                }
            )
        return self._parse_message(
            {
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": raw_calls,
            }
        )

    @staticmethod
    def _merge_tool_deltas(
        tool_parts: dict[int, dict[str, str]], raw_calls: Any
    ) -> None:
        if raw_calls is None:
            return
        if not isinstance(raw_calls, list):
            raise ModelError("model stream tool_calls delta must be a list")
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise ModelError("model stream tool call delta must be an object")
            index = raw_call.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ModelError("model stream tool call delta has an invalid index")
            part = tool_parts.setdefault(
                index,
                {"id": "", "type": "", "name": "", "arguments": ""},
            )
            for field in ("id", "type"):
                value = raw_call.get(field)
                if value is not None:
                    if not isinstance(value, str):
                        raise ModelError(f"model stream tool call {field} must be text")
                    if not part[field]:
                        part[field] = value
                    elif value != part[field]:
                        part[field] += value
            function = raw_call.get("function")
            if function is not None:
                if not isinstance(function, dict):
                    raise ModelError("model stream tool function must be an object")
                for source, target in (("name", "name"), ("arguments", "arguments")):
                    value = function.get(source)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ModelError(
                                f"model stream tool function {source} must be text"
                            )
                        part[target] += value

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

