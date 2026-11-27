"""Standalone inventory contracts; no native login, model calls, or Docker."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from arke.webapp import create_app
from arke.webconfig import load
from examples.standalone.inventory import Inventory
from tests.test_sessions import FakeAdapter

ROOT = Path(__file__).resolve().parents[1]


def test_inventory_receipts_versions_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite3"
    inventory = Inventory(path)
    first = inventory.add_item("add", "Lantern", "Garage", 2)
    assert inventory.add_item("add", "Lantern", "Garage", 2) == first
    updated = inventory.update_item("move", first["id"], 1, "Lantern", "Hall", 2)
    assert updated["version"] == 2
    reopened = Inventory(path)
    # A late retry returns its original receipt, never overwrites newer state.
    assert reopened.add_item("add", "Lantern", "Garage", 2) == first
    assert reopened.update_item("move", first["id"], 1, "Lantern", "Hall", 2) == updated
    assert reopened.get_item(first["id"]) == updated
    with pytest.raises(ValueError, match="different content"):
        reopened.add_item("add", "Lantern", "Garden", 2)
    with pytest.raises(ValueError, match="item changed"):
        reopened.update_item("stale", first["id"], 1, "Lantern", "Garden", 2)
    # A rejected operation does not consume the request ID.
    gone = reopened.update_item("stale", first["id"], 2, "Lantern", "Garden", 0)
    assert gone["quantity"] == 0 and gone["version"] == 3
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"name": " "},
        {"location": ""},
        {"quantity": -1},
        {"quantity": True},
        {"quantity": 1.5},
        {"quantity": 1_000_001},
        {"name": "x" * 201},
        {"notes": "x" * 2001},
        {"request_id": ""},
    ],
)
def test_invalid_inventory_write_is_atomic(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    inventory = Inventory(tmp_path / "inventory.sqlite3")
    values: dict[str, Any] = {
        "request_id": "add",
        "name": "Lamp",
        "location": "Hall",
        "quantity": 1,
    }
    with pytest.raises(ValueError):
        inventory.add_item(**(values | fields))
    assert inventory.list_items()["items"] == []
    assert inventory.add_item("add", "Lamp", "Hall")["version"] == 1


def test_inventory_paging_and_literal_search(tmp_path: Path) -> None:
    inventory = Inventory(tmp_path / "inventory.sqlite3")
    for i, name in enumerate(["Lamp", "100% cotton", "' OR 1=1 --", "Map"]):
        inventory.add_item(str(i), name, "Hall", notes="blue")
    first = inventory.list_items(limit=2)
    second = inventory.list_items(after_id=first["next_after_id"], limit=2)
    assert [r["id"] for r in first["items"] + second["items"]] == [1, 2, 3, 4]
    assert second["next_after_id"] is None
    assert len(inventory.list_items(query="BLUE")["items"]) == 4
    assert len(inventory.list_items(query="%")["items"]) == 1
    assert len(inventory.list_items(query="' OR 1=1 --")["items"]) == 1
    with pytest.raises(ValueError):
        inventory.list_items(limit=0)


def test_inventory_concurrent_writes(tmp_path: Path) -> None:
    inventory = Inventory(tmp_path / "inventory.sqlite3")
    barrier = Barrier(2)

    def add() -> dict[str, Any]:
        barrier.wait(timeout=5)
        return inventory.add_item("same", "Lamp", "Hall")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(add), pool.submit(add)
        assert a.result() == b.result()
    assert len(inventory.list_items()["items"]) == 1

    def move(location: str) -> str:
        barrier.wait(timeout=5)
        try:
            inventory.update_item(location, 1, 1, "Lamp", location, 1)
            return "saved"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(move, "Garage"), pool.submit(move, "Garden")
        assert sorted([a.result(), b.result()]) == ["conflict", "saved"]
    assert inventory.get_item(1)["version"] == 2


def test_standalone_configuration(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text((ROOT / "examples/standalone/agent.toml").read_text())
    for name in ("workspace", "native"):
        (tmp_path / name).mkdir(mode=0o700)
    loaded = load(config)
    assert loaded.port == 7781 and loaded.host == "127.0.0.1"
    assert loaded.shared_profile == "standalone"
    assert loaded.chat_key and loaded.chat_key != loaded.owner_key
    assert not loaded.dashboard and loaded.bus_config is None
    launch = loaded.profiles["standalone"]
    assert launch.harness == "codex" and launch.runner == "container"
    assert launch.cwd == tmp_path / "workspace"
    assert launch.native_home == tmp_path / "native"
    assert list(launch.mcp_servers) == ["inventory"]
    with TestClient(
        create_app(loaded, FakeAdapter), base_url="http://127.0.0.1"
    ) as client:
        owner = {"Authorization": "Bearer " + loaded.owner_key}
        chat = {"Authorization": "Bearer " + loaded.chat_key}
        shared = client.get("/api/conversations", headers=chat).json()
        assert shared["role"] == "chat" and len(shared["conversations"]) == 1
        cid = shared["shared"]
        assert client.get("/api/conversations", headers=owner).json()["shared"] == cid
        assert (
            client.post(
                f"/api/conversations/{cid}/resume", headers=chat, json={}
            ).status_code
            == 401
        )


def test_standalone_setup_preserves_existing_configuration(tmp_path: Path) -> None:
    # The root installation recipe has its own isolated-install suite. Here,
    # replace only uv to exercise this example's bootstrap without syncing our venv.
    data = tmp_path / "standalone"
    command = [
        "make",
        "-C",
        str(ROOT / "examples/standalone"),
        "setup",
        "UV=true",
        f"DATA={data}",
    ]
    subprocess.run(command, check=True, capture_output=True)
    config = data / "agent.toml"
    assert config.read_text() == (ROOT / "examples/standalone/agent.toml").read_text()
    assert config.stat().st_mode & 0o077 == 0
    for name in ("state", "workspace", "native"):
        assert (data / name).is_dir()
        assert (data / name).stat().st_mode & 0o077 == 0
    config.write_text("# owner configuration\n")
    subprocess.run(command, check=True, capture_output=True)
    assert config.read_text() == "# owner configuration\n"


def test_inventory_mcp_wire(tmp_path: Path) -> None:
    async def scenario() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                str(ROOT / "examples/standalone/inventory.py"),
                "--database",
                str(tmp_path / "inventory.sqlite3"),
            ],
            env=dict(os.environ),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"list_items", "get_item", "add_item", "update_item"}
            read_hint = tools["list_items"].annotations
            add_hint = tools["add_item"].annotations
            update_hint = tools["update_item"].annotations
            assert read_hint is not None and read_hint.read_only_hint is True
            assert add_hint is not None and add_hint.idempotent_hint is True
            assert update_hint is not None and update_hint.destructive_hint is True
            args = {
                "request_id": "wire",
                "name": "Lantern",
                "location": "Garage",
                "quantity": 2,
            }
            first = await client.call_tool("add_item", args)
            retry = await client.call_tool("add_item", args)
            assert not first.is_error and first.content == retry.content
            conflict = await client.call_tool("add_item", args | {"quantity": 3})
            assert conflict.is_error
            listed = await client.call_tool("list_items", {})
            assert not listed.is_error
            content = listed.content[0]
            assert content.type == "text"
            assert len(json.loads(content.text)["items"]) == 1

    asyncio.run(scenario())
