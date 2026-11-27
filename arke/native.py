"""Claude's optional background wake listener. It receives notices, never mailbox receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from . import client
else:
    import client
if __package__:
    from .procs import process_info
else:
    from procs import process_info

LIFETIME_S: float = 24 * 3600
WAIT_S: float = 20
REQUEST_S: float = 27
STARTUP_S: float = 10


def interactive(pid: int) -> bool:
    """Print-mode teardown awaits asyncRewake tasks. Only interactive terminal sessions may listen."""
    info = process_info(pid)
    if not info or not info["tty"]:
        return False
    try:
        # Claude also enters print mode automatically when stdout is redirected, even inside tmux.
        # Inspect the harness's stdout (our hook's own stdout is always captured). Never block on a pipe
        # or acquire a controlling terminal while opening its descriptor.
        with os.fdopen(
            os.open(f"/proc/{pid}/fd/1", os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY),
            "rb",
        ) as stdout:
            if not stdout.isatty():
                return False
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except (OSError, UnicodeError):
        return False
    # `--` can be an option's value (e.g. --system-prompt --), not a terminator.
    # A literal print flag in prompt text may disable native waking; missing print mode hangs shutdown.
    return not any(
        arg == "--print"
        or arg.startswith("--print=")
        or arg.startswith("-")
        and not arg.startswith("--")
        and "p" in arg[1:]
        for arg in argv[1:]
    )


def supported() -> bool:
    """Generate/enable the long-lived hook only for the tested Claude baseline or later."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        version = re.search(r"\b(\d+)\.(\d+)\.(\d+) \(Claude Code\)", result.stdout)
        return bool(
            result.returncode == 0
            and version
            and tuple(map(int, version.groups())) >= (2, 1, 310)
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def listen() -> int:
    """Exit 2 only after an explicit notice. All failures leave delivery to the tmux fallback."""
    try:
        event: Any = json.load(sys.stdin)
        if (
            not isinstance(event, dict)
            or event.get("agent_id")
            or event.get("hook_event_name")
            not in ("SessionStart", "Stop", "StopFailure")
        ):
            return 0
        sid = event.get("session_id")
        if not isinstance(sid, str) or not sid:
            return 0
        found = client.harnesses()
        if not found or found[0][1] != "claude" or len(found) != 1:
            return 0
        pid = found[0][0]
        if not interactive(pid):
            return 0
        token = client.process_token(pid)
        if not token or client.identity()[1]:
            return 0
        origin = client.server()[0]
        key = hashlib.sha256(f"{origin}\n{sid}\n{token}".encode()).hexdigest()
        directory = client.STATE / "wake"
        directory.mkdir(parents=True, exist_ok=True)
        # Do not unlink the lock file: another invocation could then lock a different inode.
        with (directory / key).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            if not supported():
                return 0
            started = time.monotonic()
            listener = uuid.uuid4().hex
            # Claude kills the hook at LIFETIME_S; a poll cut off mid-wait leaves the server a dead waiter
            while time.monotonic() - started < LIFETIME_S - 60:
                if client.process_token(pid) != token or client.server()[0] != origin:
                    return 0
                try:
                    reply = client.call(
                        "POST",
                        "/wake/wait",
                        {
                            "process_token": token,
                            "listener": listener,
                            "wait": WAIT_S,
                        },
                        timeout=REQUEST_S,
                        sid=sid,
                    )
                except client.Down:
                    time.sleep(client.DOWN_BACKOFF_S)
                    continue
                except client.BusError as error:
                    # Parallel SessionStart hooks can reach us before registration replaces the old process.
                    # Retry conflicts only during startup; ownership checks remain on the server.
                    if (
                        str(error).startswith("HTTP 409:")
                        and time.monotonic() - started < STARTUP_S
                    ):
                        time.sleep(0.2)
                        continue
                    return 0  # old server, retired session, helper, or another live listener
                if not isinstance(reply, dict):
                    return 0
                if reply.get("status") == "unregistered":
                    if time.monotonic() - started >= STARTUP_S:
                        return 0
                    time.sleep(0.2)
                    continue
                if reply.get("status") in ("timeout", "cancelled"):
                    continue
                notice = reply.get("notice")
                if (
                    not isinstance(reply.get("attempt"), str)
                    or not isinstance(notice, str)
                    or not notice.startswith("[arke]")
                ):
                    return 0
                if (
                    client.process_token(pid) != token
                    or (client.STATE / "pid" / str(pid)).read_text().strip() != sid
                ):
                    return 0
                print(notice, file=sys.stderr, flush=True)
                return 2
    except (OSError, ValueError, client.BusError):
        return 0
    return 0
