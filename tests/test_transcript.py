"""Randomized check of transcript reading: a fake agent appends to, half-writes, truncates and rewrites its transcript
while a fake console polls with `next`, pages back with `prev` and merges by entry id (dropping everything on a reset).
After every step the console's entries must equal a fresh full read: nothing missing, nothing doubled.

    uv run pytest tests/test_transcript.py

A negative control verifies that the randomized check fails when cursor validation is deliberately broken.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import transcripts


@pytest.fixture(autouse=True)
def transcript_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOTS", (tmp_path,))


def line(i: int, ts: float) -> bytes:
    """A Claude record padded to a random length so chunk and fingerprint boundaries move around: mostly a prompt,
    sometimes an assistant line with text + a tool call (two entries) or that call's result."""
    t: str = datetime.fromtimestamp(
        ts, UTC
    ).isoformat()  # same clock as the bus messages, so they interleave
    pad: str = "x" * random.randint(0, 300)
    r: float = random.random()
    cx: str = t.replace("+00:00", "Z")
    if r < 0.05:  # Codex: a turn starts
        rec: dict[str, Any] = {
            "timestamp": cx,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": f"t{i}"},
        }
    elif r < 0.07:  # Codex: undo the last turn or two
        rec = {
            "timestamp": cx,
            "type": "event_msg",
            "payload": {
                "type": "thread_rolled_back",
                "num_turns": random.randint(1, 2),
            },
        }
    elif (
        r < 0.12
    ):  # Codex: a reply, then (next line) a call that borrows its last sentence as description
        rec = {
            "timestamp": cx,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"Now step {i}."}],
            },
        }
    elif r < 0.17:
        rec = {
            "timestamp": cx,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": f"x{i}",
                "arguments": json.dumps({"cmd": f"ls {i}"}),
            },
        }
    elif r < 0.3:
        rec = {
            "type": "assistant",
            "timestamp": t,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"msg {i} {pad}"},
                    {
                        "type": "tool_use",
                        "id": f"call{i}",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        }
    elif r < 0.4:
        rec = {
            "type": "user",
            "timestamp": t,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"call{i - 1}",
                        "content": f"out {i} {pad}",
                    }
                ],
            },
        }
    else:
        rec = {
            "type": "user",
            "timestamp": t,
            "message": {"role": "user", "content": f"msg {i} {pad}"},
        }
    return (json.dumps(rec) + "\n").encode()


def merge(have: dict[str, dict[str, Any]], entries: list[dict[str, Any]]) -> None:
    """What the console does: add by id; a rollback drops the transcript rows in the byte range it undid."""
    for e in entries:
        if e["kind"] == "rollback":
            for i in [
                i
                for i in have
                if not i.startswith("bus:")
                and e["undo_from"] <= int(i.split(":")[0]) < e["undo_to"]
            ]:
                del have[i]
        have[e["id"]] = e


def key(e: dict[str, Any]) -> tuple[str, str, str, str]:
    return (e["id"], e["kind"], e.get("text", ""), e.get("desc", ""))


