"""Native wake dispatch: a dead waiter is replaced, nothing offered is never a missed pickup, a busy agent costs no try."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import server


def agent(bus: server.Bus, **extra: Any) -> server.Agent:
    bus.hook(
        {
            "session_id": "a",
            "event": "SessionStart",
            "kind": "claude",
            "host": server.LOCAL_HOST,
            "names": ["alice"],
            "process_token": "process:1",
            "tmux": "/sock:%1",
            **extra,
        }
    )
    return bus.agents["a"]


def start(
    bus: server.Bus, listener: str, wait: float
) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(
                bus.wake_wait(
                    "a",
                    {"process_token": "process:1", "listener": listener, "wait": wait},
                )
            )
        except Exception as e:  # noqa: BLE001
            out.append(e)

    t = threading.Thread(target=run)
    t.start()
    with bus.cond:
        assert bus.cond.wait_for(
            lambda: (
                (bus.wake_waiters.get("a") or {}).get("listener") == listener
                or bool(out)
            ),
            2,
        )
    return t, out


def test_same_process_replacement_listener_gets_the_waiter(bus: server.Bus) -> None:
    """A listener SIGKILLed mid-poll leaves its waiter; the re-armed listener (same token) must take over."""
    a = agent(bus)
    dead, _ = start(
        bus, "dead", wait=1.5
    )  # never reads its reply, like a killed process
    new, new_out = start(bus, "new", wait=1.0)
    bus.send("human", {"to": ["alice"], "text": "task"})
    with patch.object(bus, "try_wake") as terminal:
        bus.wake_pass()
    dead.join(3)
    new.join(3)
    terminal.assert_not_called()
    assert (
        isinstance(new_out[0], dict)
        and new_out[0].get("attempt") == a["wake"]["attempt"]
    )


def test_listener_gap_is_not_reported_as_a_missed_pickup(bus: server.Bus) -> None:
    """Claim saw a listener, dispatch didn't (poll expired): nothing was offered, so the next try uses the hook."""
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    t, _ = start(bus, "L", wait=0.3)
    real = bus.native_waiter
    calls = {"n": 0}

    def gone(x: Any) -> Any:
        calls["n"] += 1
        return real(x) if calls["n"] <= 1 else None

    with patch.object(bus, "native_waiter", side_effect=gone):
        bus.wake_pass()
    t.join(2)
    assert a["wake"]["status"] == "held"
    at = a["wake"]["at"]
    t, out = start(bus, "L", wait=0.5)
    with (
        patch.object(server.time, "time", return_value=at + 11),
        patch.object(
            bus,
            "try_wake",
            return_value={"ids": "x", "status": "typed", "at": at + 11, "tries": 1},
        ) as terminal,
    ):
        bus.wake_pass()
    t.join(2)
    print(
        "S2 next transport:",
        a["wake"].get("transport"),
        "tmux called:",
        terminal.called,
        out,
    )
    terminal.assert_not_called()
    assert a["wake"]["transport"] == "claude hook"


def test_listener_gone_logs_no_listener(bus: server.Bus) -> None:
    """Same gap, and the listener doesn't come back: the tmux fallback says "no listener"."""
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    t, _ = start(bus, "L", wait=0.3)
    real = bus.native_waiter
    calls = {"n": 0}

    def gone(x: Any) -> Any:
        calls["n"] += 1
        return real(x) if calls["n"] <= 1 else None

    with patch.object(bus, "native_waiter", side_effect=gone):
        bus.wake_pass()
    t.join(2)
    at = a["wake"]["at"]
    with (
        patch.object(server.time, "time", return_value=at + 11),
        patch.object(
            bus,
            "try_wake",
            return_value={"ids": "x", "status": "typed", "at": at + 11, "tries": 1},
        ) as terminal,
    ):
        bus.wake_pass()
    assert terminal.call_args.args[3]["fallback"] == "no listener"


def test_busy_between_claim_and_dispatch_costs_no_try(bus: server.Bus) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "task"})
    real = bus.wake_current
    calls = {"n": 0}

    def flip(snapshot: Any, mail: Any) -> bool:
        calls["n"] += 1
        if (
            calls["n"] == 1
        ):  # the agent turns busy (transcript, no mail read) right after the claim
            with bus.change(messages_changed=False):
                bus.set_state(bus.agents["a"], "busy")
        return real(snapshot, mail)

    with (
        patch.object(bus, "wake_current", side_effect=flip),
        patch.object(bus, "try_wake") as terminal,
    ):
        bus.wake_pass()
    terminal.assert_not_called()
    print("S3 after early return:", a["wake"]["status"], a["wake"].get("tries"))
    with bus.change(messages_changed=False):
        bus.set_state(a, "idle")
    at = a["wake"]["at"]
    with (
        patch.object(server.time, "time", return_value=at + 11),
        patch.object(
            bus,
            "try_wake",
            return_value={"ids": "x", "status": "typed", "at": at + 11, "tries": 1},
        ) as terminal,
    ):
        bus.wake_pass()
    terminal.assert_called_once()  # 11 s later, not after a 30 s pickup grace for nothing
    assert terminal.call_args.args[3].get("tries", 0) == 0
    assert terminal.call_args.args[3]["fallback"] == "no listener"


@pytest.mark.parametrize("wait", [None, [1], {}])
def test_bad_wait_is_a_400(bus: server.Bus, wait: Any) -> None:
    agent(bus)
    with pytest.raises(server.HttpError) as e:
        bus.wake_wait(
            "a", {"process_token": "process:1", "listener": "L", "wait": wait}
        )
    assert e.value.status == 400


