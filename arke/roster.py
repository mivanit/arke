"""Shared roster presentation. Observations describe presence; they never control delivery."""

from __future__ import annotations

from typing import Any


def presence(agent: dict[str, Any], now: float, stale_seconds: float) -> str:
    if agent["state"] == "offline":
        return "ended"
    last: float | None = agent.get("last_active")
    return (
        "recent" if last is not None and now - last <= stale_seconds else "unconfirmed"
    )


def describe(agent: dict[str, Any], now: float, stale_seconds: float) -> dict[str, Any]:
    last: float | None = agent.get("last_active")
    return {
        key: agent.get(key, "") for key in ("name", "kind", "host", "state", "about")
    } | {
        "projects": dict(agent["projects"]),
        "last_active": last,
        "last_seen_seconds": max(0, now - last) if last is not None else None,
        "presence": presence(agent, now, stale_seconds),
    }


def age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return "<1m ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def render(data: dict[str, Any], max_chars: int | None = None) -> str:
    """Bound automatic context only; explicit CLI/API listings contain every selected row."""
    projects: str = ", ".join(data["projects"])
    heading: str = {
        "global": "All projects",
        "project": f"Project subtree: {projects}",
    }.get(
        data["scope"], f"Your project peers: {projects or '(no project memberships)'}"
    )
    command: str = {
        "global": "arke who --global",
        "project": f"arke who {projects}",
    }.get(data["scope"], "arke who")
    if max_chars is not None and len(heading) > 600:
        heading = heading[:597] + "..."
    header: str = (
        heading
        + "\nNAME                   PRESENCE      LAST STATE  LAST SEEN   KIND    HOST / PROJECTS / TASK"
    )
    footer: str = (
        f"{data['ended']} ended agents"
        + (" (listed)" if data["include_ended"] else " (hidden)")
        + ". Recently seen does not guarantee availability; busy/idle is the last reported state."
    )
    hints: list[str] = (
        [] if data["include_ended"] else [f"`{command} --all` to include ended agents"]
    ) + (
        []
        if data["scope"] == "global"
        else ["`arke who --global --all` for the full registry"]
    )
    if hints:
        footer += "\nRun " + "; ".join(hints) + "."
    if data["scope"] == "peers" and not data["projects"]:
        footer += "\nUse `arke who /PROJECT` or `arke who --global` to choose a scope."
    rows: list[str] = []
    used: int = len(header) + len(footer) + 100
    for agent in data["agents"]:
        task: str = " ".join(agent["about"].split())
        row: str = (
            f"{agent['name']:<22} {agent['presence']:<13} {agent['state']:<11} "
            f"{age(agent['last_seen_seconds']):<11} {agent['kind']:<7} "
            f"{agent['host']} {' '.join(agent['projects'])} {task}"
        ).rstrip()
        if max_chars is not None and used + len(row) + 1 > max_chars:
            break
        rows.append(row)
        used += len(row) + 1
    omitted: int = len(data["agents"]) - len(rows)
    if omitted:
        rows.append(f"({omitted} more agents; run `{command}` for the complete list)")
    elif not rows:
        rows.append("  (none)")
    return header + "\n" + "\n".join(rows) + "\n" + footer
