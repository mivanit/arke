"""Only an unused, offline sharing binding can be repaired."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arke.harnesses import Event, Launch
from arke.repair_sharing import clear_empty_binding
from arke.sessions import Conversations


@pytest.mark.parametrize("use", ["unused", "request", "history", "native"])
def test_repair_preserves_private_state_and_refuses_used_sharing(
    tmp_path: Path, use: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "conversations.sqlite3"
        profiles = {"home": Launch("codex", tmp_path)}
        manager = Conversations(path, profiles)
        private = manager.create("home", "private")
        manager._record(private, "old", Event("message", {"text": "private fixture"}))
        shared = manager.get_or_create_shared("home")
        if use == "native":
            manager._change(shared, native_id="native-reference")
        elif use == "request":
            manager._change(shared, paused=1)
            manager.submit(shared, "one", "pending")
        elif use == "history":
            manager._record(shared, "old", Event("notice", {"text": "used"}))
        with pytest.raises(RuntimeError, match="stop"):
            clear_empty_binding(path)
        await manager.close()
        if use == "unused":
            assert clear_empty_binding(path)
            assert not clear_empty_binding(path)
        else:
            with pytest.raises(ValueError, match="used"):
                clear_empty_binding(path)
        manager = Conversations(path, profiles)
        try:
            assert manager.history(private)[0]["data"]["text"] == "private fixture"
            if use == "unused":
                assert manager.get_or_create_shared("home") != shared
            else:
                assert manager.get_or_create_shared("home") == shared
        finally:
            await manager.close()

    asyncio.run(scenario())
