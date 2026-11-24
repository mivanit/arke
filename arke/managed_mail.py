"""Bounded mailbox pages for managed MCP clients; legacy CLI reads stay unchanged."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

if __package__:
    from .wire import MAX_FRAME
else:
    from wire import MAX_FRAME

PAGE_TARGET = 512 * 1024
ENVELOPE_RESERVE = 64 * 1024
NOTICE_RESERVE = 4 * 1024
MAX_OVERSIZED_NOTICES = 8


def encoded_size(value: dict[str, Any]) -> int:
    """Bound both the bus response and the nested MCP text returned as a tool event.

    MCP's Any-return conversion serializes dictionaries to indented UTF-8 JSON.
    The worker then JSON-encodes that text inside its tool-event envelope. Measure
    that extra escaping explicitly and reserve space for native envelope metadata.
    Transport encode_frame still enforces the independent final hard limit.
    """
    response = len(json.dumps({"bus_reply": 0, "result": value}).encode()) + 1
    text = json.dumps(value, ensure_ascii=False, indent=2)
    tool = {
        "event": {
            "kind": "tool",
            "data": {
                "details": {"result": {"content": [{"type": "text", "text": text}]}}
            },
        }
    }
    return max(response, len(json.dumps(tool).encode()) + 1) + ENVELOPE_RESERVE


def mailbox_page(sid: str, messages: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "messages": [],
        "delivery": {"sid": sid, "ids": []},
        "more": False,
        "oversized": [],
        "oversized_count": 0,
    }
    for message in messages:
        selected = result["messages"] + [message]
        candidate: dict[str, Any] = result | {
            "messages": selected,
            "delivery": {"sid": sid, "ids": [item["id"] for item in selected]},
        }
        if encoded_size(candidate) <= PAGE_TARGET:
            result = candidate
            continue
        single: dict[str, Any] = result | {
            "messages": [message],
            "delivery": {"sid": sid, "ids": [message["id"]]},
        }
        if encoded_size(single) > MAX_FRAME - NOTICE_RESERVE:
            result["oversized_count"] += 1
            if len(result["oversized"]) < MAX_OVERSIZED_NOTICES:
                result["oversized"].append(
                    {
                        "id": message["id"],
                        "error": "message exceeds the managed transport limit; inspect it through the owner interface",
                    }
                )
            continue
        if result["messages"]:
            result["more"] = True
            break
        # One message above the ordinary target can be returned alone if it fits
        # both hard wire limits. Continue scanning only to determine `more`.
        result = single
    return result
