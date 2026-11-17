"""Measure retained-history costs against temporary state, without a live server.

From the clone root: uv run --locked python benchmarks/hook_history.py [--profile]
Times cover server methods, including real commits, but not HTTP or client startup.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import math
import os
import pstats
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def measure(call: Callable[[], Any], repeats: int) -> tuple[float, float]:
    call()  # warm caches before timing
    samples: list[float] = []
    for _ in range(repeats):
        start: float = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), sorted(samples)[math.ceil(repeats * 0.95) - 1]


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--messages", type=int, nargs="+", default=[1000, 10000, 100000]
    )
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument(
        "--state-parent",
        type=Path,
        help="place temporary databases on this filesystem (default: system temp directory)",
    )
    args: argparse.Namespace = parser.parse_args()
    if args.repeats < 2 or min(args.messages) < 1:
        parser.error("use positive message counts and at least two repeats")

    with tempfile.TemporaryDirectory(
        prefix="arke-benchmark-", dir=args.state_parent
    ) as temporary:
        root: Path = Path(temporary)
        for name in list(os.environ):
            if name.startswith(("ARKE_", "AGENTBUS_")):
                del os.environ[name]
        config: Path = root / "config.toml"
        config.write_text(
            f"[server]\nstate_dir={json.dumps(str(root))}\n"
            f"transcript_roots=[{json.dumps(str(root / 'transcripts'))}]\n"
            f"[client]\nstate_dir={json.dumps(str(root / 'client'))}\n"
        )
        os.environ["ARKE_CONFIG"] = str(config)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "arke"))
        import server

        print(f"Python {sys.version.split()[0]}; {args.repeats} samples; milliseconds")
        print("messages  operation                 median      p95", flush=True)
        for count in args.messages:
            bus: server.Bus = server.Bus(root / f"{count}.sqlite3")
            try:
                bus.hook(
                    {
                        "session_id": "a",
                        "event": "SessionStart",
                        "host": "remote",
                        "names": ["alice"],
                    }
                )
                bus.send("human", {"to": ["alice"], "text": "pending task"})
                template: dict[str, Any] = bus.messages[0]
                with bus.change():
                    bus.messages.extend(
                        template | {"id": i, "reach": ["elsewhere"], "text": "x" * 200}
                        for i in range(2, count + 1)
                    )
                    bus.next_id = count + 1
                    bus.save()
                agent: server.Agent = bus.agents["a"]

                def hook(bus: server.Bus = bus) -> dict[str, Any]:
                    return bus.hook(
                        {"session_id": "a", "event": "PostToolUse", "receipt": True}
                    )

                def inbox(bus: server.Bus = bus, agent: server.Agent = agent) -> Any:
                    return bus.inbox(agent, 0, receipt=True)

                def hook_and_ack(
                    bus: server.Bus = bus, agent: server.Agent = agent
                ) -> None:
                    agent["inbox"] = [[1, "notify", True]]
                    out = hook(bus)
                    bus.acknowledge("a", "", {"ids": out["delivery"]["ids"]})

                def ack(bus: server.Bus = bus) -> None:
                    bus.acknowledge("a", "", {"ids": [1]})

                for label, call in (
                    ("hook, one old message", hook),
                    ("inbox, one old message", inbox),
                ):
                    median, p95 = measure(call, args.repeats)
                    print(
                        f"{count:8d}  {label:24s} {median:9.3f} {p95:9.3f}", flush=True
                    )
                assert agent["inbox"] == [[1, "notify", True]]
                if args.profile:
                    profiler: cProfile.Profile = cProfile.Profile()
                    profiler.runcall(hook)
                    pstats.Stats(profiler).strip_dirs().sort_stats(
                        "cumulative"
                    ).print_stats(12)
                bus.acknowledge("a", "", {"ids": [1]})
                for label, call in (
                    ("hook, empty inbox", hook),
                    ("ack, empty inbox", ack),
                    ("wake, empty inbox", bus.wake_pass),
                    ("hook and ack", hook_and_ack),
                ):
                    median, p95 = measure(call, args.repeats)
                    print(
                        f"{count:8d}  {label:24s} {median:9.3f} {p95:9.3f}", flush=True
                    )
            finally:
                bus.store.close()


if __name__ == "__main__":
    main()
