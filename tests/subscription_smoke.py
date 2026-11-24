"""Opt-in real subscription check. Never run by pytest or make check.

Requires native subscription login already configured for the selected CLI.
Uses real subscription capacity and writes native conversation history.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from arke.harnesses import Launch, adapter


async def check(kind: str, cwd: Path, model: str | None) -> None:
    launch = Launch(kind, cwd, model=model)
    native = adapter(launch)
    reference = None
    try:
        reference = await native.open(None)
        for prompt in (
            "Reply exactly ARKE_SUBSCRIPTION_OK. Do not use tools.",
            "What exact marker did you just reply with? Do not use tools.",
        ):
            completed = False
            async for event in native.run(prompt):
                if event.kind == "session":
                    reference = event.data["native_id"]
                elif event.kind == "approval":
                    await native.respond(event.data["request_id"], {"allow": False})
                elif event.kind == "completed":
                    assert event.data["status"] == "completed", event.data
                    completed = True
            assert completed
    finally:
        await native.close()
    assert reference
    resumed = adapter(launch)
    try:
        await resumed.open(reference)
        async for event in resumed.run("Reply exactly RESUME_OK. Do not use tools."):
            if event.kind == "approval":
                await resumed.respond(event.data["request_id"], {"allow": False})
            if event.kind == "completed":
                assert event.data["status"] == "completed", event.data
    finally:
        await resumed.close()
    print(f"{kind}: subscription authentication, follow-up, and resume succeeded")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("claude", "codex"))
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--use-subscription", action="store_true", required=True)
    args = parser.parse_args()
    asyncio.run(check(args.harness, args.cwd.resolve(), args.model))


if __name__ == "__main__":
    main()
