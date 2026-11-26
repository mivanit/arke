"""Install and start Arke's systemd user service from this clone. Run without sudo."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# before importing settings, which needs tomllib (3.11+); ruff assumes the 3.12 we are checking for
if sys.version_info < (3, 12):  # noqa: UP036
    sys.exit(f"arke needs Python 3.12+; this is {sys.version.split()[0]}")
ROOT: Path = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "arke"))
import assets
import settings


def download_assets(directory: Path | None = None) -> None:
    """Compatibility entry point; all download and verification logic lives in assets."""
    print(f"Dashboard assets ready: {assets.install(directory or ROOT / '.local/web')}")


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
    values = settings.load().get("server", {})
    settings.server_key(values)  # validate opt-in before changing a working unit
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
    download_assets(settings.path(values.get("assets_dir", ".local/web")))
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
