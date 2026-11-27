"""Install and inspect optional dashboard assets. No service or authentication side effects."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

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


MAX_DOWNLOAD = 16 * 1024 * 1024


def generation() -> str:
    """A code-selected directory: running older releases keep their matching files."""
    return hashlib.sha256(json.dumps(ASSET_FILES, sort_keys=True).encode()).hexdigest()[
        :20
    ]


def directory(root: Path) -> Path:
    selected = root / generation()
    # Existing installations used a flat directory. Keep serving them until setup.
    return selected if selected.is_dir() else root


def valid(path: Path, digest: str) -> bool:
    try:
        if path.stat().st_size > MAX_DOWNLOAD:
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == digest
    except OSError:
        return False


def problems(root: Path) -> list[str]:
    selected = directory(root)
    return [
        name
        for name, (_, digest) in ASSET_FILES.items()
        if not valid(selected / name, digest)
    ]


def install(root: Path) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Concurrent installers serialize publication, not readers. Old generations stay intact.
    with (root / ".install.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = root / generation()
        if target.is_dir() and not problems(root):
            return target
        previous = directory(root)
        with tempfile.TemporaryDirectory(dir=root, prefix=".download-") as temporary:
            staged = Path(temporary) / "files"
            staged.mkdir()
            for name, (url, digest) in ASSET_FILES.items():
                source = previous / name
                if valid(source, digest):
                    shutil.copyfile(source, staged / name)
                else:
                    with urllib.request.urlopen(url, timeout=30) as response:
                        data = response.read(MAX_DOWNLOAD + 1)
                    if (
                        len(data) > MAX_DOWNLOAD
                        or hashlib.sha256(data).hexdigest() != digest
                    ):
                        raise ValueError(f"download checksum or size mismatch: {name}")
                    (staged / name).write_bytes(data)
                if not valid(staged / name, digest):
                    raise ValueError(f"asset changed during installation: {name}")
            if target.exists():
                # Repair an already damaged generation only after all replacements verify.
                for name in ASSET_FILES:
                    (staged / name).replace(target / name)
            else:
                staged.rename(target)
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check", "path"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.expanduser().resolve()
    try:
        if args.action == "install":
            print(f"Dashboard assets ready: {install(root)}")
        elif args.action == "path":
            print(directory(root))
        else:
            missing = problems(root)
            if missing:
                import shlex

                command = shlex.join(
                    [
                        sys.executable,
                        "-m",
                        "arke.assets",
                        "install",
                        "--directory",
                        str(root),
                    ]
                )
                raise ValueError(
                    f"missing or invalid assets: {', '.join(missing)}\nRun: {command}"
                )
            print(f"Dashboard assets verified: {directory(root)}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"arke assets: {exc}\n")


if __name__ == "__main__":
    main()