def test_huge_line(tmp_path: Path) -> None:
    """A line bigger than the reader's growing chunk cap must still be paged past (it used to loop forever)."""
    path: Path = tmp_path / "huge.jsonl"

    def prompt(text: str, sec: int) -> bytes:
        return (
            json.dumps(
                {
                    "type": "user",
                    "timestamp": f"2001-09-09T01:46:{sec:02d}Z",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n"
        ).encode()

    path.write_bytes(
        prompt("a", 40)
        + prompt("big " + "x" * (transcripts.TRANSCRIPT_TAIL_BYTES * 3), 41)
        + prompt("c", 42)
    )

    def no_messages(after: float) -> list[dict[str, Any]]:
        return []

    r: dict[str, Any] = transcripts.read_transcript(path, no_messages, "", 1)
    got: int = len(r["entries"])
    for _ in range(10):
        if not r["prev"]:
            break
        r = transcripts.read_transcript(path, no_messages, "", 1, r["prev"])
        got += len(r["entries"])
    assert not r["prev"] and got == 3, (
        f"paging past a huge line: {got} entries, prev={r['prev']}"
    )


def test_cursor_roundtrip(tmp_path: Path) -> None:
    """A cursor carries a page's start time exactly (rounding it moved bus messages between pages)."""
    path: Path = tmp_path / "rt.jsonl"
    path.write_bytes(line(0, 1e9))
    with path.open("rb") as f:
        for x in (1792220000.123456, 0.1 + 0.2, 7.0):
            assert transcripts.check(
                f,
                path.stat().st_size,
                transcripts.mint("p", f, path.stat().st_size, 0, x),
                "p",
            ) == (0, x), x


def test_rewrite_same_head(tmp_path: Path) -> None:
    """A rewrite that keeps the first KB and puts a same-length line at the cursor's spot must still reset."""
    path: Path = tmp_path / "head.jsonl"

    def prompt(text: str) -> bytes:
        return (
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2001-09-09T01:46:40Z",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n"
        ).encode()

    big: bytes = prompt("first " + "y" * 2000)
    path.write_bytes(big + prompt("AAAA"))

    def no_messages(after: float) -> list[dict[str, Any]]:
        return []

    r: dict[str, Any] = transcripts.read_transcript(path, no_messages, "", 5)
    path.write_bytes(big + prompt("ZZZZ") + prompt("new"))
    r2: dict[str, Any] = transcripts.read_transcript(path, no_messages, r["next"], 5)
    assert r2.get("reset"), (
        f"same-head rewrite not detected: {[e.get('text') for e in r2['entries']]}"
    )


def test_context_rewrite_same_head(tmp_path: Path) -> None:
    """The per-file context (Codex rollbacks) must be rebuilt when the file is rewritten keeping its first KB: an old
    rollback used to keep hiding the new file's lines."""
    path: Path = tmp_path / "ctx.jsonl"

    def rec(typ: str, payload: dict[str, Any]) -> bytes:
        return (
            json.dumps(
                {"timestamp": "2001-09-09T01:46:40Z", "type": typ, "payload": payload}
            )
            + "\n"
        ).encode()

    def turn(text: str) -> bytes:
        return rec("event_msg", {"type": "task_started"}) + rec(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        )

    head: bytes = rec("session_meta", {"pad": "h" * 1500})
    path.write_bytes(
        head
        + turn("aaa")
        + turn("bbb")
        + rec("event_msg", {"type": "thread_rolled_back", "num_turns": 1})
    )

    def no_messages(after: float) -> list[dict[str, Any]]:
        return []

    transcripts.read_transcript(path, no_messages, "", 50)
    path.write_bytes(head + turn("aaa") + turn("ccc " + "z" * 300) + turn("ddd"))
    got: list[str] = [
        e.get("text", "")
        for e in transcripts.read_transcript(path, no_messages, "", 50)["entries"]
        if e["kind"] == "prompt"
    ]
    assert got == ["aaa", "ccc " + "z" * 300, "ddd"], (
        f"stale rollback after a same-head rewrite: {got}"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "Fix the tests",
        "<task>Fix the tests</task>",
        '<div class="page">Fix this page</div>',
        "<environment_context_example>Keep this</environment_context_example>",
        "<environment_context>unfinished",
        "<environment_context>context</environment_context>\nMy actual request",
        "<user_shell_command>unfinished",
        "<user_shell_command>not a command</user_shell_command>",
        "<user_shell_command>\n<command>\npwd\n</command>\n<result>unfinished</user_shell_command>",
    ],
)
def test_codex_transcript_preserves_user_markup(tmp_path: Path, prompt: str) -> None:
    path = tmp_path / "codex.jsonl"
    texts = [
        "<environment_context><cwd>/repo</cwd></environment_context>",
        "<user_instructions>Background instructions</user_instructions>",
        "<skills_instructions>Available skills</skills_instructions>",
        "<skill>Loaded skill</skill>",
        "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nBe brief.\n</INSTRUCTIONS>",
        '<codex_internal_context source="daemon_recovery">The server restarted</codex_internal_context>',
        '<hook_prompt hook_run_id="stop:4:/x/hooks.json">Messages via arke</hook_prompt>',
        prompt,
    ]
    path.write_text(
        "".join(
            json.dumps(
                {
                    "timestamp": "2026-11-12T12:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
            + "\n"
            for text in texts
        )
    )
    result = transcripts.read_transcript(path, lambda _: [])
    assert [entry["text"] for entry in result["entries"]] == [prompt]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "  spaced output\n\n",
        "<result>literal </result> and </user_shell_command>\n雪",
        "x" * (transcripts.MAX_TEXT + 1),
    ],
)
def test_codex_user_shell_entries(tmp_path: Path, output: str) -> None:
    path = tmp_path / "codex.jsonl"
    commands = ['printf "<tag> & </command>"\nprintf 雪', "false"]
    results = [
        f"Exit code: {i}\nDuration: 0.0123 seconds\nOutput:\n{output}" for i in range(2)
    ]
    records = [
        {
            "timestamp": "2026-11-17T12:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<skill>Hidden context</skill>"},
                    {
                        "type": "input_text",
                        "text": (
                            f"<user_shell_command>\n<command>\n{command}\n</command>\n"
                            f"<result>\n{result}\n</result>\n</user_shell_command>"
                        ),
                    },
                ],
            },
        }
        for command, result in zip(commands, results, strict=True)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    full = transcripts.read_transcript(path, lambda _: [])
    entries = full["entries"]
    assert [e["kind"] for e in entries] == ["tool", "result", "tool", "result"]
    # A person's command must not borrow the previous assistant reply as its display label.
    transcripts.summarize_tools(entries, "I'll do something unrelated.")
    for i, (command, result) in enumerate(zip(commands, results, strict=True)):
        tool, out = entries[2 * i : 2 * i + 2]
        assert tool["tool"] == out["for"] == "shell"
        assert tool["command"] == command
        assert tool["desc"] == command.split("\n", 1)[0]
        assert out["text"] == result[: transcripts.MAX_TEXT]
        assert out.get("truncated", False) == (len(result) > transcripts.MAX_TEXT)
        assert tool["id"].split(":")[0] == out["id"].split(":")[0]
    # A page boundary must retain both halves of the shell record, with stable entry IDs.
    tail = transcripts.read_transcript(path, lambda _: [], limit=1)
    earlier = transcripts.read_transcript(
        path, lambda _: [], before=tail["prev"], limit=1
    )
    assert earlier["entries"] + tail["entries"] == entries


