# Standalone, persistent, browser-interactable agent

An agent with a persistent conversation that you can interact with through a browser,
on its own port (7781). This example supplies SQLite inventory tools to demonstrate
an application; replace the tools and instructions to give the agent another job. It uses
Arke's standalone chat app and Codex subscription login. It needs no agent bus,
dashboard, tmux, dashboard assets, or API keys.

You need Python 3.12+, uv, Make, Docker, and internet access. The web service runs on
the host; Docker runs each agent worker. Run these commands from the repository root:

```bash
make -C examples/standalone setup
make -C examples/standalone image
make -C examples/standalone login
make -C examples/standalone run
```

`setup` installs the host web dependencies and creates `.local/standalone/`. It copies
`agent.toml` only when missing, so rerunning setup preserves your configuration and
data. `image` downloads and installs the worker dependencies using the repository's
pinned versions. `login` opens Codex's device sign-in using a dedicated native home.
It does not mount your normal home or require an API key. Rebuild the images after
updating Arke or this example's tools.

Open `http://127.0.0.1:7781`. Startup creates two private keys:

- `.local/standalone/state/owner.key`: administration, approvals, stop, and resume.
- `.local/standalone/state/chat.key`: access to the agent's shared conversation.

Read the appropriate key locally and enter it in the browser; never put a key in a
URL. Everyone using the chat key sees the same conversation and history. The owner
opens the same conversation with the owner key to answer permission requests.

Try:

> Add two camping lanterns in the garage cabinet. Note that they use AA batteries.
>
> Where are the lanterns?
>
> Move the lanterns to the hallway cupboard. Their quantity is still two.

The agent has four tools: search/list, read one item, add, and update. Writes use
stable request IDs so retrying a lost response does not duplicate a change. Updates
also require the item's current version, so concurrent edits cannot silently
replace each other. A quantity of zero records an item no longer present. There is
no delete operation or arbitrary SQL tool.

## Open it to the local network

Edit `.local/standalone/agent.toml`: set `host = "0.0.0.0"` and add the machine's
actual LAN address or hostname to `allowed_hosts`, preserving localhost entries.
For example, if its address is `192.168.1.42`:

```toml
host = "0.0.0.0"
port = 7781
allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "192.168.1.42"]
```

Restart the service, then open `http://192.168.1.42:7781` from another device. Share
only the chat key. Plain HTTP sends chat and credentials unencrypted; use this on
a trusted LAN, or configure HTTPS as described in [AGENTS-WEB.md](../../AGENTS-WEB.md).
Do not forward the port to the public internet.

Stopping the service leaves work paused. After restart, the owner must click
**Resume queue**. **Stop after this turn** lets the current turn finish;
**Stop now** interrupts it. Neither undoes database writes already committed.
The shared conversation cannot be archived.

## What lives where

| Path under `.local/standalone/` | Contents |
|---|---|
| `agent.toml` | Service and agent configuration |
| `state/` | Access keys, conversation history, queue |
| `workspace/inventory.sqlite3` | Inventory items and write receipts |
| `native/` | Dedicated Codex subscription login and native history |

The worker can access `workspace/` and `native/`. It cannot access `state/`, the
configuration, your normal home, or Docker's socket. The tool code lives in the
read-only image. The worker can still read its own subscription login and directly
modify files in its workspace; tool validation is not a separate security boundary.
Use a dedicated login/home as described in [AGENTS-WEB.md](../../AGENTS-WEB.md).

For a simple consistent backup, stop the service, then copy `.local/standalone/` as
a unit; protect the copy because it includes credentials. Do not copy a live
SQLite database without its write-ahead log. Preserve native history as well as
Arke state if you want to resume the same conversation.

## Reuse it

`Inventory` in [inventory.py](inventory.py) is a normal Python class using only the
standard library. The bottom of the file exposes it as an MCP stdio server using
the MCP SDK already installed in the worker image. It imports no Arke modules.
Replace these domain operations with your own database rules, keep validation and
transactions in the tool implementation, and configure that tool server in the
profile. Arke continues to own conversation state, permissions, streaming, and
worker lifecycle.

The example uses its own fresh inventory schema; it does not migrate or interpret
an existing database. No third-party code is copied into the repository.
The dependency installation happens through `make setup` and the Docker builds.
