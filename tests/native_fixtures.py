"""Scripted native protocols shared by unit tests and container acceptance."""

CODEX_FIXTURE = r"""
import json, sys
def emit(message):
    print(json.dumps(message), flush=True)
turn = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    rid = message.get("id")
    result = {}
    if method == "initialize":
        result = {"userAgent": "fixture"}
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}
    elif method == "config/read":
        result = {"config": {"model_provider": "openai", "forced_login_method": "chatgpt"}}
    elif method in ("thread/start", "thread/resume"):
        result = {"thread": {"id": "thread-fixture", "turns": []}}
    elif method == "mcpServerStatus/list":
        assert message["params"]["threadId"] == "thread-fixture"
        result = {"data": [{"name": message["params"]["serverName"], "runtimeStatus": "connected", "tools": {}}]}
    elif method == "turn/start":
        turn += 1
        result = {"turn": {"id": str(turn), "status": "inProgress"}}
        emit({"id": rid, "result": result})
        emit({"method": "item/agentMessage/delta", "params": {"threadId": "thread-fixture", "turnId": str(turn), "delta": "Hello"}})
        emit({"id": "approval-" + str(turn), "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread-fixture", "turnId": str(turn), "command": "echo hello"}})
        continue
    elif method == "turn/interrupt":
        # Completion may arrive before the interrupt response. The reader must accept both.
        emit({"method": "turn/completed", "params": {"threadId": "thread-fixture", "turn": {"id": str(turn), "status": "interrupted"}}})
    elif method is None and str(rid).startswith("approval-"):
        assert message["result"]["decision"] in ("accept", "decline")
        emit({"method": "item/completed", "params": {"threadId": "thread-fixture", "turnId": str(turn), "item": {"type": "agentMessage", "text": "Hello"}}})
        emit({"method": "turn/completed", "params": {"threadId": "thread-fixture", "turn": {"id": str(turn), "status": "completed"}}})
        continue
    if rid is not None:
        emit({"id": rid, "result": result})
"""


CLAUDE_FIXTURE = r"""
import json, sys
def emit(message):
    print(json.dumps(message), flush=True)
def result(error=False):
    emit({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
          "is_error": error, "num_turns": 1, "session_id": "claude-fixture", "result": "done"})
if sys.argv[1:3] == ["auth", "status"]:
    emit({"loggedIn": True, "authMethod": "claude.ai"})
    sys.exit(0)
turn = 0
pending = None
for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "control_request":
        subtype = message["request"]["subtype"]
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": message["request_id"], "response": {}}})
        if subtype == "interrupt":
            pending = None
            emit({"type": "control_cancel_request", "request_id": "permission-" + str(turn)})
            result()
    elif message["type"] == "user":
        turn += 1
        pending = "permission-" + str(turn)
        emit({"type": "system", "subtype": "init", "session_id": "claude-fixture", "claude_code_version": "2.1.310"})
        emit({"type": "control_request", "request_id": "permission-" + str(turn),
              "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "echo hello"}, "tool_use_id": "tool-1"}})
    elif message["type"] == "control_response":
        if message["response"]["request_id"] != pending:
            continue  # A cancelled permission callback may reply after interruption.
        pending = None
        emit({"type": "assistant", "message": {"model": "fixture", "content": [{"type": "text", "text": "hello"}]}})
        result()
"""
