from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .errors import RivetError, ToolError
from .types import JsonObject
from .workspace import Workspace


Approver = Callable[[str, str], bool]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject
    handler: Callable[..., dict[str, Any]]
    mutating: bool = False

    def api_schema(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, config: Config, approver: Approver | None = None) -> None:
        self.config = config
        self.workspace = Workspace(
            config.workspace, max_output_chars=config.max_tool_output_chars
        )
        self.approver = approver
        self._tools = {tool.name: tool for tool in self._build_tools()}

    @property
    def schemas(self) -> list[JsonObject]:
        return [tool.api_schema() for tool in self._tools.values()]

    def execute(self, name: str, raw_arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return self._json_error(f"unknown tool: {name}")
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return self._json_error(f"arguments are not valid JSON: {exc.msg}")
        if not isinstance(arguments, dict):
            return self._json_error("tool arguments must be a JSON object")

        if tool.mutating:
            if self.config.approval_mode == "never":
                return self._json_error("mutating tools are disabled by approval mode")
            if self.config.approval_mode == "ask":
                summary = json.dumps(arguments, ensure_ascii=False)[:500]
                if self.approver is None or not self.approver(name, summary):
                    return self._json_error("operation was not approved by the user")

        try:
            result = tool.handler(**arguments)
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except TypeError as exc:
            return self._json_error(f"invalid arguments: {exc}")
        except (RivetError, OSError) as exc:
            return self._json_error(str(exc))
        except Exception as exc:  # defensive boundary: never crash the agent loop
            return self._json_error(f"unexpected {type(exc).__name__}: {exc}")

    @staticmethod
    def _json_error(message: str) -> str:
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

    def _build_tools(self) -> tuple[ToolSpec, ...]:
        object_schema = {"type": "object", "additionalProperties": False}
        return (
            ToolSpec(
                "list_files",
                "List workspace files and directories recursively. Hidden build/cache directories are skipped.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative directory"},
                        "depth": {"type": "integer", "minimum": 0, "maximum": 8},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000},
                    },
                },
                self.workspace.list_files,
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file with stable one-based line numbers.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                self.workspace.read_file,
            ),
            ToolSpec(
                "search_text",
                "Search UTF-8 workspace files for literal text and return matching lines.",
                {
                    **object_schema,
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "file_glob": {"type": "string", "description": "For example *.py"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                        "case_sensitive": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
                self.workspace.search_text,
            ),
            ToolSpec(
                "write_file",
                "Create or replace a UTF-8 file atomically. Parent directories are created.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                self.workspace.write_file,
                mutating=True,
            ),
            ToolSpec(
                "replace_text",
                "Atomically replace exact text in a UTF-8 file. Fails if the old text is absent.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                        "count": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "old", "new"],
                },
                self.workspace.replace_text,
                mutating=True,
            ),
            ToolSpec(
                "run_command",
                "Run a shell command in the workspace. Output is captured, bounded, and returned with exit status.",
                {
                    **object_schema,
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                    },
                    "required": ["command"],
                },
                lambda command, timeout=self.config.command_timeout: self.workspace.run_command(
                    command, timeout
                ),
                mutating=True,
            ),
        )