def test_codex_user_shell_command_without_result() -> None:
    entries = transcripts.codex_user_text(
        1, "<user_shell_command>\n<command>\ngs\n</command>\n</user_shell_command>"
    )
    assert len(entries) == 1
    assert entries[0]["kind"] == "tool"
    assert entries[0]["command"] == "gs"


def test_codex_patch_counts_lines_resembling_diff_headers() -> None:
    patch = (
        "*** Begin Patch\n*** Update File: README.md\n@@\n"
        "----\n-++old\n-ordinary text\n+---\n+++new\n+replacement text\n"
        "*** End Patch\n"
    )
    entries = transcripts.condense_codex(
        {
            "timestamp": "2026-11-12T12:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "patch-1",
                "input": patch,
            },
        }
    )
    assert entries[0]["edits"] == [
        {"path": "README.md", "add": 3, "del": 3, "op": "update"}
    ]


def test_small_cases(tmp_path: Path) -> None:
    """One-line fixes: a typed Codex prompt starting with "<" stays; a whole injected block goes; a nan cursor is
    refused; a Claude tool call whose input isn't an object doesn't crash."""
    assert transcripts.codex_prompt("<div> is broken") == "<div> is broken"
    assert (
        transcripts.codex_prompt(
            "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"
        )
        == ""
    )
    path: Path = tmp_path / "nan.jsonl"
    path.write_bytes(line(0, 1e9))

    def no_messages(after: float) -> list[dict[str, Any]]:
        return []

    r: dict[str, Any] = transcripts.read_transcript(path, no_messages, "", 5)
    for bad in ("nan", "inf", "x"):
        try:
            transcripts.read_transcript(
                path, no_messages, r["next"].rsplit(".", 1)[0] + "." + bad, 5
            )
            raise AssertionError(f"cursor ending in {bad} accepted")
        except transcripts.HttpError as e:
            assert e.status == 400, e.status
    out: list[dict[str, Any]] = transcripts.condense(
        {
            "type": "assistant",
            "timestamp": "2001-09-09T01:46:40Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "X", "input": [1]}],
            },
        }
    )
    assert out and out[0]["kind"] == "tool", out