def wake_events() -> list[dict[str, Any]]:
    return [
        e
        for e in map(json.loads, server.LOG_FILE.read_text().splitlines())
        if e["event"] == "wake"
    ]


def test_wake_events_say_who_and_how(bus: server.Bus) -> None:
    """The listener offer and the give-up after WAKE_TRIES both log sender, via and fallback."""
    a = agent(bus, tmux="")
    bus.send("human", {"to": ["alice"], "text": "x"})
    clock = [time.time()]
    with patch.object(server.time, "time", side_effect=lambda: clock[0]):
        for _ in range(server.WAKE_TRIES):
            bus.wake_waiters["a"] = {
                "process_token": "process:1",
                "listener": "L",
                "deadline": time.monotonic() + 1000,
            }
            bus.wake_pass()
            bus.wake_waiters.pop("a", None)  # offered, never taken
            clock[0] += server.WAKE_ACK_S + 1
        bus.wake_waiters["a"] = {
            "process_token": "process:1",
            "listener": "L",
            "deadline": time.monotonic() + 1000,
        }
        bus.wake_pass()
    offer, *_, gave_up = wake_events()
    assert (offer["sender"], offer["via"]) == ("human", "claude hook")
    assert (gave_up["ok"], gave_up["sender"], gave_up["via"]) == (
        False,
        "human",
        "claude hook",
    )
    assert a["wake"]["detail"].startswith("tried 3 times")


def test_listener_expiring_mid_pass_is_not_a_tmux_failure(bus: server.Bus) -> None:
    """One waiter lookup per agent: an agent without tmux never goes down the tmux path."""
    a = agent(bus, tmux="")
    bus.send("human", {"to": ["alice"], "text": "x"})
    base = time.monotonic()
    bus.wake_waiters["a"] = {
        "process_token": "process:1",
        "listener": "L",
        "deadline": base + 1,
    }
    ticks = iter([base])
    with patch.object(
        server.time, "monotonic", side_effect=lambda: next(ticks, base + 2)
    ):
        bus.wake_pass()
    assert a["wake"]["status"] != "failed"


@pytest.mark.parametrize("cancel_before_worker", [False, True])
def test_cancelled_offer_spends_no_try_and_stays_on_the_listener(
    bus: server.Bus,
    cancel_before_worker: bool,
) -> None:
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "x"})
    t, out = start(bus, "L", wait=5)
    real = server.wake_notice

    def racing(
        unread: Any,
    ) -> str:  # a hook lands between the offer's commit and the waiter waking
        a["state_at"] += 1
        return real(unread)

    real_change = bus.change
    caller = threading.current_thread()
    worker_changes = 0

    @contextmanager
    def scheduled_change(**kwargs: Any) -> Generator[None]:
        nonlocal worker_changes
        if threading.current_thread() not in (caller, t):
            worker_changes += 1
            if cancel_before_worker and worker_changes == 2:
                # The offer is published. Let its waiter cancel before the worker's final write.
                # Wait outside change(), so the listener can acquire the bus lock.
                t.join(3)
                assert not t.is_alive()
                assert (a["wake"]["status"], a["wake"]["tries"]) == ("held", 0)
        with real_change(**kwargs):
            yield

    with (
        patch.object(server, "wake_notice", side_effect=racing),
        patch.object(bus, "change", side_effect=scheduled_change),
    ):
        bus.wake_pass()
    t.join(3)
    assert out == [{"status": "cancelled"}]
    assert (a["wake"]["status"], a["wake"]["tries"]) == ("held", 0)
    t, out = start(bus, "L", wait=1)
    with (
        patch.object(server.time, "time", return_value=a["wake"]["at"] + 11),
        patch.object(bus, "try_wake") as terminal,
    ):
        bus.wake_pass()
    t.join(3)
    terminal.assert_not_called()
    assert out[0].get("attempt") == a["wake"]["attempt"]


def test_mail_after_a_listener_wake_waits_for_its_listener_to_rearm(
    bus: server.Bus,
) -> None:
    """The listener exits when it wakes Claude and restarts at Stop; mail arriving then isn't typed into tmux."""
    a = agent(bus)
    bus.send("human", {"to": ["alice"], "text": "first"})
    t, _ = start(bus, "L", wait=2)
    bus.wake_pass()
    t.join(3)
    out = bus.hook(
        {"session_id": "a", "event": "UserPromptSubmit", "wake": True, "receipt": True}
    )
    bus.acknowledge("a", "", {"ids": out["delivery"]["ids"]})
    bus.hook({"session_id": "a", "event": "Stop"})
    bus.send("human", {"to": ["alice"], "text": "second"})
    with (
        patch.object(
            server.time, "time", return_value=a["state_at"] + server.WAKE_BATCH_S + 1
        ),
        patch.object(bus, "try_wake") as terminal,
    ):
        bus.wake_pass()
    terminal.assert_not_called()
    with (
        patch.object(
            server.time,
            "time",
            return_value=a["state_at"] + server.LISTENER_REARM_S + 1,
        ),
        patch.object(
            bus, "try_wake", return_value={"status": "typed", "at": 0, "tries": 1}
        ) as terminal,
    ):
        bus.wake_pass()
    terminal.assert_called_once()  # a listener that never came back: tmux
