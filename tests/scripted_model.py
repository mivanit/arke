"""Local Messages/Responses API fixture for testing actual CLI tool and hook execution."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self


class ScriptedModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.actions: list[str] = []
        self.helper_finished = threading.Event()
        self.mail_queued = threading.Event()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append(body)
                if self.path.split("?")[0].endswith("/count_tokens"):
                    raw = json.dumps({"input_tokens": 100}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                try:
                    action = model.next_action(body)
                except (ValueError, AssertionError, KeyError) as error:
                    model.errors.append(str(error))
                    self.send_error(400, str(error))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def event(kind: str, **fields: Any) -> None:
                    data = json.dumps({"type": kind, **fields})
                    self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                    self.wfile.flush()

                if "messages" in body:
                    event(
                        "message_start",
                        message={
                            "id": "msg_fixture",
                            "type": "message",
                            "role": "assistant",
                            "model": body["model"],
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 100, "output_tokens": 0},
                        },
                    )
                    if action is None:
                        block = {"type": "text", "text": ""}
                        delta = {"type": "text_delta", "text": "DONE"}
                    else:
                        block = {
                            "type": "tool_use",
                            "id": action["id"],
                            "name": action["name"],
                            "input": {},
                        }
                        delta = {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(action["arguments"]),
                        }
                    event("content_block_start", index=0, content_block=block)
                    event("content_block_delta", index=0, delta=delta)
                    event("content_block_stop", index=0)
                    event(
                        "message_delta",
                        delta={
                            "stop_reason": "tool_use" if action else "end_turn",
                            "stop_sequence": None,
                        },
                        usage={"output_tokens": 1},
                    )
                    event("message_stop")
                else:
                    response = {
                        "id": "resp_fixture",
                        "object": "response",
                        "model": body["model"],
                        "status": "in_progress",
                        "output": [],
                    }
                    event("response.created", sequence_number=0, response=response)
                    item = {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "DONE", "annotations": []}
                        ],
                    }
                    if action is not None:
                        item = {
                            "id": action["id"],
                            "call_id": action["id"],
                            "name": action["name"],
                            "namespace": action["namespace"],
                            "status": "completed",
                        }
                        if action["name"] == "exec":
                            item |= {
                                "type": "custom_tool_call",
                                "input": action["arguments"],
                            }
                        else:
                            item |= {
                                "type": "function_call",
                                "arguments": json.dumps(action["arguments"]),
                            }
                    event(
                        "response.output_item.added",
                        sequence_number=1,
                        output_index=0,
                        item=item | {"status": "in_progress"},
                    )
                    if action is None:
                        event(
                            "response.output_text.delta",
                            sequence_number=2,
                            output_index=0,
                            content_index=0,
                            item_id=item["id"],
                            delta="DONE",
                        )
                    event(
                        "response.output_item.done",
                        sequence_number=3,
                        output_index=0,
                        item=item,
                    )
                    event(
                        "response.completed",
                        sequence_number=4,
                        response=response
                        | {
                            "status": "completed",
                            "output": [item],
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 1,
                                "total_tokens": 101,
                            },
                        },
                    )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def next_action(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Choose tools from the scenario, using only context the CLI gave the model."""
        claude = "messages" in body
        history = body["messages"] if claude else body["input"]
        texts = []
        for message in history:
            if message.get("role") != "user" and message.get("type") != "agent_message":
                continue
            content = message.get("content", [])
            if isinstance(content, str):
                texts.append(content)
            else:
                texts.extend(
                    part["text"]
                    for part in content
                    if part.get("type") in ("text", "input_text")
                )
                # Codex's local collaboration transport wraps the task this way.
                texts.extend(
                    part["encrypted_content"]
                    for part in content
                    if part.get("type") == "encrypted_content"
                )
        scenarios = re.findall(r"ARKE_FIXTURE (\{[^\n]+\})", "\n".join(texts))
        if not scenarios:
            raise ValueError("model request has no fixture scenario")
        scenario = json.loads(scenarios[-1])
        mode = scenario["mode"]
        # Let the driver queue mail before the first tool runs. The fixture still
        # has to discover its token through actual hook output on a model request.
        if mode == "parent" and not self.mail_queued.wait(timeout=10):
            raise ValueError("driver did not queue incoming mail")
        # Tool results, not merely emitted tool calls, advance the script.
        results = {}
        for message in history:
            if message.get("type") in (
                "function_call_output",
                "custom_tool_call_output",
            ):
                results[message["call_id"]] = message["output"]
            content = message.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "tool_result":
                        if part.get("is_error"):
                            raise ValueError(f"tool failed: {part}")
                        results[part["tool_use_id"]] = part.get("content", "")

        def call(
            stage: str, name: str, arguments: Any, namespace: str = ""
        ) -> dict[str, Any]:
            self.actions.append(f"{mode}: {stage}")
            if len(self.actions) > 40:
                raise ValueError("script exceeded 40 tool calls")
            return {
                "id": f"call_{mode}_{stage}",
                "name": name,
                "arguments": arguments,
                "namespace": namespace,
            }

        def shell(stage: str, command: str) -> dict[str, Any]:
            if claude:
                return call(stage, "Bash", {"command": command})
            return call(
                stage,
                "exec",
                "text(await tools.exec_command(" + json.dumps({"cmd": command}) + "));",
                "functions",
            )

        if f"call_{mode}_send" not in results:
            return shell("send", scenario["command"])
        if mode in ("helper", "resume"):
            if mode == "helper":
                self.helper_finished.set()
            return None
        if mode != "parent":
            raise ValueError(f"unknown fixture mode: {mode}")
        if "call_parent_receipt" not in results:
            # The random token is never passed to this fixture out of band.
            receipts = re.findall(
                r"RECEIPT-TOKEN:\s*([0-9a-f]{32})", json.dumps(history)
            )
            if receipts:
                return shell(
                    "receipt",
                    scenario["command"].rsplit(" ", 1)[0] + " " + receipts[-1],
                )
            raise ValueError("incoming mail was not delivered to the model")
        if "call_parent_spawn" not in results:
            prompt = "Run the shell command, then finish.\nARKE_FIXTURE " + json.dumps(
                {"mode": "helper", "command": scenario["helper_command"]}
            )
            if claude:
                return call(
                    "spawn",
                    "Agent",
                    {
                        "description": "Arke helper routing test",
                        "subagent_type": "general-purpose",
                        "prompt": prompt,
                    },
                )
            return call(
                "spawn",
                "spawn_agent",
                {"task_name": "arke_helper", "message": prompt, "fork_turns": "none"},
                "collaboration",
            )
        if not claude:
            waits = sum(key.startswith("call_parent_wait_") for key in results)
            if not waits or not self.helper_finished.is_set():
                if waits >= 10:
                    raise ValueError("helper did not finish after ten waits")
                return call(
                    f"wait_{waits}",
                    "wait_agent",
                    {"timeout_ms": 10000},
                    "collaboration",
                )
        return None

    def diagnostics(self) -> str:
        def compact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    k: compact(v)
                    for k, v in value.items()
                    if k not in ("tools", "instructions", "system")
                }
            if isinstance(value, list):
                return [compact(v) for v in value]
            return value

        return json.dumps(
            {
                "actions": self.actions,
                "errors": self.errors,
                "last_request": compact(self.requests[-1:]),
            },
            indent=2,
        )
