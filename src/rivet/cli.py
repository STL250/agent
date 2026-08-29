from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .agent import Agent, AgentResult
from .client import OpenAICompatibleClient
from .config import Config
from .errors import RivetError
from .session import SessionStore
from .tui import Console, configure_terminal_encoding


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rivet", description="An interactive coding agent built without an agent framework"
    )
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
    configure_terminal_encoding()
    args = build_parser().parse_args(argv)
    console = Console()
    if not sys.stdin.isatty():
        console.error("Interactive mode requires terminal input.")
        return 2

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
        agent = Agent(
            config,
            OpenAICompatibleClient(config),
            event_handler=console.event,
            approver=console.approve,
        )
        return run_interactive(config, agent, console=console)
    except KeyboardInterrupt:
        console.error("Cancelled.")
        return 130
    except RivetError as exc:
        console.error(str(exc))
        return 2


def run_interactive(
    config: Config,
    agent: Agent,
    *,
    console: Console | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Run a persistent terminal conversation until the user exits or sends EOF."""
    console = console or Console()
    sessions = SessionStore(config.workspace, model=config.model)
    session_path: Path | None = None
    console.banner(config)

    while True:
        try:
            user_input = input_fn(console.prompt()).strip()
        except KeyboardInterrupt:
            console.input_cancelled()
            continue
        except EOFError:
            console.goodbye()
            return 0

        if not user_input:
            continue
        if user_input.startswith("/"):
            command, _, argument = user_input.partition(" ")
            command = command.lower()
            argument = argument.strip()
            if command in {"/exit", "/quit"}:
                console.goodbye()
                return 0
            if command == "/help":
                console.help()
                continue
            if command == "/new":
                agent.reset()
                session_path = None
                console.notice("Started a new conversation.")
                continue
            if command == "/sessions":
                console.sessions(sessions.list_sessions())
                continue
            if command == "/resume":
                try:
                    loaded = sessions.load(argument or None)
                    drifted = agent.restore_session_state(loaded.agent_state)
                    session_path = loaded.summary.path
                    console.session_resumed(loaded.summary, drifted)
                    if loaded.summary.model not in {"legacy", config.model}:
                        console.warning(
                            f"Saved model was {loaded.summary.model}; continuing with {config.model}."
                        )
                    if agent.plan_snapshot()["active"]:
                        console.plan(agent.plan_snapshot(), title="Restored plan")
                except RivetError as exc:
                    console.error(str(exc))
                continue
            if command == "/status":
                console.status(agent.status())
                continue
            if command == "/plan":
                if argument:
                    console.warning("/plan does not accept an argument.")
                else:
                    console.plan(agent.plan_snapshot())
                continue
            if command == "/diff":
                try:
                    console.diff(agent.show_diff(argument or None), argument or None)
                except RivetError as exc:
                    console.error(str(exc))
                continue
            console.warning(f"Unknown command: {command}. Type /help for available commands.")
            continue

        result = agent.run(user_input)
        first_save = session_path is None
        try:
            session_path = sessions.save(agent, result, target=session_path)
        except RivetError as exc:
            console.error(f"Session was not saved: {exc}")
        console.turn_result(result, agent.turns)
        if first_save and session_path is not None:
            console.session_saved(session_path.relative_to(config.workspace))


def save_conversation(
    workspace: Path,
    agent: Agent,
    result: AgentResult,
    *,
    target: Path | None = None,
) -> Path:
    """Compatibility wrapper for callers of the earlier transcript exporter."""
    return SessionStore(workspace, model=agent.config.model).save(
        agent, result, target=target
    )


if __name__ == "__main__":
    raise SystemExit(main())
