from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .agent import Agent, AgentResult
from .client import OpenAICompatibleClient
from .config import Config
from .errors import RivetError


class Console:
    def event(self, event: str, data: dict[str, Any]) -> None:
        if event == "model_start":
            print(f"\n[step {data['step']}] thinking...")
        elif event == "context_compacted":
            print(f"[context] compacted to {data['messages']} messages")
        elif event == "assistant_text" and data["text"].strip():
            print(data["text"].strip())
        elif event == "tool_start":
            arguments = data["arguments"].replace("\n", " ")
            if len(arguments) > 240:
                arguments = arguments[:237] + "..."
            print(f"  -> {data['name']} {arguments}")
        elif event == "tool_end":
            try:
                result = json.loads(data["result"])
                marker = "ok" if result.get("ok") else "error"
                detail = result.get("error") or result.get("path") or result.get("exit_code")
                print(f"  <- {marker}" + (f": {detail}" if detail is not None else ""))
            except json.JSONDecodeError:
                print("  <- invalid tool result")

    @staticmethod
    def approve(tool: str, summary: str) -> bool:
        answer = input(f"Approve {tool} {summary}? [y/N] ").strip().lower()
        return answer in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rivet", description="A coding agent built without an agent framework"
    )
    parser.add_argument("task", nargs="*", help="programming task; prompts interactively if omitted")
    parser.add_argument("-w", "--workspace", default=".", help="workspace root (default: current directory)")
    parser.add_argument("-m", "--model", help="model name; or set RIVET_MODEL")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--max-steps", type=int, help="maximum model turns")
    parser.add_argument(
        "--approval",
        choices=("safe", "ask", "never"),
        default="safe",
        help="safe=auto safe operations, ask=confirm mutations, never=read-only",
    )
    parser.add_argument("--version", action="version", version=f"Rivet {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    task = " ".join(args.task).strip()
    if not task:
        if not sys.stdin.isatty():
            print("error: provide a task as an argument", file=sys.stderr)
            return 2
        print("Rivet coding agent. Describe a task; Ctrl+C to cancel.")
        task = input("task> ").strip()

    try:
        config = Config.from_env(
            args.workspace,
            model=args.model,
            base_url=args.base_url,
            max_steps=args.max_steps,
            approval_mode=args.approval,
        )
        if config.base_url == "https://api.openai.com/v1" and not config.api_key:
            raise RivetError("RIVET_API_KEY or OPENAI_API_KEY is required for api.openai.com")
        console = Console()
        agent = Agent(
            config,
            OpenAICompatibleClient(config),
            event_handler=console.event,
            approver=console.approve,
        )
        result = agent.run(task)
        session_path = save_session(config.workspace, task, result)
        print(f"\n{'Completed' if result.success else 'Stopped'} in {result.steps} step(s).")
        print(result.final)
        print(f"Session: {session_path.relative_to(config.workspace)}")
        return 0 if result.success else 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except RivetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def save_session(workspace: Path, task: str, result: AgentResult) -> Path:
    session_dir = workspace / ".rivet" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = session_dir / f"{timestamp}.json"
    suffix = 1
    while target.exists():
        target = session_dir / f"{timestamp}-{suffix}.json"
        suffix += 1
    payload = {
        "task": task,
        "success": result.success,
        "reason": result.reason,
        "steps": result.steps,
        "final": result.final,
        "messages": list(result.messages),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    raise SystemExit(main())
