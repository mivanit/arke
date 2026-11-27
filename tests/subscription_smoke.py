"""Opt-in real subscription check. Never run by pytest or make check.

Requires native subscription login already configured for the selected CLI.
Uses real subscription capacity and writes native conversation history.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
from pathlib import Path

from arke.harnesses import Adapter, Launch
from arke.runner import Runner

TURN_TIMEOUT = 180
APPROVAL_TIMEOUT = 5


async def verify_turn(native: Adapter, prompt: str, marker: str) -> str | None:
    """Verify actual recall, rather than treating any successful turn as evidence."""
    reference = None
    messages: list[str] = []
    completed = False
    async with asyncio.timeout(TURN_TIMEOUT):
        async for event in native.run(prompt):
            if event.kind == "session":
                reference = event.data["native_id"]
            elif event.kind == "message":
                messages.append(event.data["text"])
            elif event.kind == "approval":
                # These prompts need no tools. Deny once and fail explicitly;
                # do not leave a smoke check waiting for an unattended approval.
                async with asyncio.timeout(APPROVAL_TIMEOUT):
                    await native.respond(event.data["request_id"], {"allow": False})
                raise AssertionError(
                    "unexpected approval during a tool-free recall check"
                )
            elif event.kind == "completed":
                assert not completed, "multiple terminal results"
                assert event.data["status"] == "completed", event.data
                completed = True
    assert completed, "native stream ended without a completed result"
    actual = "\n".join(messages).strip()
    assert actual == marker, "native response did not recall the exact marker"
    return reference


async def check(
    kind: str,
    cwd: Path,
    model: str | None,
    *,
    container_home: Path | None = None,
    image: str = "arke-agent:local",
) -> None:
    launch = Launch(
        kind,
        cwd,
        model=model,
        runner="container" if container_home is not None else "local",
        native_home=container_home,
        image=image,
    )
    marker = "ARKE_" + secrets.token_hex(16)
    recall = "Reply with only the exact marker from my first message. Do not use tools."
    native = Runner(launch)
    reference = None
    try:
        reference = await native.open(None)
        reference = (
            await verify_turn(
                native,
                f"Remember this marker and reply with it exactly:\n{marker}\nDo not use tools.",
                marker,
            )
            or reference
        )
        reference = await verify_turn(native, recall, marker) or reference
    finally:
        await native.close()
    assert reference, "native conversation reference was not saved"
    resumed = Runner(launch)
    try:
        await resumed.open(reference)
        # This prompt deliberately contains no nonce: a fresh conversation cannot
        # pass merely by echoing its input.
        await verify_turn(resumed, recall, marker)
    finally:
        await resumed.close()
    print(
        f"{kind} ({launch.runner}): subscription authentication and verified follow-up/resume recall succeeded"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("claude", "codex"))
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument(
        "--container-home",
        type=Path,
        help="Run in the production worker container using this dedicated native home with an existing subscription login",
    )
    parser.add_argument(
        "--image", help="Locally installed worker image (requires --container-home)"
    )
    parser.add_argument("--use-subscription", action="store_true", required=True)
    args = parser.parse_args()
    if args.image and args.container_home is None:
        parser.error("--image requires --container-home")
    asyncio.run(
        check(
            args.harness,
            args.cwd.resolve(),
            args.model,
            container_home=args.container_home.resolve()
            if args.container_home
            else None,
            image=args.image or "arke-agent:local",
        )
    )


if __name__ == "__main__":
    main()
