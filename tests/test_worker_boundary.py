"""Deterministic policy checks; actual container checks live in container_acceptance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arke.harnesses import Launch
from arke.runner import Runner, validate_isolation
from arke.webapp import create_app
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
