# Managed agents and the reusable conversation interface

Arke has five independently usable pieces in this repository:

| Piece | Code | Requires |
|---|---|---|
| Native harness adapters | `arke/harnesses/` | Codex CLI, or Claude CLI and the `claude` extra |
| Agent bus | `arke/server.py`, `client.py`, `storage.py` | Python; existing terminal integration remains supported |
| Single-agent web interface | `arke/sessions.py`, `webapp.py`, `web/chat.js` | The `web` extra and a harness |
| Full dashboard | `arke/dashboard.py`, `web/console.html` | The web interface, bus, and `bus-tools` extra |
| Container deployment | `Dockerfile.agent`, `compose.agent.yml` | Docker/Compose |

Importing `arke`, its adapters, or the conversation manager does not load bus configuration.
The bus remains usable through its existing commands. OpenCode and Antigravity are not implemented.

## Run the standalone app

```bash
uv sync --locked --all-extras
uv run --locked --all-extras arke-web --config agents.example.toml
```

Open `http://127.0.0.1:7780`. On first startup, the app generates a private access key at
`.local/conversations/owner.key`. Read that file locally and enter it in the browser. Keys
are kept in the current tab's session storage, never in URLs. Locking the tab removes its key.

Sign in using the native `claude auth login` or `codex login` flow before submitting a
message. The app does not collect vendor credentials. It launches background CLI processes
over pipes: there is no hidden terminal, tmux session, or terminal emulator. Creating an
agent creates its saved conversation; the native process starts when its first message runs.

Profiles select the harness, existing working directory, instructions, model if desired, and
application-provided MCP tools. Only startup configuration supplies executable paths, launch
arguments, native resume references, and tool definitions. The browser selects a named profile.
Agents have fresh conversations but share files when their profiles use the same directory.
Arke never creates Git worktrees. A saved conversation cannot be resumed under a different
harness or working directory; create a fresh conversation instead.

The adapters require subscription authentication. Billing override environment variables are
rejected, and effective native authentication is checked before a prompt is submitted. Claude
uses the installed CLI with the official Python SDK; Codex uses app-server's stdio protocol.
There is no automatic API fallback. Expired login, incompatible native versions, and rate-limit
failures leave an actionable error and a paused queue. Administrator-enforced provider settings
can still prevent subscription use; the app does not override organizational restrictions.

Technical subscription support is separate from whether a personal plan permits a shared
service for several people. Review the provider's terms for that deployment. Arke does not
provide a token broker or account-sharing mechanism.

## A shared household assistant

Set `shared_profile` to one profile and configure a distinct `chat_key_file`. Everyone using
that chat key sees the same conversation. Chat users can send and redirect messages; only the
owner can create agents, resume paused queues, resolve uncertain outcomes, or approve tools.
The standalone app has no bus, arbitrary file browser, or administrative shell endpoint.

Supply database operations as MCP tools in the profile. Your application should own database
validation and transactions. Agent interruption is not a rollback of already completed writes.
Choose precisely which files and tools the agent can use, particularly in a container.

The default listener is loopback. To expose it on the LAN, change `host` and list the machine's
actual hostname/IP in `allowed_hosts`. Host and same-origin checks apply even with a valid key.
The app does not trust forwarded headers by default. For TLS termination, an embedding server
must configure trusted proxy handling explicitly; do not broadly trust forwarded headers.

## Queue and recovery behavior

* Ordinary messages are committed to SQLite and wait until the current turn ends. The SDK does
  not receive future queued turns in advance.
* Interrupt-and-redirect targets the active request visible to that browser. The redirect runs
  only after the previous native response ends. Older queued messages remain paused until the
  owner resumes or discards them.
* Tool approval and question responses use a separate control path. Stale and duplicate approvals
  cannot authorize another turn. Unsupported native request types fail closed.
* Reloading a browser only observes the existing session. A slow or disconnected browser does
  not own the agent's lifetime.
* After a backend restart, all recovered conversations and pending messages are paused. New
  messages, including bus notifications, do not release them.
* A disconnect after submission can leave the request's outcome uncertain. Arke never blindly
  replays it. Inspect native history on the host, resolve the outcome in the UI, then explicitly
  resume. Resolution marks the old request handled; it does not rerun it.
* Message request IDs are durable and reject reuse with different content. The browser retains
  an uncertain HTTP submission for an explicit retry with the same ID and original message.

