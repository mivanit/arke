"""Installation publishes complete generations; checking never provisions anything."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

from arke import assets


def test_failed_upgrade_keeps_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def manifest(version: str) -> dict[str, tuple[str, str]]:
        return {
            name: (
                name + version,
                hashlib.sha256((name + version).encode()).hexdigest(),
            )
            for name in ("three.js", "controls.js")
        }

    monkeypatch.setattr(assets, "ASSET_FILES", manifest("old"))
    monkeypatch.setattr(
        assets.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(url.encode())
    )
    original = assets.install(tmp_path)
    monkeypatch.setattr(assets, "ASSET_FILES", manifest("new"))

    def fail_second(url: str, timeout: float) -> io.BytesIO:
        if url.startswith("controls"):
            raise OSError("download interrupted")
        return io.BytesIO(url.encode())

    monkeypatch.setattr(assets.urllib.request, "urlopen", fail_second)
    with pytest.raises(OSError, match="interrupted"):
        assets.install(tmp_path)
    assert not (tmp_path / assets.generation()).exists()
    assert (original / "three.js").read_bytes() == b"three.jsold"
    assert (original / "controls.js").read_bytes() == b"controls.jsold"
    assert not list(tmp_path.glob(".download-*"))


def test_check_missing_directory_has_no_side_effects(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    result = subprocess.run(
        [sys.executable, "-m", "arke.assets", "check", "--directory", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "install --directory" in result.stderr
    assert not target.exists()


def test_legacy_valid_assets_migrate_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"valid existing installation"
    monkeypatch.setattr(
        assets,
        "ASSET_FILES",
        {"library.js": ("unused", hashlib.sha256(content).hexdigest())},
    )
    (tmp_path / "library.js").write_bytes(content)

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("valid files must not be downloaded again")

    monkeypatch.setattr(assets.urllib.request, "urlopen", offline)
    assert not assets.problems(tmp_path)
    installed = assets.install(tmp_path)
    assert installed == assets.directory(tmp_path)
    assert installed != tmp_path
    assert (installed / "library.js").read_bytes() == content
    assert assets.install(tmp_path) == installed


def test_web_asset_directory_is_explicit_and_protected(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arke.webapp import create_app
    from tests.test_conversation_web import config
    from tests.test_sessions import FakeAdapter

    root = tmp_path / "downloads"
    root.mkdir()
    (root / "OrbitControls.js").write_text("// chosen installation")
    settings = config(tmp_path, dashboard=True, assets_dir=root)
    app = create_app(settings, FakeAdapter)
    with TestClient(app) as client:
        assert client.get("/vendor/OrbitControls.js").text == "// chosen installation"
        assert root in app.state.dashboard.protected_paths

    # A shared worker must not be able to alter scripts served to the owner's browser.
    with (
        pytest.raises(ValueError, match="private service state"),
        TestClient(
            create_app(
                config(tmp_path, dashboard=True, assets_dir=tmp_path / "workspace"),
                FakeAdapter,
            )
        ),
    ):
        pass
