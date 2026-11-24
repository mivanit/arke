"""Deterministic policy checks; actual container checks live in container_acceptance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arke.harnesses import Launch
from arke.runner import Runner, validate_isolation
from arke.webapp import create_app
from arke.webconfig import load
from tests.test_conversation_web import OWNER, config
from tests.test_sessions import FakeAdapter


def launch(tmp_path: Path) -> Launch:
    work, home = tmp_path / "workspace", tmp_path / "native"
    work.mkdir()
    home.mkdir()
    return Launch("claude", work, runner="container", native_home=home)


def test_mounts_cannot_contain_control_state_or_real_home(tmp_path: Path) -> None:
    profile = launch(tmp_path)
    private = profile.cwd / "private"
    with pytest.raises(ValueError, match="overlaps"):
        validate_isolation(profile, [private])
    alias = tmp_path / "alias"
    alias.symlink_to(profile.cwd)
    with pytest.raises(ValueError, match="overlaps"):
        validate_isolation(replace(profile, cwd=alias), [private])
    with pytest.raises(ValueError, match="dedicated"):
        validate_isolation(replace(profile, native_home=Path.home()), [])
    with pytest.raises(ValueError, match="requires runner"):
        validate_isolation(replace(profile, runner="local"), [])


def test_container_command_only_mounts_selected_directories(tmp_path: Path) -> None:
    profile = launch(tmp_path)
    command = Runner(profile).command()
    mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
    assert mounts == [
        f"type=bind,src={profile.cwd},dst=/workspace",
        f"type=bind,src={profile.native_home},dst=/home/agent",
    ]
    assert "--read-only" in command and "--cap-drop=ALL" in command
    assert not any(
        "docker.sock" in arg or "--privileged" in arg or "network=host" in arg
        for arg in command
    )


def test_profile_cannot_request_external_sandbox(tmp_path: Path) -> None:
    settings = tmp_path / "agent.toml"
    settings.write_text(
        '[profiles.test]\nharness="codex"\ncwd="."\nexternal_sandbox=true\n'
    )
    with pytest.raises(ValueError, match="unknown settings: external_sandbox"):
        load(settings)


def test_shared_profile_cannot_create_private_native_history(tmp_path: Path) -> None:
    with TestClient(create_app(config(tmp_path), FakeAdapter)) as client:
        response = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "code"}
        )
        assert response.status_code == 400
        assert "reserved" in response.text


def test_shared_native_home_cannot_overlap_other_profile(tmp_path: Path) -> None:
    settings = config(tmp_path)
    shared = settings.profiles["code"]
    settings = replace(
        settings,
        profiles={"code": shared, "other": replace(shared, instructions="private")},
    )
    with (
        pytest.raises(ValueError, match="exclusive"),
        TestClient(create_app(settings, FakeAdapter)),
    ):
        pass


def test_enabling_shared_access_refuses_prior_private_native_history(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path, shared_profile=None, chat_key="")
    app = create_app(settings, FakeAdapter)
    with TestClient(app) as client:
        cid = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "code"}
        ).json()["id"]
        assert client.portal is not None
        client.portal.call(
            lambda: app.state.conversations._change(cid, native_id="private-native")
        )
    with (
        pytest.raises(ValueError, match="private native history"),
        TestClient(create_app(config(tmp_path), FakeAdapter)),
    ):
        pass


@pytest.mark.parametrize("native_reference_known", [True, False])
def test_retired_profile_cannot_hide_private_native_home(
    tmp_path: Path, native_reference_known: bool
) -> None:
    settings = config(tmp_path, shared_profile=None, chat_key="")
    settings = replace(settings, profiles={"retired": settings.profiles["code"]})
    app = create_app(settings, FakeAdapter)
    with TestClient(app) as client:
        cid = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "retired"}
        ).json()["id"]

        def mark_used() -> None:
            manager = app.state.conversations
            if native_reference_known:
                manager._change(cid, native_id="private-native")
            else:
                # A prior attempt may have no saved native reference. Conservatively
                # require a fresh home rather than guessing whether native history exists.
                manager._change(cid, paused=1)
                manager.submit(cid, "private", "private queued input")

        assert client.portal is not None
        client.portal.call(mark_used)
    # The retired profile is absent from current config; its recorded home is not.
    with (
        pytest.raises(ValueError, match="private native history"),
        TestClient(create_app(config(tmp_path), FakeAdapter)),
    ):
        pass


@pytest.mark.parametrize("mount", ["cwd", "native_home"])
@pytest.mark.parametrize("relationship", ["equal", "inside", "parent"])
def test_both_shared_mounts_exclude_configured_private_homes(
    tmp_path: Path, mount: str, relationship: str
) -> None:
    import sqlite3

    settings = config(tmp_path)
    shared = settings.profiles["code"]
    target = getattr(shared, mount)
    home = (
        target
        if relationship == "equal"
        else target / "private-home"
        if relationship == "inside"
        else target.parent
    )
    home.mkdir(parents=True, exist_ok=True)
    private_workspace = tmp_path / "private-workspace"
    private_workspace.mkdir()
    settings = replace(
        settings,
        profiles={
            "code": shared,
            "private": Launch(
                "claude", private_workspace, runner="container", native_home=home
            ),
        },
    )
    with (
        pytest.raises(ValueError, match="exclusive"),
        TestClient(create_app(settings, FakeAdapter)),
    ):
        pass
    with sqlite3.connect(settings.state / "conversations.sqlite3") as db:
        assert (
            db.execute(
                "SELECT value FROM metadata WHERE key='shared_conversation'"
            ).fetchone()
            is None
        )


def test_invalid_sharing_can_be_corrected_without_abandoning_private_state(
    tmp_path: Path,
) -> None:
    import sqlite3

    original = config(tmp_path, shared_profile=None, chat_key="")
    private_app = create_app(original, FakeAdapter)
    with TestClient(private_app) as client:
        private = client.post(
            "/api/conversations",
            headers=OWNER,
            json={"profile": "code", "name": "private"},
        ).json()["id"]
        assert client.portal is not None
        client.portal.call(
            lambda: private_app.state.conversations._change(
                private, native_id="private-history"
            )
        )
    with (
        pytest.raises(ValueError, match="private native history"),
        TestClient(create_app(config(tmp_path), FakeAdapter)),
    ):
        pass
    with sqlite3.connect(original.state / "conversations.sqlite3") as db:
        assert (
            db.execute(
                "SELECT value FROM metadata WHERE key='shared_conversation'"
            ).fetchone()
            is None
        )
        assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    home = tmp_path / "fresh-native"
    home.mkdir()
    corrected = replace(
        config(tmp_path),
        shared_profile="fresh",
        profiles={"fresh": replace(original.profiles["code"], native_home=home)},
    )
    with TestClient(create_app(corrected, FakeAdapter)) as client:
        assert (
            client.get("/api/conversations", headers=OWNER).json()["shared"] != private
        )
        assert (
            client.get(f"/api/conversations/{private}", headers=OWNER).json()["name"]
            == "private"
        )


def test_shared_workspace_excludes_effective_local_native_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = config(tmp_path)
    monkeypatch.setenv(
        "CODEX_HOME", str(settings.profiles["code"].cwd / "private-native")
    )
    with (
        pytest.raises(ValueError, match="exclusive"),
        TestClient(create_app(settings, FakeAdapter)),
    ):
        pass


def test_launch_rechecks_protected_mounts(tmp_path: Path) -> None:
    profile = launch(tmp_path)
    profile = replace(profile, protected_paths=(profile.cwd / "owner.key",))
    with pytest.raises(ValueError, match="overlaps"):
        Runner(profile).command()


def test_shared_workspace_excludes_retired_profiles_native_history(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path, shared_profile=None, chat_key="")
    private_home = settings.profiles["code"].cwd / "retired-native"
    private_home.mkdir()
    old = replace(
        settings.profiles["code"],
        native_home=private_home,
        cwd=tmp_path / "old-workspace",
    )
    old.cwd.mkdir()
    settings = replace(settings, profiles={"retired": old})
    app = create_app(settings, FakeAdapter)
    with TestClient(app) as client:
        private = client.post(
            "/api/conversations", headers=OWNER, json={"profile": "retired"}
        ).json()["id"]
        assert client.portal is not None
        client.portal.call(
            lambda: app.state.conversations._change(
                private, native_id="private-history"
            )
        )
    with (
        pytest.raises(ValueError, match="private native history"),
        TestClient(create_app(config(tmp_path), FakeAdapter)),
    ):
        pass
