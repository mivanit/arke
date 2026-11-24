"""The small common surface used by the conversation manager.

Adapters own native processes, never queue future user turns. Each run yields exactly
one terminal event or raises (the outcome may then be uncertain). Permission replies
and interrupts are independent of the run iterator.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class SubscriptionRequired(RuntimeError):
    """Native subscription authentication is missing or overridden."""


@dataclass(frozen=True)
class Launch:
    harness: str
    cwd: Path
    model: str | None = None
    instructions: str = ""
    # Only the application owner supplies tools/configuration, never chat requests.
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    executable: str | None = None


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    async def open(self, native_id: str | None) -> str | None: ...
    def run(self, text: str) -> AsyncIterator[Event]: ...
    async def interrupt(self) -> None: ...
    async def respond(self, request_id: str, answer: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


def native_reference(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", value
    ):
        raise ValueError("invalid native conversation reference")
    return value


def subscription_environment(harness: str) -> dict[str, str]:
    """Fail on billing overrides instead of silently changing their meaning."""
    names = (
        (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
        if harness == "claude"
        else ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")
    )
    present = [name for name in names if os.environ.get(name)]
    if present:
        raise SubscriptionRequired(
            "subscription mode conflicts with " + ", ".join(present)
        )
    env = dict(os.environ)
    # Managed conversations don't inherit the launching agent's bus/session identity.
    for name in list(env):
        if name.startswith("ARKE_") or name in (
            "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "TMUX",
            "TMUX_PANE",
        ):
            # Claude's SDK overlays env onto os.environ; an empty value also clears
            # inherited identity there. Codex receives this complete environment.
            env[name] = ""
    return env