def test_prompts_queued_while_busy_are_shown() -> None:
    # Claude Code records a prompt typed during a turn only as a queued_command attachment
    def queued(prompt: Any, mode: str = "prompt") -> list[dict[str, Any]]:
        return transcripts.condense(
            {
                "type": "attachment",
                "timestamp": "2001-09-09T01:46:40Z",
                "attachment": {
                    "type": "queued_command",
                    "prompt": prompt,
                    "commandMode": mode,
                },
            }
        )

    assert [(e["kind"], e["text"]) for e in queued("oh and the web stuff?")] == [
        ("prompt", "oh and the web stuff?")
    ]
    assert [e["text"] for e in queued([{"type": "text", "text": "as blocks"}])] == [
        "as blocks"
    ]
    note: list[dict[str, Any]] = queued(
        "<task-notification>\n<summary>done</summary>\n</task-notification>",
        "task-notification",
    )
    assert note and note[0]["kind"] != "prompt", note
    assert (
        transcripts.condense(
            {"type": "attachment", "attachment": {"type": "edited_text_file"}}
        )
        == []
    )


def exercise_transcript(tmp_path: Path, seed: int = 1, steps: int = 400) -> None:
    random.seed(seed)
    path: Path = tmp_path / "t.jsonl"
    path.write_bytes(b"")
    msgs: list[dict[str, Any]] = []

    def bus(after: float) -> list[dict[str, Any]]:
        return [m for m in msgs if m["id"] > after]

    n: int = 0
    ts: float = 1792220000.123  # real epoch times: cursors must carry them exactly
    pending: bytes = b""  # the rest of a half-written line
    have: dict[str, dict[str, Any]] = {}
    nxt: str = ""
    prev: str | None = None
    resets: int = 0
    for step in range(steps):
        op: float = random.random()  # 5% truncate, 5% rewrite, 10% bus message, 80% append (or finish a half line)
        if pending and random.random() < 0.5:
            with path.open("ab") as f:
                f.write(pending)
            pending = b""
        elif op < 0.05:  # truncate and start a new session in the same file
            path.write_bytes(
                b"".join(line(n + k, ts + k) for k in range(random.randint(0, 15)))
            )
            n += 16
            pending = b""
        elif (
            op < 0.1
        ):  # rewrite with the same first line, different rest (same fingerprint while short)
            data: bytes = path.read_bytes()
            first: bytes = data[: data.find(b"\n") + 1]
            path.write_bytes(first + line(n, ts))
            n += 1
            pending = b""
        elif op < 0.2:  # a bus message, sometimes stamped a bit in the past
            msgs.append(
                {
                    "id": len(msgs) + 1,
                    "ts": ts - random.choice([0, 0, 0.5, 5, 30]),
                    "kind": "bus",
                    "from": "x",
                    "text": f"bus {len(msgs) + 1}",
                    "to": ["a"],
                    "outgoing": False,
                    "board": False,
                }
            )
        elif not pending:
            whole: bytes = b"".join(
                line(n + k, ts + k) for k in range(random.randint(1, 4))
            )
            n += 4
            ts += 4
            cutat: int = (
                random.randint(1, len(whole) - 1)
                if random.random() < 0.3
                else len(whole)
            )
            with path.open("ab") as f:
                f.write(whole[:cutat])
            pending = whole[cutat:]
        ts += random.choice([0, 0.001, 0.25, 1])  # equal and sub-second times too
        # the console: poll (or first read), then page back to the start
        lim: int = random.randint(1, 4)
        r: dict[str, Any] = (
            transcripts.read_transcript(path, bus, nxt, lim)
            if nxt
            else transcripts.read_transcript(path, bus, "", lim)
        )
        if r.get("reset") or not nxt:
            have = {}
            resets += bool(r.get("reset"))
        if (
            "prev" in r
        ):  # a page-shaped reply (first read, reset, or the first read after an empty file)
            prev = r["prev"]
        for e in r["entries"]:
            assert e["kind"] != "notice", f"step {step}: unreadable line surfaced: {e}"
        merge(have, r["entries"])
        nxt = r["next"]
        while prev:
            p: dict[str, Any] = transcripts.read_transcript(path, bus, "", lim, prev)
            if p.get("reset"):
                have = {}
                merge(have, p["entries"])
                nxt, prev = p["next"], p.get("prev")
                continue
            merge(have, p["entries"])
            prev = p.get("prev")
        full: dict[str, Any] = transcripts.read_transcript(path, bus, "", 10**6)
        want: set[tuple[str, str, str, str]] = {key(e) for e in full["entries"]}
        got: set[tuple[str, str, str, str]] = {key(e) for e in have.values()}
        assert len(full["entries"]) == len(want), (
            f"step {step}: a full read has duplicate ids"
        )
        assert got == want, (
            f"step {step}: missing {sorted(want - got)[:3]}, extra {sorted(got - want)[:3]}"
        )
    print(
        f"ok: {steps} steps, {n} records written, {len(msgs)} bus messages, {resets} resets"
    )


