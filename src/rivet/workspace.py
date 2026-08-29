from __future__ import annotations

import difflib
import hashlib
import os
import re
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError, WorkspaceViolation


DEFAULT_IGNORES = {
    ".git",
    ".rivet",
    ".venv",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

SNAPSHOT_MAX_FILES = 10_000
SNAPSHOT_TEXT_FILE_BYTES = 2_000_000
SNAPSHOT_TEXT_BUDGET_BYTES = 20_000_000
SNAPSHOT_CHANGE_REPORT_LIMIT = 200
SNAPSHOT_IGNORED_FILES = {".coverage", ".DS_Store"}


@dataclass(frozen=True)
class FileSnapshot:
    digest: str
    size: int
    text: str | None
    text_available: bool


@dataclass(frozen=True)
class WorkspaceSnapshot:
    files: dict[Path, FileSnapshot]
    complete: bool

RISKY_COMMANDS = (
    re.compile(r"\b(?:rm|rmdir)\b.*(?:-[a-zA-Z]*r|/s)"),
    re.compile(r"\b(?:del|erase)\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.IGNORECASE),
    re.compile(r"\bgit(?:\s+-\S+)*\s+(?:push|clean|reset\s+--hard)\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b.*(?:--data|--upload-file|-d\s)", re.IGNORECASE),
    re.compile(r"\bscp\b", re.IGNORECASE),
    re.compile(r"^\s*(?:shutdown|reboot|format)(?:\s|$)", re.IGNORECASE),
)

REVIEW_COMMANDS = (
    (
        "downloads or network access",
        re.compile(r"\b(?:curl|wget|Invoke-WebRequest|iwr)\b", re.IGNORECASE),
    ),
    (
        "package installation",
        re.compile(
            r"\b(?:pip|pip3|uv)\s+(?:install|uninstall)|"
            r"\b(?:npm|pnpm|yarn)\s+(?:install|add|remove|ci)|"
            r"\b(?:cargo|gem)\s+install\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Git history or branch mutation",
        re.compile(
            r"\bgit(?:\s+-\S+)*\s+(?:add|commit|checkout|switch|merge|rebase|tag)\b",
            re.IGNORECASE,
        ),
    ),
)

VERIFICATION_COMMANDS = (
    re.compile(
        r"\b(?:python(?:3)?|py)(?:\.exe)?\b.*\s-m\s+"
        r"(?:unittest|pytest|compileall|mypy|ruff|pyright)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:pytest|tox|nox|ruff|mypy|pyright)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:npm|pnpm|yarn)\s+(?:test|run\s+(?:test|build|lint|check|typecheck))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcargo\s+(?:test|check|clippy|build)\b", re.IGNORECASE),
    re.compile(r"\bgo\s+test\b|\bdotnet\s+(?:test|build)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:mvn|gradle|gradlew)\s+(?:test|verify|check|build)\b|"
        r"\bmake\s+(?:test|check|build)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgit\s+diff\s+--check\b", re.IGNORECASE),
)


class Workspace:
    """All filesystem operations are resolved against one trusted root."""

    def __init__(self, root: Path, *, max_output_chars: int = 20_000) -> None:
        self.root = root.resolve()
        self.max_output_chars = max_output_chars
        self._original_text: dict[Path, str | None] = {}
        self._unavailable_diffs: dict[Path, str] = {}

    def export_diff_state(self) -> dict[str, Any]:
        """Return the bounded baseline needed for /diff after session resume."""
        original = []
        for target in sorted(self._original_text, key=lambda item: self.display(item)):
            original.append(
                {
                    "path": self.display(target),
                    "content": self._original_text[target],
                    "current": self._file_identity(target),
                }
            )
        unavailable = []
        for target in sorted(self._unavailable_diffs, key=lambda item: self.display(item)):
            unavailable.append(
                {
                    "path": self.display(target),
                    "reason": self._unavailable_diffs[target],
                    "current": self._file_identity(target),
                }
            )
        return {"original_text": original, "unavailable_diffs": unavailable}

    def restore_diff_state(self, payload: Any) -> list[str]:
        """Restore diff baselines and report files changed since the session was saved."""
        if not isinstance(payload, dict):
            raise ToolError("saved workspace state must be an object")
        original = payload.get("original_text", [])
        unavailable = payload.get("unavailable_diffs", [])
        if not isinstance(original, list) or not isinstance(unavailable, list):
            raise ToolError("saved workspace diff entries must be lists")
        if len(original) + len(unavailable) > SNAPSHOT_MAX_FILES:
            raise ToolError("saved workspace diff contains too many files")

        restored_original: dict[Path, str | None] = {}
        restored_unavailable: dict[Path, str] = {}
        saved_identities: dict[Path, dict[str, Any]] = {}
        text_bytes = 0

        for entry in original:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ToolError("saved text diff entry is invalid")
            target = self.resolve(entry["path"])
            if target in restored_original or target in restored_unavailable:
                raise ToolError(f"duplicate saved diff path: {self.display(target)}")
            content = entry.get("content")
            if content is not None and not isinstance(content, str):
                raise ToolError(f"invalid saved diff content: {self.display(target)}")
            if isinstance(content, str):
                text_bytes += len(content.encode("utf-8"))
                if text_bytes > SNAPSHOT_TEXT_BUDGET_BYTES:
                    raise ToolError("saved workspace diff text exceeds the restore budget")
            restored_original[target] = content
            saved_identities[target] = self._validate_file_identity(entry.get("current"))

        for entry in unavailable:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("reason"), str)
            ):
                raise ToolError("saved unavailable diff entry is invalid")
            target = self.resolve(entry["path"])
            if target in restored_original or target in restored_unavailable:
                raise ToolError(f"duplicate saved diff path: {self.display(target)}")
            restored_unavailable[target] = entry["reason"]
            saved_identities[target] = self._validate_file_identity(entry.get("current"))

        drifted = [
            self.display(target)
            for target, identity in saved_identities.items()
            if self._file_identity(target) != identity
        ]
        self._original_text = restored_original
        self._unavailable_diffs = restored_unavailable
        return sorted(drifted)

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
        self._remember_original(target)
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
        self._remember_original(target, text=text)
        self._atomic_write(target, updated)
        return {
            "path": self.display(target),
            "replacements": count,
            "remaining_matches": occurrences - count,
        }

    def show_diff(self, path: str | None = None, context_lines: int = 3) -> dict[str, Any]:
        if not 0 <= context_lines <= 20:
            raise ToolError("context_lines must be between 0 and 20")
        if path is None:
            tracked = set(self._original_text) | set(self._unavailable_diffs)
            targets = sorted(tracked, key=lambda item: self.display(item))
        else:
            target = self.resolve(path)
            targets = [target] if target in self._original_text or target in self._unavailable_diffs else []

        changed_files: list[str] = []
        diff_parts: list[str] = []
        for target in targets:
            if target not in self._original_text:
                display_path = self.display(target)
                changed_files.append(display_path)
                reason = self._unavailable_diffs[target]
                diff_parts.append(
                    f"--- a/{display_path}\n+++ b/{display_path}\n"
                    f"[diff unavailable: {reason}]\n"
                )
                continue
            original = self._original_text[target]
            try:
                current = target.read_text(encoding="utf-8-sig") if target.exists() else None
            except UnicodeDecodeError as exc:
                raise ToolError(f"file is not UTF-8 text: {self.display(target)}") from exc
            if original == current:
                continue
            display_path = self.display(target)
            changed_files.append(display_path)
            before = [] if original is None else original.splitlines(keepends=True)
            after = [] if current is None else current.splitlines(keepends=True)
            diff_parts.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile="/dev/null" if original is None else f"a/{display_path}",
                    tofile="/dev/null" if current is None else f"b/{display_path}",
                    n=context_lines,
                )
            )

        full_diff = "".join(diff_parts)
        return {
            "files": changed_files,
            "diff": self._truncate(full_diff),
            "truncated": len(full_diff) > self.max_output_chars,
        }

    @staticmethod
    def command_review_reason(command: str) -> str | None:
        for reason, pattern in REVIEW_COMMANDS:
            if pattern.search(command):
                return reason
        return None

    @staticmethod
    def is_verification_command(command: str) -> bool:
        return any(pattern.search(command) for pattern in VERIFICATION_COMMANDS)

    def run_command(
        self, command: str, timeout: int, purpose: str = "auto"
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        if len(command) > 10_000:
            raise ToolError("command must not exceed 10000 characters")
        if "\n" in command or "\r" in command:
            raise ToolError("multi-line commands are not allowed")
        if any(pattern.search(command) for pattern in RISKY_COMMANDS):
            raise ToolError("command blocked by safety policy; perform a narrower operation")
        if not 1 <= timeout <= 600:
            raise ToolError("timeout must be between 1 and 600 seconds")
        if purpose not in {"auto", "inspect", "verify"}:
            raise ToolError("purpose must be auto, inspect, or verify")

        environment = {
            key: value
            for key, value in os.environ.items()
            if not re.search(
                r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)|^(?:RIVET|OPENAI)_",
                key,
                re.IGNORECASE,
            )
        }
        verification = purpose == "verify" or (
            purpose == "auto" and self.is_verification_command(command)
        )
        before = self._capture_snapshot(capture_text=True)
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        process = subprocess.Popen(
            command,
            cwd=self.root,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate()
            command_result: dict[str, Any] = {
                "command": command,
                "exit_code": None,
                "timed_out": True,
                "cancelled": False,
                "verification": verification,
                "purpose": purpose,
                "stdout": self._truncate(self._output_text(stdout)),
                "stderr": self._truncate(self._output_text(stderr)),
            }
        except KeyboardInterrupt:
            self._terminate_process_tree(process)
            stdout, stderr = process.communicate()
            command_result = {
                "command": command,
                "exit_code": None,
                "timed_out": False,
                "cancelled": True,
                "verification": verification,
                "purpose": purpose,
                "stdout": self._truncate(self._output_text(stdout)),
                "stderr": self._truncate(self._output_text(stderr)),
            }
        else:
            command_result = {
                "command": command,
                "exit_code": process.returncode,
                "timed_out": False,
                "cancelled": False,
                "verification": verification,
                "purpose": purpose,
                "stdout": self._truncate(stdout),
                "stderr": self._truncate(stderr),
            }

        try:
            after = self._capture_snapshot(capture_text=False)
            changes = self._record_command_changes(before, after)
        except KeyboardInterrupt:
            command_result["cancelled"] = True
            after = self._capture_snapshot(capture_text=False)
            changes = self._record_command_changes(before, after)
        reported_changes = changes[:SNAPSHOT_CHANGE_REPORT_LIMIT]
        changes_truncated = len(changes) > len(reported_changes)
        command_result["file_changes"] = reported_changes
        command_result["file_change_count"] = len(changes)
        command_result["file_changes_truncated"] = changes_truncated
        command_result["tracking_complete"] = (
            before.complete and after.complete and not changes_truncated
        )
        return command_result

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Best-effort termination of the shell and descendants on Windows and POSIX."""
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _capture_snapshot(self, *, capture_text: bool) -> WorkspaceSnapshot:
        files: dict[Path, FileSnapshot] = {}
        text_budget = SNAPSHOT_TEXT_BUDGET_BYTES
        complete = True

        for current, dirnames, filenames in os.walk(self.root):
            current_path = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in DEFAULT_IGNORES and not (current_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                if filename in SNAPSHOT_IGNORED_FILES:
                    continue
                if len(files) >= SNAPSHOT_MAX_FILES:
                    return WorkspaceSnapshot(files, False)
                target = current_path / filename
                if target.is_symlink() or not target.is_file():
                    continue
                try:
                    size = target.stat().st_size
                    data: bytes | None = None
                    if size <= SNAPSHOT_TEXT_FILE_BYTES:
                        data = target.read_bytes()
                        digest = hashlib.sha256(data).hexdigest()
                    else:
                        digest_hash = hashlib.sha256()
                        with target.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                digest_hash.update(chunk)
                        digest = digest_hash.hexdigest()

                    text: str | None = None
                    text_available = False
                    if (
                        capture_text
                        and data is not None
                        and len(data) <= text_budget
                        and b"\x00" not in data[:4096]
                    ):
                        try:
                            text = data.decode("utf-8-sig")
                            text_available = True
                            text_budget -= len(data)
                        except UnicodeDecodeError:
                            pass
                    files[target] = FileSnapshot(digest, size, text, text_available)
                except OSError:
                    complete = False
        return WorkspaceSnapshot(files, complete)

    def _record_command_changes(
        self, before: WorkspaceSnapshot, after: WorkspaceSnapshot
    ) -> list[dict[str, Any]]:
        changes: list[tuple[Path, str]] = []
        all_paths = sorted(
            set(before.files) | set(after.files), key=lambda item: self.display(item)
        )
        for target in all_paths:
            old = before.files.get(target)
            new = after.files.get(target)
            if old is None:
                changes.append((target, "created"))
            elif new is None:
                changes.append((target, "deleted"))
            elif old.digest != new.digest:
                changes.append((target, "modified"))

        payload: list[dict[str, Any]] = []
        for target, change in changes:
            self._remember_command_baseline(target, change, before.files.get(target))
            payload.append(
                {
                    "path": self.display(target),
                    "change": change,
                    "diff_available": target in self._original_text,
                }
            )
        return payload

    def _remember_command_baseline(
        self, target: Path, change: str, old: FileSnapshot | None
    ) -> None:
        if target in self._original_text or target in self._unavailable_diffs:
            return
        if change == "created":
            try:
                data = target.read_bytes()
                if (
                    len(data) <= SNAPSHOT_TEXT_FILE_BYTES
                    and b"\x00" not in data[:4096]
                ):
                    data.decode("utf-8-sig")
                    self._original_text[target] = None
                    return
            except (OSError, UnicodeDecodeError):
                pass
            self._unavailable_diffs[target] = "created file is binary, oversized, or unreadable"
            return

        if old is not None and old.text_available:
            self._original_text[target] = old.text
        else:
            self._unavailable_diffs[target] = (
                f"original content for {change} file was binary, oversized, or outside snapshot budget"
            )

    def _remember_original(self, target: Path, *, text: str | None = None) -> None:
        if target in self._original_text:
            return
        if not target.exists():
            self._original_text[target] = None
            return
        if not target.is_file():
            raise ToolError(f"not a file: {self.display(target)}")
        try:
            original = text if text is not None else target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {self.display(target)}") from exc
        self._original_text[target] = original

    @staticmethod
    def _validate_file_identity(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ToolError("saved file identity must be an object")
        kind = value.get("kind")
        if kind not in {"missing", "file", "other", "unreadable"}:
            raise ToolError("saved file identity has an invalid kind")
        digest = value.get("sha256")
        if kind == "file":
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ToolError("saved file identity has an invalid digest")
            return {"kind": kind, "sha256": digest}
        if digest is not None:
            raise ToolError("saved non-file identity must not contain a digest")
        return {"kind": kind, "sha256": None}

    @staticmethod
    def _file_identity(target: Path) -> dict[str, Any]:
        try:
            if not target.exists():
                return {"kind": "missing", "sha256": None}
            if target.is_symlink() or not target.is_file():
                return {"kind": "other", "sha256": None}
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"kind": "file", "sha256": digest.hexdigest()}
        except OSError:
            return {"kind": "unreadable", "sha256": None}

    @staticmethod
    def _output_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

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
