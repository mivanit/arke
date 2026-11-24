"""Optional stdio MCP tools for a managed agent's own bus identity."""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from typing import Any


def main() -> None:
    from mcp.server.mcpserver import MCPServer

    socket_path = os.environ.get("ARKE_MANAGED_SOCKET")
    url, key = (
        os.environ.get("ARKE_MANAGED_URL", ""),
        os.environ.get("ARKE_MANAGED_KEY", ""),
    )
    server = MCPServer("arke")

    def call(tool: str, data: dict[str, Any]) -> Any:
        if socket_path:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(30)
                connection.connect(socket_path)
                connection.sendall(
                    (json.dumps({"tool": tool, "data": data}) + "\n").encode()
                )
                with connection.makefile("rb") as stream:
                    reply = json.loads(stream.readline(8 * 1024 * 1024))
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]
        request = urllib.request.Request(
            url + "/" + tool,
            data=json.dumps(data).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    @server.tool()
    def arke_list() -> Any:
        """List other agents on this bus."""
        return call("list", {})

    @server.tool()
    def arke_send(to: list[str], text: str, request_id: str) -> Any:
        """Send a message. Choose a unique request_id and reuse it if retrying this exact send."""
        return call("send", {"to": to, "text": text, "request_id": request_id})

    @server.tool()
    def arke_inbox() -> Any:
        """Read external agent messages. After consuming them, acknowledge the returned delivery IDs."""
        return call("inbox", {})

    @server.tool()
    def arke_ack(ids: list[int]) -> Any:
        """Acknowledge exactly the delivery IDs returned by arke_inbox after reading them."""
        return call("ack", {"ids": ids})

    server.run()


if __name__ == "__main__":
    main()
