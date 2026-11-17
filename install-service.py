"""Install and start Arke's systemd user service from this clone. Run without sudo."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# before importing settings, which needs tomllib (3.11+); ruff assumes the 3.12 we are checking for
if sys.version_info < (3, 12):  # noqa: UP036
    sys.exit(f"arke needs Python 3.12+; this is {sys.version.split()[0]}")
ROOT: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "arke"))
import settings

THREE: str = "https://cdn.jsdelivr.net/npm/three@0.169.0/"
# name in .local/web -> (pinned URL, sha256 of the file)
ASSET_FILES: dict[str, tuple[str, str]] = {
    "three.module.min.js": (
        THREE + "build/three.module.min.js",
        "f7cee3c7533449a1505cc12cb5128b89e3d4fd3d7ea62b05f9f5464a217472ee",
    ),
    "OrbitControls.js": (
        THREE + "examples/jsm/controls/OrbitControls.js",
        "80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e",
    ),
    "three-LICENSE": (
        THREE + "LICENSE",
        "4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454",
    ),
    # the console's syntax highlighting (BSD-3-Clause, the license is in the file's header)
    "highlight.min.js": (
        "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js",
        "837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846",
    ),
}


def download_assets() -> None:
    """Download the pinned web dependencies once; generated files stay out of Git."""
    directory: Path = ROOT / ".local/web"
    for name, (url, digest) in ASSET_FILES.items():
        target: Path = directory / name
        if (
            target.is_file()
            and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        ):
            continue
        with urllib.request.urlopen(url, timeout=30) as response:
            data: bytes = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"download checksum mismatch: {name}")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=directory, prefix=".download-"
        ) as temporary:
            staged: Path = Path(temporary) / name
            staged.write_bytes(data)
            staged.replace(target)
        print(f"Downloaded {target}", flush=True)


def quote(value: str) -> str:
    """Quote a systemd value, including literal specifier characters."""
    if any(c in value for c in "\r\n\0"):
        raise ValueError("service paths cannot contain line breaks or NUL")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="install files without calling systemctl",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a differing unit after carrying its settings into TOML",
    )
    parser.add_argument(
        "--assets-only",
        action="store_true",
        help="download the Three.js and highlight.js files without installing a service",
    )
    args: argparse.Namespace = parser.parse_args()
    if args.assets_only:
        download_assets()
        return
    config: Path = settings.config_file()
    settings.server_key(
        settings.load().get("server", {})
    )  # validate opt-in before changing a working unit
    service_command: str = " ".join(
        quote(p).replace("$", "$$")
        for p in [sys.executable, str(ROOT / "arke/server.py")]
    )
    environment: str = (
        "Environment=" + quote("ARKE_CONFIG=" + str(config)) + "\n"
        if os.environ.get("ARKE_CONFIG")
        else ""
    )
    unit: str = f"""[Unit]
Description=Arke agent message bus
StartLimitIntervalSec=0

[Service]
{environment}Type=exec
ExecStart={service_command}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    directory: Path = (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd/user"
    )
    target: Path = directory / "arke.service"
    changed: bool = target.exists() and target.read_text() != unit
    if changed and not args.replace:
        raise ValueError(
            f"{target} differs from the generated service; nothing installed or restarted. "
            f"Carry its environment settings and command options (including --tailscale) into {config} "
            "first, then rerun with --replace. Add --no-start to install without restarting."
        )
    download_assets()  # fail before changing the service if a required download is unavailable or invalid
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("x") as output:
            output.write((ROOT / "arke.example.toml").read_text())
    directory.mkdir(parents=True, exist_ok=True)
    if changed:
        shutil.copy2(target, directory / "arke.service.bak")
    # Keep the installed unit until its replacement is fully written; replace a symlink itself, not its target.
    with tempfile.TemporaryDirectory(dir=directory, prefix=".install-") as temporary:
        staged: Path = Path(temporary) / target.name
        with staged.open("w") as output:
            output.write(unit)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(target)
    print(f"Installed {target}\nConfiguration: {config}", flush=True)
    if not args.no_start:
        for command in (
            ["daemon-reload"],
            ["enable", "arke.service"],
            ["restart", "arke.service"],
        ):
            subprocess.run(["systemctl", "--user", *command], check=True)
        print(
            "Check: systemctl --user status arke.service\nLogs: journalctl --user -u arke.service -f"
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        sys.exit(f"arke service install: {e}")
