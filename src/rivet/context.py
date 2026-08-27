from __future__ import annotations

import json
from typing import Any

from .types import Message


class ContextManager:
    """Keeps complete tool-call units while bounding approximate context size."""

    SUMMARY_NAME = "rivet_context"

    def __init__(self, system: str, task: str, max_chars: int) -> None:
        self.max_chars = max_chars
        self.messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]

    def append(self, message: Message) -> None:
        self.messages.append(message)

    def compact(self) -> bool:
        if self._size(self.messages) <= self.max_chars:
            return False

        fixed = self.messages[:2]
        body = self.messages[2:]
        previous_summary = ""
        if body and body[0].get("name") == self.SUMMARY_NAME:
            previous_summary = str(body.pop(0).get("content") or "")

        units = self._group_units(body)
        keep: list[list[Message]] = []
        target = max(self.max_chars * 3 // 5, 4_000)
        running = self._size(fixed)
        for unit in reversed(units):
            unit_size = self._size(unit)
            if keep and running + unit_size > target:
                break
            keep.append(unit)
            running += unit_size
        keep.reverse()
        dropped_count = len(units) - len(keep)
        if dropped_count <= 0:
            return False

        dropped = units[:dropped_count]
        summary_parts = [previous_summary] if previous_summary else []
        summary_parts.extend(self._summarize_unit(unit) for unit in dropped)
        summary = "\n".join(part for part in summary_parts if part)
        if len(summary) > 8_000:
            summary = "[older summary omitted]\n" + summary[-8_000:]
        summary_message: Message = {
            "role": "system",
            "name": self.SUMMARY_NAME,
            "content": "[Compacted prior activity; facts only]\n" + summary,
        }
        self.messages = fixed + [summary_message] + [message for unit in keep for message in unit]
        return True

    @staticmethod
    def _size(messages: list[Message]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _group_units(messages: list[Message]) -> list[list[Message]]:
        units: list[list[Message]] = []
        index = 0
        while index < len(messages):
            current = messages[index]
            unit = [current]
            if current.get("role") == "assistant" and current.get("tool_calls"):
                expected = {
                    call.get("id")
                    for call in current.get("tool_calls", [])
                    if isinstance(call, dict)
                }
                index += 1
                while index < len(messages):
                    candidate = messages[index]
                    if candidate.get("role") != "tool" or candidate.get("tool_call_id") not in expected:
                        break
                    unit.append(candidate)
                    index += 1
                units.append(unit)
                continue
            units.append(unit)
            index += 1
        return units

    @staticmethod
    def _summarize_unit(unit: list[Message]) -> str:
        pieces: list[str] = []
        for message in unit:
            role = str(message.get("role", "unknown"))
            if role == "assistant" and message.get("tool_calls"):
                names = [
                    str(call.get("function", {}).get("name", "unknown"))
                    for call in message["tool_calls"]
                    if isinstance(call, dict)
                ]
                pieces.append(f"assistant called {', '.join(names)}")
            elif role == "tool":
                content = str(message.get("content") or "").replace("\n", " ")
                pieces.append(f"tool {message.get('name', '')}: {content[:350]}")
            else:
                content = str(message.get("content") or "").replace("\n", " ")
                pieces.append(f"{role}: {content[:350]}")
        return " | ".join(pieces)

