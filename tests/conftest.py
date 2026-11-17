"""Select temporary configuration before importing any application modules."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
        prefix="arke-pytest-"
    )
    config.add_cleanup(temporary.cleanup)
    root: Path = Path(temporary.name)
    settings: Path = root / "config.toml"
    settings.write_text(
        f"[server]\nstate_dir={json.dumps(str(root / 'server'))}\n"
        f"transcript_roots=[{json.dumps(str(root / 'transcripts'))}]\n"
        f"[client]\nstate_dir={json.dumps(str(root / 'client'))}\n"
    )
    patch: pytest.MonkeyPatch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    # run as if from a plain shell: not inside tmux or an agent's session, and no repo above the temp dir
    for name in list(os.environ):
        if name.startswith(("ARKE_", "AGENTBUS_")) or name in (
            "TMUX",
            "TMUX_PANE",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
        ):
            patch.delenv(name)
    patch.setenv("GIT_CEILING_DIRECTORIES", tempfile.gettempdir())
    patch.setenv("ARKE_CONFIG", str(settings))

    import client

    patch.setattr(client, "CONFIG", root / "server-url")


@pytest.fixture
def bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Any]:
    """A new bus whose database, event log and transcript root are private to one test."""
    import server
    import transcripts

    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "log.jsonl")
    monkeypatch.setattr(server, "TRANSCRIPT_ROOTS", (tmp_path,))
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOTS", (tmp_path,))
    monkeypatch.setattr(server, "WAKE_BATCH_S", 0)
    created: server.Bus = server.Bus(tmp_path / "state.sqlite3")
    yield created
    created.store.close()