def test_randomized_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Small reads force line alignment and fingerprint boundaries to be exercised.
    monkeypatch.setattr(transcripts, "TAIL_CHUNK", 512)
    exercise_transcript(tmp_path)


def test_broken_cursor_checks_are_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the same randomized scenario must detect a broken cursor check."""
    original: Callable[..., tuple[int, float] | None] = transcripts.check

    def loose(f: Any, size: int, cursor: str, kind: str) -> tuple[int, float] | None:
        parts: list[str] = cursor.split(".", 5)
        return (
            (int(parts[3]), float(parts[5]))
            if int(parts[3]) <= size
            else original(f, size, cursor, kind)
        )

    monkeypatch.setattr(transcripts, "TAIL_CHUNK", 512)
    monkeypatch.setattr(transcripts, "check", loose)
    with pytest.raises(
        AssertionError, match=r"step \d+: (unreadable line surfaced|missing)"
    ):
        exercise_transcript(tmp_path)


def test_a_page_with_no_timestamps_still_pages_back(tmp_path: Path) -> None:
    """Its prev cursor ends in inf (no time bound); the server refused its own cursor."""
    path: Path = tmp_path / "s.jsonl"
    with path.open("w") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-11-10T00:00:00Z",
                    "message": {"role": "user", "content": "first"},
                }
            )
            + "\n"
        )
        f.write("x" * 70000 + "\n")  # the tail page doesn't reach byte 0
        for i in range(50):
            f.write(
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": f"p{i}"}}
                )
                + "\n"
            )
    out: dict[str, Any] = transcripts.read_transcript(path, lambda since: [], limit=10)
    transcripts.read_transcript(path, lambda since: [], limit=10, before=out["prev"])
