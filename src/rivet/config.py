from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Config:
    workspace: Path
    api_key: str | None
    base_url: str
    model: str
    max_steps: int = 30
    request_timeout: int = 120
    command_timeout: int = 120
    max_context_chars: int = 120_000
    max_tool_output_chars: int = 20_000
    approval_mode: str = "safe"

    @classmethod
    def from_env(
        cls,
        workspace: str | Path,
        *,
        model: str | None = None,
        base_url: str | None = None,
        max_steps: int | None = None,
        approval_mode: str = "safe",
    ) -> "Config":
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ConfigurationError(f"workspace does not exist: {root}")

        chosen_model = model or os.getenv("RIVET_MODEL") or os.getenv("OPENAI_MODEL")
        if not chosen_model:
            raise ConfigurationError(
                "model is required; pass --model or set RIVET_MODEL"
            )

        chosen_base = (
            base_url
            or os.getenv("RIVET_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        if not chosen_base.startswith(("http://", "https://")):
            raise ConfigurationError("base URL must begin with http:// or https://")
        if approval_mode not in {"safe", "ask", "never"}:
            raise ConfigurationError("approval mode must be safe, ask, or never")

        chosen_max_steps = (
            max_steps if max_steps is not None else _positive_int("RIVET_MAX_STEPS", 30)
        )
        if chosen_max_steps <= 0:
            raise ConfigurationError("max steps must be greater than zero")

        return cls(
            workspace=root,
            api_key=os.getenv("RIVET_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=chosen_base,
            model=chosen_model,
            max_steps=chosen_max_steps,
            request_timeout=_positive_int("RIVET_REQUEST_TIMEOUT", 120),
            command_timeout=_positive_int("RIVET_COMMAND_TIMEOUT", 120),
            max_context_chars=_positive_int("RIVET_MAX_CONTEXT_CHARS", 120_000),
            max_tool_output_chars=_positive_int("RIVET_MAX_TOOL_OUTPUT_CHARS", 20_000),
            approval_mode=approval_mode,
        )
