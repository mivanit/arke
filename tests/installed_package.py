"""Build and exercise a wheel in isolated environments, without native model calls.

Dependency installation uses the project's lockfile and may access package indexes.
The probes use synthetic credentials and never submit an agent turn. All generated
files, homes, caches and environments live under the repository's .temp directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from arke import assets

ROOT = Path(__file__).resolve().parents[1]


def check(uv: str, asset_root: Path) -> None:
    missing = assets.problems(asset_root)
    if missing:
        raise ValueError("run make setup-assets before installed-package checks")
    base = ROOT / ".temp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base, prefix="installed-") as temporary:
        root = Path(temporary)
        home, scratch = root / "home", root / "tmp"
        home.mkdir()
        scratch.mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "TMPDIR": str(scratch),
            "UV_CACHE_DIR": str(ROOT / ".local/uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def run(command: list[str], cwd: Path = root) -> None:
            subprocess.run(
                command,
                cwd=cwd,
                env=env,
                check=True,
                timeout=300,
                stdout=subprocess.DEVNULL if command[1:2] == ["export"] else None,
            )

        run([uv, "build", "--wheel", "--out-dir", str(root / "dist")], ROOT)
        wheel = next((root / "dist").glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert "arke/web/chat.js" in names
            assert not any(Path(name).name in assets.ASSET_FILES for name in names)
            assert not any(
                part in name.split("/")
                for name in names
                for part in (".local", "node_modules", ".venv")
            )

        for label, extras in (
            ("core", []),
            ("web", ["web"]),
            ("claude", ["claude"]),
            ("dashboard", ["web", "bus-tools"]),
        ):
            directory = root / label
            directory.mkdir()
            requirements = directory / "requirements.txt"
            flags = [arg for extra in extras for arg in ("--extra", extra)]
            run(
                [
                    uv,
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-hashes",
                    *flags,
                    "--output-file",
                    str(requirements),
                ],
                ROOT,
            )
            venv = directory / "venv"
            run([uv, "venv", "--python", sys.executable, str(venv)])
            python = str(venv / "bin/python")
            distribution = str(wheel) + ("[" + ",".join(extras) + "]" if extras else "")
            run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    python,
                    "-r",
                    str(requirements),
                    distribution,
                ]
            )
            run([uv, "pip", "check", "--python", python])
            probe = f"""
import importlib.util
from pathlib import Path
import arke, arke.sessions, arke.runner, arke.worker, arke.harnesses.codex
assert Path(arke.__file__).is_relative_to({str(venv)!r}), arke.__file__
assert bool(importlib.util.find_spec('fastapi')) == {("web" in extras)!r}
assert bool(importlib.util.find_spec('claude_agent_sdk')) == {("claude" in extras)!r}
if {"claude" in extras!r}:
    import arke.harnesses.claude
if {"bus-tools" in extras!r}:
    import arke.bus_tools
"""
            run([python, "-B", "-I", "-c", probe])
            if "web" in extras:
                web_probe(python, directory, asset_root, label == "dashboard", env)
            print(f"Installed package: {label} passed", flush=True)


def web_probe(
    python: str, root: Path, asset_root: Path, dashboard: bool, env: dict[str, str]
) -> None:
    workspace = root / "workspace"
    workspace.mkdir()
    configured_assets = root / "downloaded-assets"
    if dashboard:
        # Seed the legacy download cache; installed setup must verify and migrate it
        # without fetching anything. Real downloads happen in make setup-assets.
        configured_assets.mkdir()
        for name in assets.ASSET_FILES:
            shutil.copyfile(
                assets.directory(asset_root) / name, configured_assets / name
            )
        command = [python, "-B", "-I", "-m", "arke.assets"]
        for action in ("install", "install", "check"):
            subprocess.run(
                [*command, action, "--directory", str(configured_assets)],
                cwd=root,
                env=env,
                check=True,
                timeout=30,
            )
    bus = root / "bus.toml"
    bus.write_text(f"[server]\nstate_dir={json.dumps(str(root / 'bus-state'))}\n")
    key = root / "owner.key"
    key.write_text("synthetic-installed-test-key-00000000000000000")
    key.chmod(0o600)
    config = root / "web.toml"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config.write_text(
        f'[web]\nport={port}\nstate_dir="state"\nowner_key_file="owner.key"\n'
        f'dashboard={str(dashboard).lower()}\nbus_config="bus.toml"\nassets_dir="downloaded-assets"\n'
        '[profiles.codex]\nharness="codex"\ncwd="workspace"\n'
    )
    with (root / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [python, "-B", "-I", "-m", "arke.webapp", "--config", str(config)],
            cwd=root,
            env=env | {"ARKE_CONFIG": str(bus)},
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/chat/chat.js", timeout=1
                    ) as response:
                        assert b"class ArkeChat" in response.read()
                    break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(log.read()) from None
                    time.sleep(0.05)
            if dashboard:
                for name in (
                    "three.module.min.js",
                    "OrbitControls.js",
                    "highlight.min.js",
                ):
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/vendor/{name}", timeout=3
                    ) as response:
                        assert (
                            response.read()
                            == (assets.directory(asset_root) / name).read_bytes()
                        )
            else:
                assert not configured_assets.exists(), (
                    "standalone chat provisioned dashboard dependencies"
                )
        finally:
            process.terminate()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        log.seek(0)
        output = log.read()
        # Uvicorn re-raises SIGTERM after graceful shutdown on supported versions.
        assert process.returncode in (0, -signal.SIGTERM), output
        assert "Application shutdown complete" in output, output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--assets-directory", type=Path, default=ROOT / ".local/web")
    args = parser.parse_args()
    check(
        str(Path(shutil.which(args.uv) or args.uv).resolve()),
        args.assets_directory.resolve(),
    )


if __name__ == "__main__":
    main()
