from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .errors import ToolError, WorkspaceViolation


DEFAULT_IGNORES = {
    ".git",
    ".rivet",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

RISKY_COMMANDS = (
    re.compile(r"\b(?:rm|rmdir)\b.*(?:-[a-zA-Z]*r|/s)"),
    re.compile(r"\b(?:del|erase)\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.IGNORECASE),
    re.compile(r"\bgit\s+(?:push|clean|reset\s+--hard)\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b.*(?:--data|--upload-file|-d\s)", re.IGNORECASE),
    re.compile(r"\b(?:scp|shutdown|reboot|format)\b", re.IGNORECASE),
)


class Workspace:
    """All filesystem operations are resolved against one trusted root."""

    def __init__(self, root: Path, *, max_output_chars: int = 20_000) -> None:
        self.root = root.resolve()
        self.max_output_chars = max_output_chars

    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ToolError("path must be a non-empty string")
        supplied = Path(relative_path)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        try:
            resolved = candidate.resolve(strict=must_exist)
        except OSError as exc:
            raise ToolError(f"cannot resolve path: {relative_path}: {exc}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {relative_path}") from exc
        return resolved

    def display(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        data = target.read_bytes()
        if b"\x00" in data[:4096]:
            raise ToolError(f"binary file is not readable as text: {path}")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {path}") from exc
        lines = text.splitlines()
        if start_line < 1:
            raise ToolError("start_line must be at least 1")
        final = len(lines) if end_line is None else end_line
        if final < start_line:
            raise ToolError("end_line must be greater than or equal to start_line")
        selected = lines[start_line - 1 : final]
        numbered = "\n".join(
            f"{number:>6} | {line}"
            for number, line in enumerate(selected, start=start_line)
        )
        return {
            "path": self.display(target),
            "start_line": start_line,
            "end_line": min(final, len(lines)),
            "total_lines": len(lines),
            "content": self._truncate(numbered),
        }

    def list_files(self, path: str = ".", depth: int = 3, max_entries: int = 300) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        if not 0 <= depth <= 8:
            raise ToolError("depth must be between 0 and 8")
        if not 1 <= max_entries <= 2000:
            raise ToolError("max_entries must be between 1 and 2000")

        base_depth = len(target.parts)
        entries: list[str] = []
        for current, dirnames, filenames in os.walk(target):
            current_path = Path(current)
            level = len(current_path.parts) - base_depth
            dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_IGNORES)
            if level >= depth:
                dirnames[:] = []
            for dirname in dirnames:
                entries.append(self.display(current_path / dirname) + "/")
            for filename in sorted(filenames):
                entries.append(self.display(current_path / filename))
            if len(entries) >= max_entries:
                break
        limited = entries[:max_entries]
        return {
            "path": self.display(target),
            "entries": limited,
            "truncated": len(entries) > max_entries,
        }

    def search_text(
        self,
        query: str,
        path: str = ".",
        file_glob: str = "*",
        max_results: int = 100,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        if not query:
            raise ToolError("query must not be empty")
        if not 1 <= max_results <= 1000:
            raise ToolError("max_results must be between 1 and 1000")
        target = self.resolve(path, must_exist=True)
        files = [target] if target.is_file() else target.rglob(file_glob)
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []

        for file_path in files:
            if len(matches) >= max_results:
                break
            if not file_path.is_file() or any(part in DEFAULT_IGNORES for part in file_path.parts):
                continue
            try:
                data = file_path.read_bytes()
                if b"\x00" in data[:4096] or len(data) > 2_000_000:
                    continue
                text = data.decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(
                        {
                            "path": self.display(file_path),
                            "line": number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= max_results:
                        break
        return {"query": query, "matches": matches, "truncated": len(matches) >= max_results}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        self._atomic_write(target, content)
        return {
            "path": self.display(target),
            "action": "updated" if existed else "created",
            "bytes": len(content.encode("utf-8")),
        }

    def replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise ToolError("old text must not be empty")
        if count < 1:
            raise ToolError("count must be at least 1")
        target = self.resolve(path, must_exist=True)
        try:
            text = target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {path}") from exc
        occurrences = text.count(old)
        if occurrences == 0:
            raise ToolError("old text was not found; re-read the file before editing")
        if occurrences < count:
            raise ToolError(f"requested {count} replacements but found only {occurrences}")
        updated = text.replace(old, new, count)
        self._atomic_write(target, updated)
        return {
            "path": self.display(target),
            "replacements": count,
            "remaining_matches": occurrences - count,
        }

    def run_command(self, command: str, timeout: int) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        if any(pattern.search(command) for pattern in RISKY_COMMANDS):
            raise ToolError("command blocked by safety policy; perform a narrower operation")
        if not 1 <= timeout <= 600:
            raise ToolError("timeout must be between 1 and 600 seconds")

        environment = {
            key: value
            for key, value in os.environ.items()
            if not re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", key, re.IGNORECASE)
        }
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._truncate(exc.stdout or "")
            stderr = self._truncate(exc.stderr or "")
            return {
                "command": command,
                "exit_code": None,
                "timed_out": True,
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "command": command,
            "exit_code": completed.returncode,
            "timed_out": False,
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
        }

    def _atomic_write(self, target: Path, content: str) -> None:
        temporary_name: str | None = None
        existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=target.parent, delete=False
            ) as stream:
                stream.write(content)
                temporary_name = stream.name
            if existing_mode is not None:
                os.chmod(temporary_name, existing_mode)
            os.replace(temporary_name, target)
        except OSError as exc:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise ToolError(f"failed to write {self.display(target)}: {exc}") from exc

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        head = self.max_output_chars * 3 // 4
        tail = self.max_output_chars - head
        omitted = len(text) - self.max_output_chars
        return f"{text[:head]}\n...[truncated {omitted} characters]...\n{text[-tail:]}"
