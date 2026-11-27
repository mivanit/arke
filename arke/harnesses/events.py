"""Bounded event delivery that never blocks a native protocol reader.

Preview deltas may be combined; durable records retain their order. A failure
lives outside the buffer so reporting it never removes an accepted record.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .base import Event

PREVIEW_CHARS = 100_000
EVENT_BYTES = 16 * 1024 * 1024
EVENT_ITEMS = 8192
READ_BATCH = 32


class EventOverflow(ConnectionError):
    """Durable output exceeded the bounded delivery allowance."""


def event_value(event: Event) -> Any:
    return asdict(event)


def event_delta(event: Event) -> Event:
    if event.kind == "delta":
        return Event(
            "delta", event.data | {"text": event.data.get("text", "")[-PREVIEW_CHARS:]}
        )
    return event


def merge_events(previous: Event, current: Event) -> Event | None:
    if previous.kind == current.kind == "delta" and (
        {k: v for k, v in previous.data.items() if k != "text"}
        == {k: v for k, v in current.data.items() if k != "text"}
    ):
        return Event(
            "delta",
            current.data
            | {
                "text": (previous.data.get("text", "") + current.data.get("text", ""))[
                    -PREVIEW_CHARS:
                ]
            },
        )
    return None


def native_delta(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("method") == "item/agentMessage/delta":
        return message | {
            "params": message.get("params", {})
            | {"delta": message.get("params", {}).get("delta", "")[-PREVIEW_CHARS:]}
        }
    return message


def merge_native(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any] | None:
    if (
        previous.get("method") != "item/agentMessage/delta"
        or current.get("method") != "item/agentMessage/delta"
    ):
        return None
    before, after = previous.get("params", {}), current.get("params", {})
    if {k: v for k, v in before.items() if k != "delta"} != {
        k: v for k, v in after.items() if k != "delta"
    }:
        return None
    return current | {
        "params": after
        | {"delta": (before.get("delta", "") + after.get("delta", ""))[-PREVIEW_CHARS:]}
    }


class EventBuffer[T]:
    def __init__(
        self,
        value: Callable[[T], Any],
        normalize: Callable[[T], T],
        merge: Callable[[T, T], T | None],
        *,
        max_bytes: int = EVENT_BYTES,
        max_items: int = EVENT_ITEMS,
    ) -> None:
        self.value = value
        self.normalize = normalize
        self.merge = merge
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.items: deque[tuple[T, int]] = deque()
        self.bytes = 0
        self.failure: Exception | None = None
        self.ready = asyncio.Event()

    def fail(self, failure: Exception) -> None:
        if self.failure is None:
            self.failure = failure
        self.ready.set()

    def put_nowait(self, item: T | Exception) -> None:
        if isinstance(item, Exception):
            self.fail(item)
            return
        if self.failure is not None:
            return  # The connection is already failed; accepted records stay intact.
        item = self.normalize(item)
        merged = self.merge(self.items[-1][0], item) if self.items else None
        old_size = 0
        if merged is not None:
            item = merged
            old_size = self.items[-1][1]
        size = len(json.dumps(self.value(item)).encode())
        count = len(self.items) + (merged is None)
        if self.bytes - old_size + size > self.max_bytes or count > self.max_items:
            failure = EventOverflow(
                "native durable output exceeded the bounded event buffer"
            )
            self.fail(failure)
            raise failure
        if merged is not None:
            self.items.pop()
        self.items.append((item, size))
        self.bytes += size - old_size
        self.ready.set()

    async def get(self) -> T | Exception:
        while True:
            if self.items:
                item, size = self.items.popleft()
                self.bytes -= size
                return item
            if self.failure is not None:
                return self.failure
            self.ready.clear()
            await self.ready.wait()


def event_buffer(**limits: int) -> EventBuffer[Event]:
    return EventBuffer(event_value, event_delta, merge_events, **limits)


def native_buffer(**limits: int) -> EventBuffer[dict[str, Any]]:
    return EventBuffer(lambda value: value, native_delta, merge_native, **limits)
