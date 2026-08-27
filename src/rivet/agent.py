from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import Config
from .context import ContextManager
from .prompt import system_prompt
from .tools import Approver, ToolRegistry
from .types import EventHandler, Message, ModelClient, ToolCall


@dataclass(frozen=True)
class AgentResult:
    success: bool
    final: str
    steps: int
    reason: str
    messages: tuple[Message, ...]


class Agent:
    def __init__(
        self,
        config: Config,
        client: ModelClient,
        *,
        event_handler: EventHandler | None = None,
        approver: Approver | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.events = event_handler or (lambda _event, _data: None)
        self.tools = ToolRegistry(config, approver=approver)

    def run(self, task: str) -> AgentResult:
        if not task.strip():
            return AgentResult(False, "Task is empty.", 0, "empty_task", ())
        context = ContextManager(
            system_prompt(self.config.workspace), task.strip(), self.config.max_context_chars
        )
        previous_signature: str | None = None
        repeat_count = 0
        empty_replies = 0

        for step in range(1, self.config.max_steps + 1):
            compacted = context.compact()
            if compacted:
                self.events("context_compacted", {"messages": len(context.messages)})
            self.events("model_start", {"step": step})
            reply = self.client.complete(context.messages, self.tools.schemas)
            assistant_message = self._assistant_message(reply.content, reply.tool_calls)
            context.append(assistant_message)

            if reply.content.strip():
                self.events("assistant_text", {"text": reply.content})

            if reply.tool_calls:
                empty_replies = 0
                for call in reply.tool_calls:
                    signature = self._signature(call)
                    if signature == previous_signature:
                        repeat_count += 1
                    else:
                        previous_signature = signature
                        repeat_count = 1
                    if repeat_count >= 3:
                        final = f"Stopped after the same tool call was requested three times: {call.name}."
                        self.events("stopped", {"reason": "repeated_tool_call", "tool": call.name})
                        return AgentResult(
                            False, final, step, "repeated_tool_call", tuple(context.messages)
                        )
                    self.events(
                        "tool_start",
                        {"step": step, "name": call.name, "arguments": call.arguments},
                    )
                    result = self.tools.execute(call.name, call.arguments)
                    context.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": result,
                        }
                    )
                    self.events(
                        "tool_end", {"step": step, "name": call.name, "result": result}
                    )
                continue

            if reply.content.strip():
                self.events("completed", {"step": step})
                return AgentResult(True, reply.content.strip(), step, "completed", tuple(context.messages))

            empty_replies += 1
            if empty_replies >= 2:
                final = "Model returned two empty responses without tool calls."
                self.events("stopped", {"reason": "empty_model_response"})
                return AgentResult(False, final, step, "empty_model_response", tuple(context.messages))
            context.append(
                {
                    "role": "user",
                    "content": "Your last response was empty. Continue the task using tools or give a final answer.",
                }
            )

        final = f"Stopped after reaching the {self.config.max_steps}-step limit."
        self.events("stopped", {"reason": "max_steps"})
        return AgentResult(False, final, self.config.max_steps, "max_steps", tuple(context.messages))

    @staticmethod
    def _signature(call: ToolCall) -> str:
        try:
            arguments: Any = json.loads(call.arguments)
            canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            canonical = call.arguments.strip()
        return f"{call.name}:{canonical}"

    @staticmethod
    def _assistant_message(content: str, calls: tuple[ToolCall, ...]) -> Message:
        message: Message = {"role": "assistant", "content": content or None}
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
        return message