One process owns each conversation database, enforced by a filesystem lock. Use one ASGI
worker. Native sessions, authentication, and the SQLite state must all persist if resume is
required. Arke records the conversation activity it observes; it does not import arbitrary
pre-existing native sessions into the browser. History is paginated, and event streams start
with a snapshot so reconnects do not depend on retaining every token event.

## Full dashboard and agent bus

Set `[web] dashboard = true` and optionally `bus_config` to an existing bus configuration file.
Stop the old bus process before starting the combined app; both honor the same bus state lock.
The dashboard uses the existing bus database and request dispatch code, retaining receipt and
terminal wake-up behavior. Its **agents** link opens the reusable conversation interface, and
managed agents in the roster link directly to their conversations.

The combined dashboard requires the owner key for private bus routes, or the bus's configured
experimental shared key. Existing hook clients therefore need a matching `client.key_file` and
the combined server's port. The original standalone bus keeps its existing authentication behavior.

Managed agents receive four stdio MCP tools: `arke_list`, `arke_send`, `arke_inbox`, and `arke_ack`.
Each gets a credential scoped to its own mailbox/tools, not the dashboard owner key. Incoming
mail queues a fixed instruction to check the inbox. The actual message content enters through
tool output, preserving its origin as external data. Reading returns delivery IDs; the agent
acknowledges them explicitly after consuming the tool result. A failed turn does not trigger
an automatic continuation or repeated mailbox wake-up.

Manually started terminal agents still use the existing hooks and tmux support. Managed
agents do not need dummy tmux windows. This release does not rearrange existing tmux sessions.

## Embed the modules

```python
from pathlib import Path
from arke.harnesses import Launch
from arke.webapp import create_app
from arke.webconfig import WebConfig

app = create_app(
    WebConfig(
        state=Path("./state"),
        profiles={"home": Launch("claude", Path("/srv/home"))},
        shared_profile="home",
        owner_key=owner_key_from_your_application,
        chat_key=chat_key_from_your_application,
    )
)
```

The browser component is a standard custom element exported by `/chat/chat.js`:

```javascript
import { ArkeChat } from '/chat/chat.js';
const view = document.createElement('arke-chat');
document.body.append(view);
view.open({ id: conversationId, request: authenticatedFetchReturningJSON, owner: false });
```

The host's `request(path, options)` returns parsed JSON normally and a streaming `Response`
when `options.stream` is true. If mounted beneath a URL prefix, the host adds that prefix in
its request function and serves the component asset at the corresponding URL. The supplied
standalone HTML shell assumes it is mounted at the origin root. For a non-web application,
use `Conversations` directly and call `close()` on shutdown.

## Containers

```bash
docker compose -f compose.agent.yml build
docker compose -f compose.agent.yml run --rm --entrypoint claude agent auth login
docker compose -f compose.agent.yml up
```

For Codex, change the profile's harness in `container.agent.toml` and use the native
`codex login --device-auth` flow in the same container volume. No credentials are built into
the image. The native home and conversation database have separate persistent volumes.
Only the selected workspace is mounted; no Docker socket is exposed. The sample binds the
web port to localhost and runs as an unprivileged user with dropped capabilities. Match the
container UID to the workspace's ownership if your host user is not UID 1000.

Read the initial owner key with `docker compose -f compose.agent.yml exec agent cat /state/owner.key`.
The first container implementation keeps the web backend and native harness together.
For isolation between agents, run separate instances with distinct ports, state, native homes,
and workspace mounts. A single dashboard spawning remote per-agent workers is not implemented.

## Validation

`make check` installs the optional extras from the lockfile and runs formatting, type checks,
Python tests, and browser tests. Native protocol fixtures and fake adapters exercise ordering,
interruption, authorization, receipts, and recovery without model calls or credentials.

An explicit subscription smoke check is separate:

```bash
uv run --locked --all-extras python -m tests.subscription_smoke claude --cwd /path/to/workspace --use-subscription
uv run --locked --all-extras python -m tests.subscription_smoke codex --cwd /path/to/workspace --use-subscription
```

These commands consume real subscription capacity and write native history. They require an
existing native login and verify authentication, follow-up, and resume. Browser approval and
interrupt flows additionally need a real-harness acceptance pass before a production rollout.
The older `test-live` target is an API-billed hook test and does not establish subscription support.

Native contracts used here: [Claude SDK](https://code.claude.com/docs/en/agent-sdk/overview),
[Claude authentication](https://code.claude.com/docs/en/authentication), and
[Codex app-server](https://developers.openai.com/codex/app-server).
