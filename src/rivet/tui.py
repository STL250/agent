from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .agent import AgentResult
from .config import Config
from .session import SessionSummary
from .types import JsonObject


def configure_terminal_encoding() -> None:
    """Prefer UTF-8 for the Windows CLI while preserving redirected stream behavior."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class Console:
    """Small dependency-free terminal renderer with graceful plain-text fallback."""

    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    CYAN = "\x1b[36m"

    TOOL_LABELS = {
        "update_plan": "Plan",
        "list_files": "List",
        "read_file": "Read",
        "search_text": "Search",
        "write_file": "Write",
        "replace_text": "Edit",
        "show_diff": "Diff",
        "run_command": "Run",
    }
    UNICODE_GLYPHS = {
        "top": "┌─",
        "side": "│",
        "bottom": "└─",
        "prompt": "› ",
        "working": "•",
        "arrow": "→",
        "success": "✓",
        "failure": "×",
        "notice": "•",
        "pipe": "│",
        "pending": "○",
        "current": "●",
        "blocked": "!",
    }
    ASCII_GLYPHS = {
        "top": "+-",
        "side": "|",
        "bottom": "+-",
        "prompt": "> ",
        "working": "*",
        "arrow": "->",
        "success": "ok",
        "failure": "x",
        "notice": "*",
        "pipe": "|",
        "pending": "o",
        "current": "*",
        "blocked": "!",
    }

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        color: bool | None = None,
    ) -> None:
        self.stream = stream
        self.error_stream = error_stream
        self._color = color
        self._unicode = self._supports_unicode()
        self._stream_active = False
        self._stream_parts: list[str] = []
        self._last_stream_text = ""
        if stream is None and color is not False and os.name == "nt":
            self._enable_windows_vt()

    @property
    def output(self) -> TextIO:
        return self.stream or sys.stdout

    @property
    def errors(self) -> TextIO:
        return self.error_stream or sys.stderr

    @property
    def color(self) -> bool:
        if self._color is not None:
            return self._color
        return (
            "NO_COLOR" not in os.environ
            and os.getenv("TERM", "").lower() != "dumb"
            and bool(getattr(self.output, "isatty", lambda: False)())
        )

    def style(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + self.RESET

    def glyph(self, name: str) -> str:
        glyphs = self.UNICODE_GLYPHS if self._unicode else self.ASCII_GLYPHS
        return glyphs[name]

    def banner(self, config: Config) -> None:
        print(
            self.style(f"{self.glyph('top')} Rivet {__version__}", self.BOLD, self.CYAN),
            file=self.output,
        )
        print(f"{self.glyph('side')}  model      {config.model}", file=self.output)
        print(f"{self.glyph('side')}  protocol   {config.protocol}", file=self.output)
        print(f"{self.glyph('side')}  workspace  {config.workspace}", file=self.output)
        print(f"{self.glyph('side')}  approval   {config.approval_mode}", file=self.output)
        print(
            f"{self.glyph('bottom')} /help for commands | Ctrl+C cancels | /exit quits",
            file=self.output,
        )

    def prompt(self) -> str:
        return self.style("\n" + self.glyph("prompt"), self.BOLD, self.GREEN)

    def event(self, event: str, data: dict[str, Any]) -> None:
        if event == "model_start":
            self._last_stream_text = ""
            turn = data.get("turn", 1)
            step = data["step"]
            label = self.style(f"{self.glyph('working')} Working", self.YELLOW)
            meta = self.style(f"turn {turn} | step {step}", self.DIM)
            print(f"\n{label}  {meta}", file=self.output)
        elif event == "context_compacted":
            detail = f"context compacted to {data['messages']} messages"
            print(self.style(f"  └ {detail}", self.DIM), file=self.output)
        elif event == "assistant_stream_start":
            self._stream_active = True
            self._stream_parts = []
            print(f"\n{self.style('rivet', self.BOLD)}", file=self.output)
        elif event == "assistant_text_delta":
            text = str(data.get("text") or "")
            self._stream_parts.append(text)
            print(text, end="", file=self.output, flush=True)
        elif event == "assistant_stream_end":
            if self._stream_active:
                print(file=self.output)
            self._stream_active = False
            self._last_stream_text = "".join(self._stream_parts)
        elif event == "assistant_text" and data["text"].strip():
            if data.get("has_tool_calls") and not data.get("streamed"):
                self._text_block("rivet", data["text"].strip())
        elif event == "tool_start":
            label = self.TOOL_LABELS.get(data["name"], data["name"])
            detail = self._tool_arguments(data["name"], data["arguments"])
            prefix = self.style(self.glyph("arrow"), self.CYAN)
            print(f"  {prefix} {label}" + (f"  {detail}" if detail else ""), file=self.output)
        elif event == "tool_end":
            self._tool_result(data["name"], data["result"])
        elif event == "plan_updated":
            self.plan(data, title="Plan updated")
        elif event == "plan_completion_required":
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(
                f"  {marker} Active plan still has unfinished steps",
                file=self.output,
            )
        elif event == "verification_required":
            files = ", ".join(data.get("files", [])) or "changed files"
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(f"  {marker} Verification required  {files}", file=self.output)
        elif event == "cancelled":
            phase = str(data.get("phase") or "current operation")
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(f"\n{marker} Cancelled  {phase}", file=self.output)

    def turn_result(self, result: AgentResult, turn: int) -> None:
        if result.success:
            marker = self.style(self.glyph("success"), self.GREEN, self.BOLD)
            label = self.style("Completed", self.GREEN, self.BOLD)
        elif result.reason == "cancelled":
            marker = self.style("!", self.YELLOW, self.BOLD)
            label = self.style("Cancelled", self.YELLOW, self.BOLD)
        else:
            marker = self.style(self.glyph("failure"), self.RED, self.BOLD)
            label = self.style("Stopped", self.RED, self.BOLD)
        meta = self.style(f"turn {turn} | {result.steps} step(s)", self.DIM)
        print(f"\n{marker} {label}  {meta}", file=self.output)
        if self._last_stream_text.strip() != result.final.strip():
            self._text_block("rivet", result.final)
        self._last_stream_text = ""

    def session_saved(self, path: Path) -> None:
        print(self.style(f"  session  {path}", self.DIM), file=self.output)

    def session_resumed(self, summary: SessionSummary, drifted: list[str]) -> None:
        self.notice(
            f"Resumed {summary.session_id} | {summary.turns} turn(s) | "
            f"{summary.total_steps} step(s)."
        )
        if drifted:
            files = ", ".join(drifted[:5])
            suffix = " ..." if len(drifted) > 5 else ""
            self.warning(
                f"{len(drifted)} tracked file(s) changed while the session was closed: "
                f"{files}{suffix}. Verification is pending again."
            )

    def sessions(self, sessions: list[SessionSummary]) -> None:
        print(self.style("\nSaved sessions", self.BOLD), file=self.output)
        if not sessions:
            print("  none", file=self.output)
            return
        for index, session in enumerate(sessions, start=1):
            preview = session.task_preview or "(empty task)"
            print(
                f"  {index:>2}. {self.style(session.session_id, self.CYAN)}"
                f"  {session.turns} turn(s)  {preview}",
                file=self.output,
            )
        print(self.style("  Use /resume [session-id]", self.DIM), file=self.output)

    def help(self) -> None:
        print(self.style("\nCommands", self.BOLD), file=self.output)
        rows = (
            ("/help", "show this help"),
            ("/status", "show conversation and verification state"),
            ("/plan", "show the current task plan and progress"),
            ("/diff [path]", "show changes made in this conversation"),
            ("/sessions", "list recent saved conversations"),
            ("/resume [id]", "resume the latest or selected conversation"),
            ("/new", "start a fresh conversation"),
            ("/exit", "quit Rivet"),
        )
        for command, description in rows:
            padding = " " * max(1, 18 - len(command))
            print(
                f"  {self.style(command, self.CYAN)}{padding}{description}",
                file=self.output,
            )

    def status(self, status: JsonObject) -> None:
        changed = ", ".join(status["changed_files"]) or "none"
        inspected = ", ".join(status["inspected_files"]) or "none"
        if not status["verification_required"]:
            verification = "not required"
        elif status["verification_passed"]:
            verification = "passed"
        else:
            verification = "pending"
        print(self.style("\nStatus", self.BOLD), file=self.output)
        print(
            f"  turns       {status['turns']}  |  steps {status['total_steps']}"
            f"  |  messages {status['messages']}",
            file=self.output,
        )
        print(f"  inspected   {inspected}", file=self.output)
        print(f"  changed     {changed}", file=self.output)
        print(f"  verification {verification}", file=self.output)
        tracking = "complete" if status.get("workspace_tracking_complete", True) else "limited"
        print(f"  tracking    {tracking}", file=self.output)
        plan = status.get("plan", {})
        if isinstance(plan, dict) and plan.get("active"):
            counts = plan.get("counts", {})
            completed = counts.get("completed", 0) if isinstance(counts, dict) else 0
            total = len(plan.get("steps", [])) if isinstance(plan.get("steps"), list) else 0
            plan_status = "blocked" if plan.get("blocked") else f"{completed}/{total} completed"
        else:
            plan_status = "none"
        print(f"  plan        {plan_status}", file=self.output)

    def plan(self, plan: JsonObject, *, title: str = "Plan") -> None:
        print(self.style(f"\n{title}", self.BOLD), file=self.output)
        steps = plan.get("steps", [])
        if not isinstance(steps, list) or not steps:
            print("  no active plan", file=self.output)
            return
        explanation = str(plan.get("explanation") or "").strip()
        if explanation:
            print(self.style(f"  {explanation}", self.DIM), file=self.output)
        for index, item in enumerate(steps, start=1):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "pending")
            text = str(item.get("step") or "")
            if status == "completed":
                marker = self.style(self.glyph("success"), self.GREEN, self.BOLD)
            elif status == "in_progress":
                marker = self.style(self.glyph("current"), self.CYAN, self.BOLD)
            elif status == "blocked":
                marker = self.style(self.glyph("blocked"), self.RED, self.BOLD)
            else:
                marker = self.style(self.glyph("pending"), self.DIM)
            print(f"  {marker} {index}. {text}", file=self.output)

    def diff(self, result: JsonObject, path: str | None) -> None:
        print(self.style("\nDiff", self.BOLD), file=self.output)
        if not result["files"]:
            detail = "no tracked changes" if path is None else f"no tracked changes for {path}"
            print(f"  {detail}", file=self.output)
            return
        print(str(result["diff"]).rstrip(), file=self.output)
        if result["truncated"]:
            print(self.style("  output truncated", self.YELLOW), file=self.output)

    def notice(self, text: str) -> None:
        print(
            f"\n{self.style(self.glyph('notice'), self.CYAN)} {text}",
            file=self.output,
        )

    def warning(self, text: str) -> None:
        print(f"\n{self.style('!', self.YELLOW, self.BOLD)} {text}", file=self.output)

    def error(self, text: str) -> None:
        marker = self.style(self.glyph("failure"), self.RED, self.BOLD)
        print(f"{marker} {text}", file=self.errors)

    def goodbye(self) -> None:
        print(self.style("\nSession ended.", self.DIM), file=self.output)

    def input_cancelled(self) -> None:
        self.notice("Input cleared. Type /exit to quit.")

    def approve(self, tool: str, summary: str) -> bool:
        short = self._truncate(summary.replace("\n", " "), 180)
        prompt = self.style(f"\n? Approve {tool}  {short}? [y/N] ", self.YELLOW, self.BOLD)
        answer = input(prompt).strip().lower()
        return answer in {"y", "yes"}

    def _tool_result(self, name: str, raw_result: str) -> None:
        try:
            result = json.loads(raw_result)
        except json.JSONDecodeError:
            marker = self.style(self.glyph("failure"), self.RED)
            print(f"    {marker} invalid tool result", file=self.output)
            return

        if not result.get("ok"):
            cancelled = result.get("cancelled") is True
            marker = self.style(
                "!" if cancelled else self.glyph("failure"),
                self.YELLOW if cancelled else self.RED,
            )
            detail = self._truncate(str(result.get("error") or "tool failed"), 180)
            print(f"    {marker} {detail}", file=self.output)
            return

        command_cancelled = name == "run_command" and result.get("cancelled") is True
        command_failed = name == "run_command" and (
            result.get("timed_out") or result.get("exit_code") != 0
        )
        if command_cancelled:
            marker = self.style("!", self.YELLOW)
        elif command_failed:
            marker = self.style(self.glyph("failure"), self.RED)
        else:
            marker = self.style(self.glyph("success"), self.GREEN)
        detail = self._tool_result_detail(name, result)
        print(f"    {marker}" + (f" {detail}" if detail else " done"), file=self.output)
        if name == "run_command":
            for line in self._command_excerpt(result):
                pipe = self.glyph("pipe")
                print(self.style(f"      {pipe} {line}", self.DIM), file=self.output)

    @classmethod
    def _tool_arguments(cls, name: str, raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return cls._truncate(raw_arguments.replace("\n", " "), 120)
        if not isinstance(arguments, dict):
            return cls._truncate(str(arguments), 120)
        if name == "run_command":
            return cls._truncate(str(arguments.get("command") or ""), 120)
        if name == "update_plan":
            steps = arguments.get("steps", [])
            count = len(steps) if isinstance(steps, list) else 0
            explanation = cls._truncate(str(arguments.get("explanation") or ""), 80)
            return f"{count} step(s)" + (f" | {explanation}" if explanation else "")
        if name == "search_text":
            query = cls._truncate(str(arguments.get("query") or ""), 70)
            path = str(arguments.get("path") or ".")
            return f"{query!r} in {path}"
        path = arguments.get("path")
        return cls._truncate(str(path), 120) if path else ""

    @classmethod
    def _tool_result_detail(cls, name: str, result: JsonObject) -> str:
        if name == "read_file":
            return f"{result.get('path', '')} | {result.get('total_lines', 0)} lines"
        if name == "list_files":
            return f"{len(result.get('entries', []))} entries"
        if name == "search_text":
            return f"{len(result.get('matches', []))} matches"
        if name == "write_file":
            return f"{result.get('action', 'written')} {result.get('path', '')}"
        if name == "replace_text":
            return f"{result.get('replacements', 0)} replacement(s) in {result.get('path', '')}"
        if name == "show_diff":
            return f"{len(result.get('files', []))} changed file(s)"
        if name == "run_command":
            if result.get("cancelled"):
                detail = "cancelled"
            elif result.get("timed_out"):
                detail = "timed out"
            else:
                detail = f"exit {result.get('exit_code')}"
            if result.get("verification"):
                detail += " | verification"
            change_count = result.get("file_change_count")
            if not isinstance(change_count, int):
                changes = result.get("file_changes", [])
                change_count = len(changes) if isinstance(changes, list) else 0
            if change_count:
                detail += f" | {change_count} file change(s)"
            if result.get("tracking_complete") is False:
                detail += " | tracking limited"
            return detail
        if name == "update_plan":
            steps = result.get("steps", [])
            count = len(steps) if isinstance(steps, list) else 0
            return f"{count} step(s)" + (" updated" if result.get("changed") else " unchanged")
        return str(result.get("path") or "done")

    def _text_block(self, speaker: str, text: str) -> None:
        print(f"\n{self.style(speaker, self.BOLD)}", file=self.output)
        print(text.strip(), file=self.output)

    @classmethod
    def _command_excerpt(cls, result: JsonObject) -> list[str]:
        lines: list[str] = []
        for key in ("stdout", "stderr"):
            value = str(result.get(key) or "")
            lines.extend(line.strip() for line in value.splitlines() if line.strip())
        return [cls._truncate(line, 140) for line in lines[-3:]]

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _supports_unicode(self) -> bool:
        encoding = getattr(self.output, "encoding", None)
        if not encoding:
            return True
        try:
            "┌─│└›•→✓×".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    @staticmethod
    def _enable_windows_vt() -> None:
        """Enable ANSI processing on older Windows console hosts when available."""
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except (AttributeError, OSError):
            pass
