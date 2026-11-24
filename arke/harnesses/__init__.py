"""Native harness adapters; no web server or bus required."""

from .base import Adapter, Event, Launch, SubscriptionRequired

__all__ = ["Adapter", "Event", "Launch", "SubscriptionRequired", "adapter"]


def adapter(launch: Launch) -> Adapter:
    if launch.runner != "local":
        raise ValueError("use arke.runner.Runner for container execution")
    if launch.native_home is not None:
        raise ValueError("native_home is supported only for container profiles")
    if launch.harness == "claude":
        from .claude import Claude

        return Claude(launch)
    if launch.harness == "codex":
        from .codex import Codex

        return Codex(launch)
    raise ValueError(f"unsupported harness: {launch.harness}")
