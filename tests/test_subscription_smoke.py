"""The opt-in native acceptance check must actually prove conversation continuity."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from arke.harnesses import Event, Launch
from tests import subscription_smoke as smoke


@pytest.mark.parametrize("resume_result", ["correct", "wrong", "missing_terminal"])
def test_smoke_requires_recall_after_fresh_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume_result: str
) -> None:
    instances: list[FakeRunner] = []
    memory: dict[str, str] = {}

    class FakeRunner:
        def __init__(self, launch: Launch) -> None:
            instances.append(self)
            self.prompts: list[str] = []
            self.opened: list[str | None] = []
            self.closed = False

        async def open(self, native_id: str | None) -> str:
            self.opened.append(native_id)
            return native_id or "native-reference"

        async def run(self, text: str) -> AsyncIterator[Event]:
            self.prompts.append(text)
            match = re.search(r"ARKE_[a-f0-9]{32}", text)
            if match:
                memory["marker"] = match.group()
            result = memory["marker"]
            if self is instances[-1] and len(instances) == 2:
                assert not match, "resume prompt must not supply the remembered marker"
                if resume_result == "wrong":
                    result = "A new conversation has no marker"
                elif resume_result == "missing_terminal":
                    yield Event("message", {"text": result})
                    return
            yield Event("session", {"native_id": "native-reference"})
            yield Event("message", {"text": result})
            yield Event("completed", {"status": "completed"})

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(smoke, "Runner", FakeRunner)
    if resume_result == "correct":
        asyncio.run(smoke.check("codex", tmp_path, None))
    else:
        with pytest.raises(AssertionError):
            asyncio.run(smoke.check("codex", tmp_path, None))
    assert len(instances) == 2
    assert instances[0].closed and instances[1].closed
    assert instances[0].opened == [None]
    assert instances[1].opened == ["native-reference"]
    assert memory["marker"] in instances[0].prompts[0]
    assert memory["marker"] not in instances[0].prompts[1]
    assert memory["marker"] not in instances[1].prompts[0]


@pytest.mark.parametrize("blocked_response", [False, True])
def test_smoke_fails_on_unexpected_approval(
    monkeypatch: pytest.MonkeyPatch, blocked_response: bool
) -> None:
    class ApprovalRunner:
        def __init__(self) -> None:
            self.answers: list[dict[str, Any]] = []

        async def open(self, native_id: str | None) -> str | None:
            return native_id

        async def run(self, text: str) -> AsyncIterator[Event]:
            yield Event("approval", {"request_id": "approval"})
            raise AssertionError("the smoke check should not continue after approval")

        async def respond(self, request_id: str, answer: dict[str, Any]) -> None:
            assert request_id == "approval"
            self.answers.append(answer)
            if blocked_response:
                await asyncio.Event().wait()

        async def interrupt(self) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(smoke, "APPROVAL_TIMEOUT", 0.01)
    native = ApprovalRunner()
    with pytest.raises(TimeoutError if blocked_response else AssertionError):
        asyncio.run(smoke.verify_turn(native, "remember", "ARKE_example"))
    assert native.answers == [{"allow": False}]
