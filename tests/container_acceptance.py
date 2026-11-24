"""Opt-in deployment boundary check; no model requests or native login required.

Build the worker image first, then run from this repository:
    python -m tests.container_acceptance --image arke-agent:local

This creates a real container using Runner's production flags. All host fixtures
are temporary files inside the repository. It does not verify the native CLIs'
inner sandboxes or hide credentials deliberately placed in the native home.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from arke.harnesses import Launch
from arke.runner import Runner, validate_isolation

ROOT = Path(__file__).resolve().parents[1]
ENV_SENTINEL = "ARKE_ACCEPTANCE_SUPERVISOR_SECRET"

# This runs inside the production boundary, without importing Arke or a harness.
PROBE = r"""
import json
import os
import pathlib
import sys

settings = json.loads(sys.argv[1])
checks = []

def require(condition, name):
    if not condition:
        raise RuntimeError("isolation check failed: " + name)
    checks.append(name)

def unreadable(path):
    try:
        pathlib.Path(path).read_bytes()
    except OSError:
        return True
    return False

for path in settings["private_paths"]:
    require(unreadable(path), "private state is unreadable: " + path)
require(unreadable("/workspace/escape-owner"), "symlink cannot reach private owner key")
require(unreadable("/workspace/escape-history"), "symlink cannot reach private history")
for path in ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock"):
    require(not pathlib.Path(path).exists(), "host control socket absent: " + path)
require(os.readlink("/proc/self/ns/pid") != settings["pid_namespace"], "separate PID namespace")
require(os.readlink("/proc/self/ns/mnt") != settings["mount_namespace"], "separate mount namespace")
require(settings["environment_name"] not in os.environ, "supervisor environment not inherited")
marker = settings["environment_marker"].encode()
for proc in pathlib.Path("/proc").iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        environment = (proc / "environ").read_bytes()
    except OSError:
        continue
    require(marker not in environment, "supervisor environment not visible through proc " + proc.name)
workspace = pathlib.Path("/workspace")
require((workspace / "input.txt").read_text() == "workspace fixture", "selected workspace is readable")
(workspace / "output.txt").write_text("worker output")
require((workspace / "output.txt").read_text() == "worker output", "selected workspace is writable")
require(pathlib.Path("/home/agent/native-home-fixture").read_text() == "dedicated native home",
        "dedicated native home is intentionally readable")
try:
    pathlib.Path("/app/.arke-isolation-probe").write_text("unexpected write")
except OSError:
    checks.append("image filesystem is read-only")
else:
    raise RuntimeError("isolation check failed: image filesystem accepted a write")
print(json.dumps({"checks": checks}))
"""


def check(image: str, fixtures_dir: Path) -> None:
    fixtures_dir = fixtures_dir.resolve()
    if not fixtures_dir.is_relative_to(ROOT):
        raise ValueError("fixture directory must be inside this repository")
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="container-acceptance-", dir=fixtures_dir
    ) as temporary:
        root = Path(temporary)
        workspace, home, state = (
            root / part for part in ("workspace", "native-home", "private-state")
        )
        for directory in (workspace, home, state):
            directory.mkdir(mode=0o700)
        key, history = state / "owner.key", state / "conversations.sqlite3"
        key.write_text("private owner fixture")
        history.write_text("private conversation fixture")
        (workspace / "escape-owner").symlink_to(key)
        (workspace / "escape-history").symlink_to(history)
        (workspace / "input.txt").write_text("workspace fixture")
        (home / "native-home-fixture").write_text("dedicated native home")
        launch = Launch(
            "codex", workspace, runner="container", image=image, native_home=home
        )
        validate_isolation(launch, (state, key, history))
        runner = Runner(launch)
        command = runner.command()
        if command[-2:] != ["-m", "arke.worker"]:
            raise RuntimeError(
                "production worker invocation changed; update the acceptance probe"
            )
        sentinel = secrets.token_urlsafe(32)
        probe_settings = {
            "private_paths": [
                str(key),
                str(history),
                "/state/owner.key",
                "/state/conversations.sqlite3",
            ],
            "pid_namespace": os.readlink("/proc/self/ns/pid"),
            "mount_namespace": os.readlink("/proc/self/ns/mnt"),
            "environment_name": ENV_SENTINEL,
            "environment_marker": sentinel,
        }
        command[-2:] = ["-c", PROBE, json.dumps(probe_settings)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=os.environ | {ENV_SENTINEL: sentinel},
            )
            if result.returncode:
                # The probe never prints process environments or real credential contents.
                raise RuntimeError(
                    "container isolation probe failed:\n" + result.stderr.strip()
                )
            report = json.loads(result.stdout)
            if (workspace / "output.txt").read_text() != "worker output":
                raise RuntimeError(
                    "worker did not write to the selected host workspace"
                )
            print(
                f"Passed {len(report['checks'])} container boundary checks using {image}."
            )
            print(
                "Native-home contents are intentionally readable; native CLI acceptance is separate."
            )
        finally:
            # --rm handles normal exit. Explicit removal also handles a timeout or failed probe.
            if runner.container is not None:
                cleanup = subprocess.run(
                    ["docker", "rm", "-f", runner.container],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if cleanup.returncode and "No such container" not in cleanup.stderr:
                    raise RuntimeError(
                        "could not confirm acceptance container removal: "
                        + cleanup.stderr.strip()
                    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="arke-agent:local")
    parser.add_argument("--fixtures-dir", type=Path, default=ROOT / ".temp")
    args = parser.parse_args()
    check(args.image, args.fixtures_dir)


if __name__ == "__main__":
    main()
