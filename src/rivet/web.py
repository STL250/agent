from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit

from .agent import Agent, AgentResult
from .config import Config
from .errors import ConfigurationError, RivetError
from .session import SessionStore
from .types import JsonObject, ModelClient


MAX_REQUEST_BYTES = 1_000_000
APPROVAL_TIMEOUT_SECONDS = 300
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class WebRuntime:
    """Own one browser session while keeping the Agent UI-independent."""

    def __init__(self, config: Config, client: ModelClient) -> None:
        self.config = config
        self.sessions = SessionStore(config.workspace, model=config.model)
        self.session_path: Path | None = None
        self.token = secrets.token_urlsafe(32)
        self.turn_lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.approval_condition = threading.Condition()
        self.pending_approval: JsonObject | None = None
        self.writer: BinaryIO | None = None
        self.agent = Agent(
            config,
            client,
            event_handler=self._agent_event,
            approver=self._approve,
        )

    def snapshot(self) -> JsonObject:
        return {
            "config": {
                "model": self.config.model,
                "protocol": self.config.protocol,
                "workspace": str(self.config.workspace),
                "workspace_name": self.config.workspace.name,
                "approval": self.config.approval_mode,
            },
            "status": self.agent.status(),
            "plan": self.agent.plan_snapshot(),
            "diff": self.agent.show_diff(),
            "sessions": [self._session_payload(item) for item in self.sessions.list_sessions()],
            "conversation": self._visible_conversation(),
            "busy": self.turn_lock.locked(),
            "session_id": self.session_path.stem if self.session_path else None,
        }

    def run_turn(self, task: str, writer: BinaryIO) -> None:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("Rivet is already processing another turn")
        try:
            self.writer = writer
            self._emit("turn_started", {"task": task})
            try:
                result = self.agent.run(task)
                first_save = self.session_path is None
                try:
                    self.session_path = self.sessions.save(
                        self.agent, result, target=self.session_path
                    )
                except RivetError as exc:
                    self._emit("session_error", {"message": str(exc)})
                if first_save and self.session_path is not None:
                    self._emit("session_saved", {"id": self.session_path.stem})
                self._write_record(
                    {
                        "type": "turn_complete",
                        "result": self._result_payload(result),
                        "snapshot": self.snapshot(),
                    }
                )
            except RivetError as exc:
                self._write_record({"type": "fatal_error", "message": str(exc)})
            except Exception:
                print("Unexpected error while processing a web turn", file=sys.stderr)
                self._write_record(
                    {"type": "fatal_error", "message": "Unexpected local server error"}
                )
        finally:
            with self.approval_condition:
                if self.pending_approval is not None:
                    self.pending_approval["approved"] = False
                    self.approval_condition.notify_all()
                self.pending_approval = None
            self.writer = None
            self.turn_lock.release()

    def new_session(self) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            self.agent.reset()
            self.session_path = None
            return self.snapshot()
        finally:
            self.turn_lock.release()

    def resume_session(self, reference: str | None) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            loaded = self.sessions.load(reference or None)
            drifted = self.agent.restore_session_state(loaded.agent_state)
            self.session_path = loaded.summary.path
            snapshot = self.snapshot()
            snapshot["resume"] = {
                "id": loaded.summary.session_id,
                "drifted": drifted,
                "saved_model": loaded.summary.model,
            }
            return snapshot
        finally:
            self.turn_lock.release()

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        with self.approval_condition:
            pending = self.pending_approval
            if pending is None or pending.get("id") != approval_id:
                return False
            pending["approved"] = approved
            self.approval_condition.notify_all()
            return True

    def _approve(self, tool: str, summary: str) -> bool:
        approval_id = secrets.token_urlsafe(12)
        with self.approval_condition:
            self.pending_approval = {
                "id": approval_id,
                "tool": tool,
                "summary": summary,
                "approved": None,
            }
            self._emit(
                "approval_required",
                {"id": approval_id, "tool": tool, "summary": summary},
            )
            resolved = self.approval_condition.wait_for(
                lambda: self.pending_approval is None
                or self.pending_approval.get("approved") is not None,
                timeout=APPROVAL_TIMEOUT_SECONDS,
            )
            approved = bool(
                resolved
                and self.pending_approval is not None
                and self.pending_approval.get("approved") is True
            )
            self.pending_approval = None
            return approved

    def _agent_event(self, event: str, data: JsonObject) -> None:
        self._emit(event, data)

    def _emit(self, event: str, data: JsonObject) -> None:
        self._write_record({"type": "event", "event": event, "data": data})

    def _write_record(self, payload: JsonObject) -> None:
        writer = self.writer
        if writer is None:
            return
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self.writer_lock:
            try:
                writer.write(encoded)
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.writer = None

    def _visible_conversation(self) -> list[JsonObject]:
        visible: list[JsonObject] = []
        task_index = 0
        for message in self.agent.messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                if task_index >= len(self.agent.tasks) or content != self.agent.tasks[task_index]:
                    continue
                task_index += 1
                visible.append({"role": role, "content": content})
            elif role == "assistant":
                visible.append({"role": role, "content": content})
        return visible

    @staticmethod
    def _session_payload(summary: Any) -> JsonObject:
        payload = asdict(summary)
        payload.pop("path", None)
        return payload

    @staticmethod
    def _result_payload(result: AgentResult) -> JsonObject:
        return {
            "success": result.success,
            "final": result.final,
            "steps": result.steps,
            "reason": result.reason,
            "state": result.state,
        }


class RivetWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: WebRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, RivetRequestHandler)


class RivetRequestHandler(BaseHTTPRequestHandler):
    server: RivetWebServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            if not self._authorized():
                return
            if parsed.path == "/api/bootstrap":
                self._json_response(self.server.runtime.snapshot())
                return
            if parsed.path == "/api/diff":
                query = parse_qs(parsed.query)
                path = query.get("path", [None])[0]
                try:
                    result = self.server.runtime.agent.show_diff(path)
                except RivetError as exc:
                    self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
                    return
                self._json_response(result)
                return
            self._json_error("API route not found", HTTPStatus.NOT_FOUND)
            return
        self._serve_asset(parsed.path)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not self._authorized():
            return
        try:
            body = self._json_body()
        except ValueError as exc:
            self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
            return

        runtime = self.server.runtime
        if parsed.path == "/api/turn":
            task = body.get("message")
            if not isinstance(task, str) or not task.strip():
                self._json_error("message must be non-empty text", HTTPStatus.BAD_REQUEST)
                return
            if len(task) > 100_000:
                self._json_error("message is too long", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            if runtime.turn_lock.locked():
                self._json_error("Rivet is already working", HTTPStatus.CONFLICT)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                runtime.run_turn(task.strip(), self.wfile)
            except RuntimeError as exc:
                encoded = (
                    json.dumps(
                        {"type": "fatal_error", "message": str(exc)},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                self.wfile.write(encoded)
                self.wfile.flush()
            return

        if parsed.path == "/api/approval":
            approval_id = body.get("id")
            approved = body.get("approved")
            if not isinstance(approval_id, str) or not isinstance(approved, bool):
                self._json_error("invalid approval response", HTTPStatus.BAD_REQUEST)
                return
            if not runtime.resolve_approval(approval_id, approved):
                self._json_error("approval is no longer pending", HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True})
            return

        try:
            if parsed.path == "/api/session/new":
                self._json_response(runtime.new_session())
                return
            if parsed.path == "/api/session/resume":
                reference = body.get("id")
                if reference is not None and not isinstance(reference, str):
                    raise ValueError("session id must be text")
                self._json_response(runtime.resume_session(reference))
                return
        except (RivetError, RuntimeError, ValueError) as exc:
            self._json_error(str(exc), HTTPStatus.CONFLICT)
            return
        self._json_error("API route not found", HTTPStatus.NOT_FOUND)

    def _serve_asset(self, request_path: str) -> None:
        names = {
            "/": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        name = names.get(unquote(request_path))
        if name is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content = files("rivet").joinpath("webui").joinpath(name).read_bytes()
        except (FileNotFoundError, OSError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if name == "index.html":
            content = content.replace(b"__RIVET_TOKEN__", self.server.runtime.token.encode("ascii"))
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def _authorized(self) -> bool:
        host = self.headers.get("Host", "")
        hostname = urlsplit("//" + host).hostname
        if hostname not in LOOPBACK_HOSTS:
            self._json_error("loopback host required", HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.hostname not in LOOPBACK_HOSTS or parsed.port != self.server.server_port:
                self._json_error("cross-origin request rejected", HTTPStatus.FORBIDDEN)
                return False
        if not secrets.compare_digest(
            self.headers.get("X-Rivet-Token", ""), self.server.runtime.token
        ):
            self._json_error("invalid local session token", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _json_body(self) -> JsonObject:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        try:
            payload: Any = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _json_response(self, payload: JsonObject, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _json_error(self, message: str, status: HTTPStatus) -> None:
        self._json_response({"ok": False, "error": message}, status)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def run_web(
    config: Config,
    client: ModelClient,
    *,
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    if not 0 <= port <= 65535:
        raise ConfigurationError("web port must be between 0 and 65535")
    runtime = WebRuntime(config, client)
    try:
        server = RivetWebServer(("127.0.0.1", port), runtime)
    except OSError as exc:
        raise ConfigurationError(f"could not start local web UI: {exc}") from exc
    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Rivet Web is running at {url}")
    print("Press Ctrl+C to stop the local server.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nRivet Web stopped.")
    finally:
        server.server_close()
    return 0
