# Managed agents and the reusable conversation interface

Arke has five independently usable pieces in this repository:

| Piece | Code | Requires |
|---|---|---|
| Native harness adapters | `arke/harnesses/` | Codex CLI, or Claude CLI and the `claude` extra |
| Agent bus | `arke/server.py`, `client.py`, `storage.py` | Python; existing terminal integration remains supported |
| Single-agent web interface | `arke/sessions.py`, `webapp.py`, `web/chat.js` | The `web` extra and a harness |
| Full dashboard | `arke/dashboard.py`, `web/console.html` | The web interface, bus, and `bus-tools` extra |
| Container deployment | `arke/runner.py`, `Dockerfile.agent`, `compose.agent.yml` | Docker; Compose for the whole-service example |

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
The web app owns a subprocess worker for each running conversation. In the default `local`
mode that worker runs with the owner's machine access; a subprocess is not a security
boundary. Owner-only operation does not require Docker.

Profiles select the harness, existing working directory, instructions, model if desired, and
application-provided MCP tools. Only startup configuration supplies executable paths, launch
arguments, native resume references, and tool definitions. The browser selects a named profile.
Agents have fresh conversations but share files when their profiles use the same directory.
Arke never creates Git worktrees. A saved conversation cannot be resumed under a different
harness or working directory; create a fresh conversation instead. Container profiles also
select a trusted worker image and an existing dedicated `native_home`. Mount only directories
whose entire contents the agent is permitted to access, including any sockets inside them.

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

Use `shared.agent.example.toml` for a separate shared service. It has its own port setting,
state, selected workspace, and native home. Set `shared_profile` to its container profile and
configure a distinct `chat_key_file`. Everyone using that chat key sees one conversation.
Chat users can send and redirect messages; only the owner can create private agents, release
held messages or recovery pauses, resolve uncertain outcomes, and approve tools.

The first startup with sharing enabled creates a fresh conversation and stores its shared
identity. Restart reopens that exact conversation. An existing private conversation is never
selected just because its profile matches. If that profile already has private conversations,
startup refuses sharing; configure a fresh shared profile and dedicated native home. The shared
profile and native home are reserved for the shared conversation; use a separate profile/home
for private conversations.

Shared general-purpose agents require `runner = "container"`. The web/session service stays
on the host and launches an isolated worker. Only the selected workspace and dedicated native
home are mounted into it. Startup rejects mounts that overlap Arke's private state or
configured control files. The worker receives scoped commands and events, not the owner key,
private history, or Docker socket. The same container must not hold both the shared worker
and Arke's administration state. See the setup below.

This boundary protects Arke administration and other conversations. The native CLI needs its
subscription login, so tools inside its worker may read credentials in that worker's native
home. A dedicated home limits what is exposed; it does not hide those credentials from the
worker. Do not mount the owner's whole home or reuse private native session history.

Supply household database operations as MCP tools in the profile; the standalone service
needs no bus. Your application should own database validation and transactions. Configure
paths and endpoints that are available inside the worker. Agent interruption is not a rollback
of writes already performed.

The default listener is loopback. To expose it on the LAN, change `host` and list the machine's
actual hostname/IP in `allowed_hosts`. Host and same-origin checks apply even with a valid key.
The app does not trust forwarded headers by default. For TLS termination, an embedding server
must configure trusted proxy handling explicitly; do not broadly trust forwarded headers.

## Queue and recovery behavior

* Ordinary messages are committed to SQLite and wait until the current turn ends. The SDK does
  not receive future queued turns in advance.
* Interrupt-and-redirect targets the active request visible to that browser. The redirect runs
  only after the previous native response ends. Older queued messages become **held** until the
  owner resumes or discards them. Messages submitted afterward can run behind the redirect;
  they are not held simply because an older message is held. A full ordinary queue does not
  prevent a redirect.
* An interrupt acknowledgement alone does not prove that the turn ended. If completion cannot
  be confirmed within the deadline, Arke stops the worker and pauses for recovery; it does not
  guess the outcome and run the redirect.
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
terminal wake-up behavior. An explicit `bus_config` owns that instance's database, logs,
watcher settings, and transcript roots; another imported bus configuration cannot replace it. Its **agents** link opens the reusable conversation interface, and
managed agents in the roster link directly to their conversations.

The combined dashboard requires the owner key for private bus routes, or the bus's configured
experimental shared key. Existing hook clients therefore need a matching `client.key_file` and
the combined server's port. The original standalone bus keeps its existing authentication behavior.

