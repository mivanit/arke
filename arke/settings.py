"""Shared TOML configuration. Paths in the file are relative to the clone, not the working directory."""

from __future__ import annotations

import math
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parent.parent
SCHEMA: dict[str, dict[str, type[Any] | tuple[type[Any], ...]]] = {
    "server": {
        "port": int,
        "tailscale": bool,
        "state_dir": str,
        "log_file": str,
        "ntfy_url": str,
        "horatio_url": str,
        "stale_seconds": (int, float),
        "process_interval": (int, float),
        "transcript_roots": list,
        "browser_hosts": list,
        "experimental_require_key": bool,
        "key_file": str,
    },
    "client": {"url": str, "state_dir": str, "key_file": str},
}


def config_file() -> Path:
    return (
        Path(os.environ.get("ARKE_CONFIG") or ROOT / ".local/config.toml")
        .expanduser()
        .resolve()
    )


def path(value: str | Path) -> Path:
    result: Path = Path(value).expanduser()
    return result if result.is_absolute() else ROOT / result


def read_key(value: str) -> str:
    """Read a private, generated key. Never include its contents in an error."""
    filename: Path = path(value)
    with os.fdopen(os.open(filename, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        info: os.stat_result = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                f"{filename}: key must be a private file owned by this user (chmod 600)"
            )
        key: bytes = stream.read(128).strip()
        if info.st_size > 128 or not re.fullmatch(rb"[A-Za-z0-9_-]{43}", key):
            raise ValueError(
                f"{filename}: expected a key generated with secrets.token_urlsafe(32)"
            )
    return key.decode("ascii")


def server_key(values: dict[str, Any]) -> str:
    return (
        read_key(values.get("key_file", ".local/key"))
        if values.get("experimental_require_key", False)
        else ""
    )


def load() -> dict[str, Any]:
    filename: Path = config_file()
    try:
        with filename.open("rb") as stream:
            data: dict[str, Any] = tomllib.load(stream)
    except FileNotFoundError:
        if not os.environ.get("ARKE_CONFIG"):
            return {}
        raise
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{filename}: {e}") from e
    for section, values in data.items():
        if section not in SCHEMA:
            raise ValueError(
                f"{filename}: unknown section {section!r}; use [server] or [client]"
            )
        if not isinstance(values, dict):
            raise ValueError(f"{filename}: {section} must be a [{section}] table")  # noqa: TRY004
        for key, value in values.items():
            expected: type[Any] | tuple[type[Any], ...] | None = SCHEMA[section].get(
                key
            )
            if (
                expected is None
                or not isinstance(value, expected)
                or (isinstance(value, bool) and expected is not bool)
            ):
                raise ValueError(
                    f"{filename}: unknown setting or wrong type for {section}.{key}"
                )
            if key == "port" and not 1 <= value <= 65535:
                raise ValueError(f"{filename}: server.port must be between 1 and 65535")
            if key in ("stale_seconds", "process_interval") and (
                not math.isfinite(value) or value <= 0
            ):
                raise ValueError(
                    f"{filename}: server.{key} must be a finite number greater than zero"
                )
            if (
                key in ("state_dir", "log_file", "url", "key_file")
                and not value.strip()
            ):
                raise ValueError(f"{filename}: {section}.{key} must not be empty")
            if key in ("transcript_roots", "browser_hosts") and any(
                not isinstance(p, str) or not p.strip() for p in value
            ):
                raise ValueError(
                    f"{filename}: server.{key} must be an array of nonempty strings"
                )
    return data
