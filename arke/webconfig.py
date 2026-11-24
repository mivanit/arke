"""Explicit startup configuration for the reusable web app; no import-time I/O."""

from __future__ import annotations

import os
import secrets
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harnesses import Launch


@dataclass(frozen=True)
class WebConfig:
    state: Path
    profiles: dict[str, Launch]
    owner_key: str
    chat_key: str = ""
    host: str = "127.0.0.1"
    port: int = 7780
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")
    shared_profile: str | None = None
    dashboard: bool = False
    # Each chat credential sees only the single shared conversation. Dashboard access is owner-only.
    title: str = "Agent"
    bus_config: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def private_key(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise ValueError(
            f"{path}: access key must be a private file owned by this user"
        )
    value = path.read_text().strip()
    if len(value) < 32 or len(value) > 128 or not value.isascii():
        raise ValueError(f"{path}: invalid access key")
    return value


def load(path: Path) -> WebConfig:
    path = path.resolve()
    data = tomllib.loads(path.read_text())
    web = data.get("web", {})

    def resolve(value: str) -> Path:
        return (path.parent / Path(value).expanduser()).resolve()

    state = resolve(web.get("state_dir", ".local/conversations"))
    profiles = {}
    for name, values in data.get("profiles", {}).items():
        if values.get("harness") not in ("claude", "codex"):
            raise ValueError(f"{name}: harness must be claude or codex")
        unknown = set(values) - {
            "harness",
            "cwd",
            "model",
            "instructions",
            "mcp_servers",
            "executable",
        }
        if unknown:
            raise ValueError(f"{name}: unknown settings: {', '.join(sorted(unknown))}")
        profiles[name] = Launch(**(values | {"cwd": resolve(values["cwd"])}))
    if not profiles:
        raise ValueError("configure at least one [profiles.NAME] launch profile")
    shared = web.get("shared_profile")
    if shared is not None and shared not in profiles:
        raise ValueError("shared_profile must name a configured profile")
    owner = private_key(
        resolve(web["owner_key_file"])
        if web.get("owner_key_file")
        else state / "owner.key"
    )
    chat = (
        private_key(resolve(web["chat_key_file"])) if web.get("chat_key_file") else ""
    )
    if chat and (not shared or chat == owner):
        raise ValueError("chat access needs a shared_profile and a separate chat key")
    allowed = tuple(web.get("allowed_hosts", ["localhost", "127.0.0.1", "[::1]"]))
    if not allowed or "*" in allowed:
        raise ValueError(
            "list explicit allowed_hosts; wildcard hosts are not supported"
        )
    port = int(web.get("port", 7780))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return WebConfig(
        state=state,
        profiles=profiles,
        owner_key=owner,
        chat_key=chat,
        host=web.get("host", "127.0.0.1"),
        port=port,
        allowed_hosts=allowed,
        shared_profile=shared,
        dashboard=bool(web.get("dashboard", False)),
        title=web.get("title", "Agent"),
        bus_config=resolve(web["bus_config"]) if web.get("bus_config") else None,
    )