Managed agents receive four stdio MCP tools: `arke_list`, `arke_send`, `arke_inbox`, and `arke_ack`.
The worker forwards those calls over its existing connection to a capability scoped to its
own mailbox/tools. It does not need a localhost callback URL or the dashboard owner key.
Registration completes before its first tool call. Incoming mail queues a fixed instruction
to check the inbox; a full queue keeps mail pending without blocking other agents or startup. The actual message content enters through
tool output, preserving its origin as external data. Reading returns delivery IDs; the agent
acknowledges them explicitly after consuming the tool result. A failed turn does not trigger
an automatic continuation or repeated mailbox wake-up.

Manually started terminal agents still use the existing hooks and tmux support. Managed
agents do not need dummy tmux windows. This release does not rearrange existing tmux sessions.

## Embed the modules

`create_app(config)` is a standalone ASGI app. When mounting it inside another app, the host
must explicitly enter its service lifetime; mounting alone does not start or stop it:

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from arke.webapp import create_app, service_lifespan
from arke.webconfig import load

child = create_app(load(Path("shared.agent.example.toml")))

@asynccontextmanager
async def lifespan(app):
    async with service_lifespan(child):
        yield

app = FastAPI(lifespan=lifespan)
app.mount("/agent", child)
```

There must be exactly one lifetime owner and one ASGI worker per conversation database.
Configuration and approval rights stay with the host application. When constructing
`WebConfig` directly, include private control-file paths in `protected_paths` so container
mount validation can reject overlaps with them.

The browser component is a standard custom element. For the mounted example:

```javascript
import { ArkeChat } from '/agent/chat/chat.js';
const view = document.createElement('arke-chat');
document.body.append(view);
view.open({
  id: conversationId,
  request: (path, options) => authenticatedFetchReturningJSON('/agent' + path, options),
  owner: false,
});
```

The host's request function supplies authentication and returns parsed JSON normally, or a
streaming `Response` when `options.stream` is true. The supplied standalone HTML shell assumes
an origin-root deployment; the API and component support the prefix supplied by the host.
For a non-web application, use `Conversations` directly and call `close()` on shutdown.

## Containers

For a shared assistant, build the worker image and create its two selected directories:

```bash
docker build -f Dockerfile.agent -t arke-agent:local .
mkdir -p .local/home-workspace .local/home-native
```

Sign in through the native CLI using that dedicated home. For the example's Claude profile:

```bash
docker run --rm -it --user "$(id -u):$(id -g)" \
  --env HOME=/home/agent \
  --env CODEX_HOME=/home/agent/.codex \
  --env CLAUDE_CONFIG_DIR=/home/agent/.claude \
  --mount "type=bind,src=$PWD/.local/home-native,dst=/home/agent" \
  --entrypoint claude arke-agent:local auth login
uv run --locked --all-extras arke-web --config shared.agent.example.toml
```

For Codex, change the example profile's harness to `codex`; use the same login command with
`--entrypoint codex` and arguments `login --device-auth`. No credentials are built into the
image. Read `.local/shared-state/owner.key` locally for administration, and distribute only
the separate chat key to household users.

The host service needs access to a local Docker daemon. It starts workers with a read-only
image, separate process namespace, dropped capabilities, no privilege escalation, resource
limits, and only the selected workspace/native-home mounts. Worker images must be built
locally or otherwise installed first: launch does not pull an image automatically. The worker
has no Docker socket. The selected directories must be writable by the host service's UID/GID.

`compose.agent.yml` remains an owner-only example that contains the whole service and its
native CLI together. This contains the service relative to the host; it does not separate
agent tools from administration inside that container. Use the host-supervisor/container-worker
setup above for shared access. A remote worker scheduler is not implemented.

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
existing native login. The smoke check verifies a fresh random marker in the response, recall
on a follow-up, and recall after closing and resuming through a fresh adapter. Native approval
and interrupt behavior still require an acceptance pass against the supported CLI versions.

After building the image, run the deterministic container boundary check:

```bash
uv run --locked --all-extras python -m tests.container_acceptance --image arke-agent:local
```

It uses the production runner flags without a model request. It checks private-state access,
escaping symlinks, process/environment isolation, control sockets, and intended workspace
access. Host fixtures stay under this repository's `.temp` directory. This does not establish
that each native CLI's own sandbox works inside the container: native command execution,
authentication, approval, and interruption need a separate deployment acceptance pass.
Do not treat fixture tests or a successful image build as completion of those checks.
The older `test-live` target is an API-billed hook test and does not establish subscription support.

Native contracts used here: [Claude SDK](https://code.claude.com/docs/en/agent-sdk/overview),
[Claude authentication](https://code.claude.com/docs/en/authentication), and
[Codex app-server](https://developers.openai.com/codex/app-server).
