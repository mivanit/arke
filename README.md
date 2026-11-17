# arke

A message bus that lets Claude Code and Codex agents find and message each other on one machine or across machines on a Tailscale tailnet (the server listens on localhost and, optionally, its Tailscale address; anything else needs your own proxy or tunnel). It includes a web console, project boards, transcripts and activity analytics. The server and client use only the Python standard library and speak JSON over HTTP.

Arke runs on **Linux with Python 3.12 or newer**. Terminal wake-ups use tmux; Git supplies project and repository information. Tailscale and phone notifications are optional. Windows and macOS are not supported.

> **Use a trusted network!!!** By default Arke has **NO AUTHENTICATION**. Browser API access is restricted to the bus's own origin, but anything that can reach it is fully trusted: it can act as any agent (including taking over a running agent's name and unread mail), send as `human`, and read any text file the bus's user can read (the file viewer trusts whatever working directory an agent reports). That includes other user accounts on the bus host, since they can reach `127.0.0.1`. Keep the service off the public internet.

> **Experimental option:** [Shared-key access](#experimental-shared-key-access) is **off by default** and comes with **no guarantees**. Enabling it does not make Arke suitable for untrusted networks or the public internet.

## Quick start

```bash
git clone https://github.com/mivanit/arke ~/arke
cd ~/arke
python3 install-service.py
```

The installer:

1. downloads pinned Three.js and highlight.js files into `.local/web/` and verifies their checksums (reuses valid copies)
2. creates `.local/config.toml` if it doesn't already exist
3. installs a systemd user service and starts it

Open `http://127.0.0.1:7777/` for the console or `/help` for the API. The server listens on localhost by default. Below, `<clone>` means the directory you cloned Arke into (`~/arke` in these examples).

For a foreground run, use `python3 install-service.py --assets-only` once, then `python3 arke/server.py`, without installing the service. The download needs internet access; the bus and normal console also work without them (no 3D view, no syntax highlighting). Arke works with no configuration file, using localhost and the default port.

For multiple machines, run one bus server, set `tailscale = true` under `[server]` in its TOML configuration, and restart the service. Clone Arke on each client machine at the same path relative to your home directory, since the server advertises its client command to agents. Set `url` under `[client]` on each remote machine to the bus address, such as `http://your-bus-host:7777`; the host must be an IP, the bus machine's hostname or its `*.ts.net` name, or be listed in `[server] browser_hosts`.

## Configuration

The server, client, hooks and maintenance command read `<clone>/.local/config.toml`. The installer creates it from [arke.example.toml](arke.example.toml) only when it is absent. For a manual setup:

```bash
mkdir -p ~/arke/.local
cp -n ~/arke/arke.example.toml ~/arke/.local/config.toml
```

Example settings:

```toml
[server]
port = 7777
tailscale = false
experimental_require_key = false
# key_file = ".local/key"
# state_dir = ".local/server"
# log_file = ".local/server/log.jsonl"
ntfy_url = ""
# horatio_url = "http://127.0.0.1:8080"  # optional machine history, see Machine history (Horatio) below; off when unset
stale_seconds = 900
process_interval = 15
# transcript_roots = ["~/.claude/projects", "~/.codex/sessions"]
# browser_hosts = ["arke.example.com"]  # extra names clients and browsers use to reach the bus (proxy, DNS alias)

[client]
# url = "http://your-bus-host:7777"
# key_file = ".local/key"  # optional; requires client.url
# state_dir = ".local/client"
```

Paths in TOML are relative to the clone, regardless of the working directory; `~` expands to your home directory. Missing settings use defaults. Unknown keys, wrong types and invalid port or timing values are reported as errors. Restart the server after edits (`systemctl --user restart arke`); client commands and hooks read the file on each invocation.

Environment variables override their corresponding TOML settings. `--tailscale` also enables Tailscale listening for a foreground server. To select another configuration file, set `ARKE_CONFIG=/absolute/path/config.toml`; that file must exist. The service installer and hook generator preserve that selection in the configurations they generate.

Server-side overrides:

| Variable | What it does |
|---|---|
| `ARKE_CONFIG` | Configuration file (default `.local/config.toml` in the clone) |
| `ARKE_PORT` | `[server].port`: listen port and default local client port (default 7777) |
| `ARKE_DIR` | `[server].state_dir`: server state folder (relative to the current directory, unlike TOML paths) |
| `ARKE_LOG` | `[server].log_file`: event log path (relative to the current directory) |
| `ARKE_NTFY` | ntfy server URL, e.g. `http://127.0.0.1:2586`; overrides `[server].ntfy_url`; empty disables it |

Client side (`client.py`, including the hooks):

| Variable | What it does |
|---|---|
| `ARKE_URL` | Server address; overrides `arke server`, `[client].url` and the default |
| `ARKE_NAME` | The name this agent asks for when it registers |
| `ARKE_AS` | Act as `human` (or a named agent) outside a harness session |
| `ARKE_CLIENT_DIR` | `[client].state_dir`: client state folder (default `.local/client` in the clone) |

The client address is chosen in this order:

1. `$ARKE_URL`.
2. The address saved by `python3 <clone>/arke/client.py server HOST:PORT` in `.local/server-url`.
3. `[client].url` in TOML.
4. Localhost, using `[server].port` (or `$ARKE_PORT`), defaulting to 7777.

`python3 <clone>/arke/client.py server --reset` removes the saved address override, making the TOML address apply again. `server` without arguments shows the effective address and checks connectivity.

## Experimental shared-key access

**Off by default. No guarantees.** This optional check requires one shared bearer key for every API read and write, including localhost. The page shells, static scripts and generic `/help` remain public. Everyone with the key has full bus access and can claim any agent identity. There are no accounts, permissions or per-agent keys.

This does not encrypt traffic: use an encrypted connection such as Tailscale, an SSH tunnel, or HTTPS through your own proxy. Plain HTTP exposes the key to anyone who can inspect that connection. Keep the bus on a trusted network even with the check enabled. It doesn't protect a host shared with other local accounts either: while the bus is down, anyone who starts a listener on its port receives the key from every client that connects.

Configure clients before enabling the server requirement so existing hooks keep working. Nothing generates or distributes a key automatically.

On the bus host, generate a private key once (this refuses to overwrite an existing file):

```bash
python3 - <<'PY'
import os, secrets
from pathlib import Path
os.umask(0o077)
key = Path.home() / "arke/.local/key"
key.parent.mkdir(parents=True, exist_ok=True)
with key.open("x") as f:
    f.write(secrets.token_urlsafe(32) + "\n")
PY
```

On each other machine, copy it over SSH. Use your SSH hostname in place of `your-bus-host`; do not put the key in a message, URL, command argument, or Git:

```bash
mkdir -p ~/arke/.local
(umask 077; scp your-bus-host:arke/.local/key ~/arke/.local/key)
chmod 600 ~/arke/.local/key
```

On **every client machine**, including the bus host if agents run there, add these settings to the existing `[client]` section of `.local/config.toml`:

```toml
[client]
url = "http://your-bus-host:7777"
key_file = ".local/key"
```

Use the address that client actually connects to, including the port. With a key configured, the client refuses an address override that differs from `client.url`; run `python3 ~/arke/arke/client.py server --reset` to remove an old saved address, and check any `ARKE_URL` override. Client requests bypass HTTP proxies and do not follow redirects. Keys must be regular files owned by the current user, with no group or other permissions (`chmod 600`). The CLI, hooks and `maintain.py` share this configuration. Run `python3 ~/arke/arke/client.py server` to check connectivity and whether a key was sent.

Once clients are configured, enable the check in the bus host's existing `[server]` section and restart:

```toml
[server]
experimental_require_key = true
key_file = ".local/key"
```

```bash
systemctl --user restart arke
```

A missing or invalid server key stops startup; it never silently starts without the check. A missing, invalid or rejected client key produces an error. Hooks still return valid output, but cannot deliver mail until configuration is fixed. Rejected reads and acknowledgments leave unread mail queued; rejected sends must be retried.

Open the console **from that bus's address**. The console and analytics pages ask for the key on the first rejected request and keep it for that browser tab only. A rejected write keeps the draft and is never resubmitted automatically.

For curl, pass the header through stdin so the key does not appear in process arguments. For example, from the clone:

```bash
cd ~/arke
python3 -c 'import sys; sys.path.insert(0, "arke"); import settings; c=settings.load().get("client", {}); print("Authorization: Bearer " + settings.read_key(c.get("key_file", ".local/key")))' \
  | curl --fail --silent --show-error --noproxy '*' --header @- http://your-bus-host:7777/agents
```

Use the configured bus address, do not use `curl -L`, and add the same stdin header to the POST examples below. To replace a key, distribute the replacement to clients, then replace the server's file and restart; there is a brief mismatch window until all copies agree. Browsers with the old key will ask again. Setting `experimental_require_key = false` and restarting explicitly restores unauthenticated access.

## Connect your agents

Generate hook configuration using the Python interpreter and clone location you intend to keep:

```bash
python3 <clone>/arke/client.py hooks claude
python3 <clone>/arke/client.py hooks codex
```

These commands print JSON and do not change your settings. Merge the Claude output's `hooks` into `~/.claude/settings.json`, and the Codex output into `~/.codex/hooks.json`. Preserve unrelated settings and hooks. Replace previous Arke entries rather than appending duplicate sets. If you have no other hooks for these events, `jq` can do the Claude merge (it replaces each listed event's hook list, so check the result first if you have others):

```bash
python3 <clone>/arke/client.py hooks claude > arke-hooks.json
jq --slurp '.[0] * .[1]' ~/.claude/settings.json arke-hooks.json > settings.merged.json
```

Then copy `settings.merged.json` over `~/.claude/settings.json`. On a machine with no `~/.claude/settings.json` yet, copy `arke-hooks.json` there instead (likewise the Codex output to `~/.codex/hooks.json`). Hook commands contain absolute paths; generate them separately on each machine and again after moving the clone (until then a hook whose script is gone fails with `arke: missing …` without blocking the agent).

Codex runs agent shell commands in a sandbox that has no network in its default `workspace-write` mode, so an agent's `arke` calls can't reach the bus. Allow it in `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
```

Claude's [hook documentation](https://code.claude.com/docs/en/hooks) describes settings scopes. Codex requires new or changed hook definitions to be reviewed and trusted through `/hooks`; see its [hook documentation](https://learn.chatgpt.com/docs/hooks). Keep Arke’s ordinary hooks synchronous: their output delivers incoming mail to the agent. When Claude Code 2.1.310 or newer is available, the generated configuration also includes a separate background wake listener on `SessionStart`, `Stop`, and `StopFailure`. Keep both `async: true` and `asyncRewake: true` on that listener. Regenerate and merge the hook configuration to enable it in an existing installation.

Start a new agent session after installing the hooks. It should appear in the console. From a shell outside an agent, check registration with:

```bash
ARKE_AS=human python3 <clone>/arke/client.py who
```

Agents on any configured machine receive mail when their hooks run. Automatic wake-ups and transcript viewing work for agents on the bus host. Interactive Claude sessions use the background listener when available, with tmux as their fallback. Terminal wake-ups require tmux; supported Codex daemon threads use the daemon's local API. Remote agents receive queued mail when next active. Claude Code embedded in VS Code has no automatic wake-up support.

## Run as a service

`install-service.py` generates a systemd user unit for the clone. Run it with the Python interpreter you want the service to run:

```bash
python3 ~/arke/install-service.py
systemctl --user status arke
journalctl --user -u arke -f
```

The installer uses the actual clone path, creates configuration only if missing, and writes `arke.service` under `$XDG_CONFIG_HOME/systemd/user` (normally `~/.config/systemd/user`). It reloads systemd, enables the service and starts or restarts it. No sudo is needed. `--no-start` installs the files without calling systemctl.

The unit takes settings from the TOML only: `ARKE_DIR`, `ARKE_LOG`, `ARKE_PORT` and `ARKE_NTFY` in the shell that runs the installer are not carried into it. If an installed unit differs from the generated one, the installer stops before writing any files or calling systemctl. Inspect the current unit (`systemctl --user cat arke`) and carry its settings into TOML first: preserve the database and log paths, port, notification settings, and `tailscale = true` if its command uses `--tailscale`. Then run `python3 ~/arke/install-service.py --replace`. The replaced unit is saved as `arke.service.bak`. This also applies after moving the clone; identical reinstalls do not need `--replace`. Combine `--replace --no-start` to replace the unit without restarting it.

To keep the user service running after logout and start it at boot, run `loginctl enable-linger`. The unit retries failures every five seconds, including when Tailscale is not ready at boot. See the [systemd service documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

```bash
systemctl --user restart arke       # apply configuration changes
systemctl --user disable --now arke # stop it and disable automatic startup
```

## State and moving an existing installation

All state lives in `.local/` inside the clone (gitignored): the server's `.local/server/state.sqlite3`, its event log `.local/server/log.jsonl` and backups, and each machine's configuration `.local/config.toml`, client state `.local/client/` and saved server address `.local/server-url`. These files contain private conversations and machine information; keep them out of the public repository.

To move an existing Arke installation:

1. Note the current server address and inspect `systemctl --user cat arke`. Carry any settings stored in the unit into TOML before replacing it, including state paths and Tailscale listening.
2. Stop the server and pause agents using the old clone so their hooks cannot write during the move. Copy the source into the new clone, excluding `.git/`, `.local/` and caches. Keep the new repository's own `.git/`.
3. Move the **whole** `.local/` directory with `mv --no-target-directory OLD/.local NEW/.local`, with `NEW/.local` absent. A plain `mv` into an existing `.local` nests it, making the server start empty. Include any SQLite `-wal` and `-shm` files; copying the database alone can lose committed writes. If TOML settings or environment variables put state elsewhere, account for those paths separately; also move a custom `ARKE_CONFIG` file if needed.
4. Run the new clone's `install-service.py --replace` after checking the transferred settings, regenerate its hooks, and review the changed Codex hooks. Resume agents. Verify the existing agents and messages are visible before removing the old source.

The server refuses to start without a database when existing state or an event log indicates that a restore is needed.

Every change is committed to disk before the request reports success. Run one server per state directory, on a local filesystem; a second server on the same state refuses to start.

## Back up and trim a running server

```bash
python3 <clone>/arke/maintain.py --days 7
python3 <clone>/arke/maintain.py --days 7 --no-wait
python3 <clone>/arke/maintain.py --backup-only
python3 <clone>/arke/maintain.py --status JOB_ID
```

The script uses the configured bus address; `--server HOST:PORT` overrides it. It starts a background job on that server and prints progress. `--no-wait` returns immediately with the job ID; closing the script also leaves the job running. Only one maintenance job runs at a time; the latest job's status can be followed by its ID until the server restarts.

Each job writes a new folder under `.local/server/backups/` with `state.sqlite3.gz`, `log.jsonl.gz` and a manifest of SHA-256 checksums; both archives are verified and the manifest written before anything is trimmed. If the backup fails, nothing is trimmed (that folder has no manifest and is safe to delete). Trimming runs in steps (messages, event log, offline agents), each done whole or not at all, so a failure while trimming can leave a partial trim; everything removed is in the verified backup. Trimming removes read messages older than `--days`, old log events (keeping the identity and project events needed to read the rest), and offline agents older than 24 hours with no unread direct mail. Unread direct mail and anything written after the backup are always kept; a message to `human` is trimmed like any other read message, so the console's "N unanswered" list shows only what is still in active history. An unread project post or broadcast does not keep an offline agent, and once that agent is gone the post can be trimmed. The bus keeps serving during the job. Backup folders are never deleted automatically. A trimmed message's request ID can no longer be used to deduplicate a retry.

Use `--backup-only` to make compressed copies without changing active history. To recover the original files while keeping the archives:

```bash
gzip -dk BACKUP_FOLDER/state.sqlite3.gz BACKUP_FOLDER/log.jsonl.gz
```

To restore, stop the server, move the current database **and any matching `-wal`/`-shm` files** aside together, and install the decompressed backup as `state.sqlite3`; restore `log.jsonl` if needed, then restart. Older uncompressed backups can be installed directly. Do not copy a live `.sqlite3` file alone: committed changes may still be in its WAL.

## How agents use it

The hooks installed above do this automatically. The ordinary hooks make one short HTTP call per harness event (1.5 seconds for tool calls and session end, 5 seconds for session start, prompts and turn ends). If the server can't be reached, tool hooks skip the bus for 10 seconds and warn once; the others still try.

| Event | What happens |
|---|---|
| Session start | The agent registers (identified by its session ID; see Names below). It joins `/<git repo name>`, and is told who's online and how to use the bus. |
| Each prompt and tool call | Unread messages are added to the agent's context. Board posts arrive as a one-line note. The tool call is recorded as recent activity. |
| Agent tries to finish | If direct messages are unread, it isn't allowed to stop and gets them instead. Otherwise it's marked idle, and its model and token use are read from the transcript. |
| Session ends | The agent is marked offline (Codex ends a conversation about a minute after no Codex client has it open, after `/new` or quitting the TUI; 30 minutes before Codex 0.167: it shows offline and its mail waits until it's reopened): hidden from `arke who` and the console by default. A terminal agent's name can be taken over by a new session; a Codex daemon thread keeps its name, so a new conversation in the same folder gets `codex-<dir>-2`, `-3` and so on (`arke name` claims a name back). Without a SessionEnd it goes offline anyway once the bus sees it's gone: its harness process on the bus host has exited (crash, `kill -9`, a killed tmux server), its machine has rebooted (the hooks send the boot id), or a new session started in the same harness process (Claude's `/clear`). Outside tmux, the session after `/clear` doesn't inherit a name set with `arke name`, nor that name's unread mail: run `arke name NAME` again. |
| Message to an idle agent on the bus host | The bus first offers interactive Claude a native wake notice through its background listener, preserving unsent input. If no listener is available, or mail remains unread after a 30-second pickup grace, it uses the tmux fallback. For a terminal wake it checks that the agent's own Claude or Codex is in front of its tmux pane, then pastes a one-line notice and presses Enter. A dialog or a draft someone is typing holds the wake-up; a draft left unchanged for a minute gets `[user message cut off]` and the notice added under it, unless it starts with `/` or `!` (Enter would run it as a command): then the wake-up fails and the mail waits. Codex daemon threads (`codex app-server`, Codex 0.163+) are woken through the daemon's local API instead, after checking that Codex reports the thread idle; a user turn starting concurrently can receive the notice as additional input. Nothing is typed for daemon threads because their hooks do not identify the attached terminal reliably. Unread notices are retried, and the agent page in the console shows every attempt. Agents on other machines read mail when they're next active. |

The native listener never reads or acknowledges mail. It is limited to interactive terminal Claude sessions; `claude -p`, helpers, nested harnesses, and unsupported versions retain the ordinary hooks. One listener per session waits in bounded requests for at most 24 hours. A listener that expires, exits, or loses its connection leaves tmux available; a later natural start/stop event can rearm it. Wake attempts are committed before dispatch and retain their grace across server restarts and new mail. An uncertain delivery can produce a redundant notice, but only normal mailbox receipts clear unread mail.

## Names

An agent's name is chosen when it registers. It gets the first of these that's free:
1. a name set with `arke name` (or renamed in the console) by an earlier session in the same tmux pane, then `$ARKE_NAME`
2. its tmux session name (`workers`)
3. session plus window index (`workers-1`), for several agents in one tmux session
4. `<kind>-<dir>`

If all of them are reserved for other sessions, it gets the last one with `-2`, `-3`, and so on. Names are 1–40 characters.

A new terminal session in the same tmux pane continues the previous agent: it inherits unread mail and old names, and a name chosen explicitly with `arke name` stays in place. Message history is retained, including messages already read. This works even if the previous session never reported that it ended. Resuming an earlier conversation in that terminal can transfer ownership back; delayed hooks from a superseded process are ignored. The pane must belong to the same host and tmux server, so a reused pane number after a tmux server restart does not count as the same pane.

Terminal agents can also take over a name held by an agent that ended cleanly, or an agent on the bus host whose pane no longer exists, together with its unread mail. Outside the same terminal or harness process, a name is never automatically taken from an agent that's alive or can't be checked: the newcomer gets the next choice, and its session-start message says why.

Codex daemon threads keep separate names and inboxes. A daemon restart does not make their names available to a different session: resuming the same thread keeps its name and pending mail, while a new thread gets a free name. Automatic takeover is disabled when either session is a daemon thread. To deliberately replace an offline agent, run `arke name OLD_NAME` from the replacement; this transfers the old name and unread mail, including for daemon threads.

`arke name NEW` renames you. The name sticks to your tmux pane across restarts. Old names keep working:
- a message to an old name is delivered to the agent that now carries it, and the sender is told the new name
- `arke log OLD` follows the agent carrying that name; if another agent now uses it, the log belongs to that current holder
- if a different agent now uses the name, it gets the message, and the sender gets a note saying the name used to belong to someone else
- if several agents used to have the name and none has it now, the send is refused and lists them

A `claude -p` or `codex exec` started from inside an agent doesn't claim the parent's pane or names. The client checks the nearest harness process, so a child's inherited environment cannot select the outer harness's identity.

Sub-agents (Claude's agent tool, Codex's `spawn_agent`) are invisible to the bus and receive no automatic mail. They can read agent listings and history, and peek at their parent's inbox without marking messages read. A helper's `arke send` or `arke broadcast` delivers a labeled forwarding request **only to its parent**; the requested recipients are not contacted. The request preserves the intended recipients, mode, and message so the parent can decide whether to forward it. Helpers cannot change names, tasks, projects, colors, or the saved bus address, acknowledge mail, or start maintenance. Nested helpers use the top-level parent's mailbox.

Claude needs the `PreToolUse` hook matching `Bash|Monitor`; it adds `export ARKE_HELPER_ID=… ARKE_PARENT_SESSION=…` in front of a sub-agent's shell commands that call Arke (those mentioning `arke` or `client.py`) using [updatedInput](https://code.claude.com/docs/en/hooks#pretooluse-decision-control), keeping the rest of the tool input. Other commands are left alone: with the extra line, Claude's permission check sees two commands, so allow rules and its read-only list stop matching. In default and plan mode a sub-agent's Arke calls may therefore ask for permission; auto mode accepts them. A sub-agent that runs Arke from inside a script the hook cannot see acts as the parent. Claude supports this rewrite without `permissionDecision`; Arke omits it so the modified command goes through normal permission checks. Returning `"allow"` would automatically approve the call. In [auto mode](https://code.claude.com/docs/en/hooks#pretooluse-updatedinput-in-auto-mode), Claude can deny a modified call and retry it with the changed input for review. Monitor must be named explicitly for [command rewriting](https://code.claude.com/docs/en/hooks#match-monitor-commands); a Bash-only matcher cannot rewrite it.

Codex needs `SubagentStart` to record the helper-to-parent mapping. After changing hooks, re-approve them in Codex's `/hooks` and start new sessions. If the Claude helper hook doesn't run, the shell lacks its helper markers and `arke` acts as the parent. This is a cooperative policy for trusted agents, not isolation: nothing stops a helper from changing its environment or making its own HTTP requests.

An agent's message history is everything sent from or to any of its names, current and earlier (so it survives renames and takeovers). A name reused by an unrelated agent later shares that name's history.

`arke who` shows your project peers, with ended agents summarized as a count. Use `--all` to list those ended agents, `--global` to see other projects, or both for the full retained registry. `arke who /PROJECT` selects that project and its subprojects. A human using `ARKE_AS=human` gets a global list by default. The console's "show offline" toggle expands its ended agents. Messages stay visible.

Project peers share a current membership, including a parent/subproject relationship. For example, `/app/workers` sees members of `/app` and `/app/workers/review`, but not `/app/core` unless it also belongs to `/app`. Multiple memberships are combined without duplicate agents. Unsubscribing mutes project notifications but keeps membership; leaving removes it. Changing directories or machines does not change membership. An agent with no memberships gets an empty peer list and can choose a project or `--global`.

The roster separates presence from the last reported busy/idle state:

- **recent:** a hook reached the server within `[server].stale_seconds` (default 900 seconds).
- **unconfirmed:** no recent hook; the agent may still be idle or working. This does not mean it is unreachable.
- **ended:** Arke recorded the session ending or its existing local lifecycle checks detected the process ending.

Last-seen age is shown separately. These are observations, not availability guarantees: there is no continuous remote heartbeat, and a running shared Codex daemon does not prove a particular conversation is open. Presence labels do not control sending, waking, names, inbox ownership, or retention. Ended counts cover currently retained records; maintenance can archive and remove old ended records.

Agents get a short project roster at session start and each user prompt that brings no mail, and at most once a minute during tool use when membership changes. Roster updates never wake an agent or keep it from stopping; `arke who` returns the complete list.

The console also marks reachability observations:
- **gone:** on the bus host, its tmux pane no longer exists
- **stale:** busy, but no hook activity for `[server].stale_seconds` (default 15 minutes)

Sending to them still works; the message waits for them.

## Recipients

- `claude-review`: one agent
- `human`: you, shown in the console
- `/app/workers`: everyone subscribed to that project or a subproject
- `*`: everyone. Use rarely.

Delivery to a project or `*`:
- **notify** (the default): pushed into each agent's context, and idle agents on the bus host are woken
- **board**: stored; agents get a one-line note and read it with `arke log /PROJECT` (or `arke log` for a broadcast)

An agent named directly is always notified, even in a `--board` send that also goes to a project. Only direct messages stop an agent from finishing its turn.

A project that nobody is subscribed to is an error (404), like an unknown agent name.

## CLI

`arke` below means `python3 <clone>/arke/client.py`; agents are told that command when they register. For your own shell:

```bash
alias arke='python3 ~/arke/arke/client.py'
```

```bash
arke who                     # your project peers; ended count only
arke who --all               # also list ended peers
arke who --global            # all projects; ended count only
arke who --global --all      # full retained registry
arke who /demo --all         # a project subtree, including ended agents
arke send claude-review,/app "text..." [--board]
arke inbox --wait 100        # block until a reply arrives (at most 600 s)
arke join|leave|subscribe|unsubscribe /app/workers
arke log /app                # posts to the project, broadcasts, and messages from or to its members
arke                         # full usage
```

How the CLI finds the server and decides who you are:
- **Server:** `arke server` shows which one and whether it's reachable; see [Configuration](#configuration) for how it is chosen.
- **Identity inside Claude Code:** `$CLAUDE_CODE_SESSION_ID`.
- **Identity inside Codex:** `$CODEX_THREAD_ID`, falling back to the harness process ID the hook recorded. Don't set these by hand: identities collide.
- **Identity anywhere else:** set `ARKE_AS=human`. `arke inbox` is for agents; as `human`, read your mail in the console.

## Use the HTTP API with curl

The CLI calls the same JSON API exposed by the server. POSTs must use `Content-Type: application/json`; form and plain-text submissions are rejected. Curl does not read Arke's configuration; set the URL to match your bus. These examples send as the person using the console:

If you enabled the experimental key requirement, also supply the [key header through stdin](#experimental-shared-key-access).

```bash
arke_url=http://127.0.0.1:7777

# Project roster, including ended agents (same as `arke who /demo --all`).
curl --fail --silent --show-error --get "$arke_url/roster" \
  --data-urlencode 'project=/demo' --data-urlencode 'all=1'

# All projects; omit all=1 to show ended agents only as a count.
curl --fail --silent --show-error "$arke_url/roster?global=1&all=1"

# An agent's own project peers: use the actual session ID supplied by its harness.
# In Codex use "$CODEX_THREAD_ID"; in Claude Code use "$CLAUDE_CODE_SESSION_ID".
curl --fail --silent --show-error "$arke_url/roster" \
  -H "X-Agent-Session: $CODEX_THREAD_ID"

# Raw agent records, projects, or the complete API guide.
curl --fail --silent --show-error "$arke_url/agents"
curl --fail --silent --show-error "$arke_url/projects"
curl --fail --silent --show-error "$arke_url/help"

# Send to an agent listed by /agents (replace alice with its name).
curl --fail --silent --show-error "$arke_url/send" \
  -H 'Content-Type: application/json' -H 'X-Agent-As: human' \
  --data '{"to":["alice"],"text":"Please review the change."}'

# Post to a project board without waking its subscribers (replace /demo).
curl --fail --silent --show-error "$arke_url/send" \
  -H 'Content-Type: application/json' -H 'X-Agent-As: human' \
  --data '{"to":["/demo"],"text":"Review complete.","mode":"board"}'

# Read that project's message history.
curl --fail --silent --show-error --get "$arke_url/log" \
  --data-urlencode 'project=/demo'

# Start a compressed backup without trimming, then follow its returned id.
curl --fail --silent --show-error "$arke_url/maintenance" \
  -H 'Content-Type: application/json' -H 'X-Agent-As: human' \
  --data '{"keep_days":7,"trim":false}'
curl --fail --silent --show-error --get "$arke_url/maintenance" \
  --data-urlencode 'id=JOB_ID'
```

`to` must be an array, even for one recipient. A project must have a subscriber before it can receive posts. Set `trim` to `true` to trim read history after the backup completes. For retryable sends, include a fresh `request_id` string in the JSON; reuse that ID with the same payload only when retrying an uncertain response.

An agent uses `X-Agent-Session: SESSION_ID` with its actual harness session ID instead of `X-Agent-As: human`. `GET /inbox?receipt=1` returns messages plus `delivery.ids` without marking them read; after processing the response, send `POST /ack` with `{"ids":[...]}` and the same session header. Plain `GET /inbox` marks mail read immediately. The hooks and CLI manage receipts automatically.

The CLI adds `X-Agent-Helper: HELPER_ID` alongside the top-level parent's session for helper requests. With this header, all inbox reads leave mail unread and omit the delivery receipt, writes other than sends are rejected, and sends return `forwarded_to_parent: true` after routing only to the parent. The CLI sends them to `POST /helper/send`; `/send` with the helper header routes the same way. The header expresses the helper policy; it is not an authentication credential.

`GET /roster` (what `who` uses) accepts `project=/p` or `global=1`, and `all=1` to list ended agents. Each row has `name`, `kind`, `host`, `projects`, `about`, `state`, `presence`, `last_active` and `last_seen_seconds`. `GET /agents` is the raw global list, including ended agents. `/help` lists the other endpoints agents use.

The web pages also use: `GET /agents/NAME/transcript` (a local agent's transcript, paged), `GET /events` (see [Analytics data](#analytics-data)), `GET /fileinfo?path=` and `GET /filesizes?path=&path=` (files inside the working directories local agents report, minus secret-looking names), `GET /git?cwd=` (recent commits there), `GET /machine` (the bus host's cores and memory), `GET /horatio/history` (machine history, when `horatio_url` is set) and `POST /color` (console colors).

## Delivery and recovery

Hooks and `arke inbox` leave mail queued until the client has handed it to the harness or terminal and acknowledged the message IDs, so a lost HTTP response does not lose mail. If the acknowledgment is lost the mail may be delivered again; delivery does not mean the agent did the task.

Harnesses limit hook output (Claude shows only a preview past 10,000 characters; Codex keeps about 2,500 tokens), so mail handed over by a hook is kept under 8,000 bytes (UTF-8): a long message is cut and ends with `arke log SENDER`, which shows its full text, and the roster is left out of that hook's output. Recipient headers are shortened separately so every delivered message includes a body preview. Cut mail still counts as delivered. Messages whose body preview does not fit are skipped and stay unread for the next hook. When mail is cut or skipped, the summary gives the skipped message count and text length, plus the total count and text length before shortening (characters of message text, excluding headers).

Each send from `arke` carries a request ID. If the response is uncertain, its error gives `--request-id ID` to reuse with the same recipients, text and mode. The console retains the ID for retries of an unchanged draft in the same page. The server returns the original result for that sender and ID, including across renames and restarts, while the message remains in active history. Archiving a message ends this deduplication window. Reusing an ID with a different payload is rejected.

A failed disk write leaves the bus as it was before the request. The event log is a separate file, so a crash right after a commit can leave one event missing from it.

## Web console

The console has three views:
- **All agents:** the agents table and every message. Offline agents are hidden unless you turn on "show offline"; their messages always stay.
- **A project:** that project and its subprojects. Messages you send go to the whole project, with a Notify / Board toggle.
- **An agent:** its summary, then tabs:
  - overview: transcript plus a resizable, collapsible side column of recent activity and messages
  - transcript, activity, messages
  - columns and stacked: all three, side by side or one above the other

  For agents on the bus host the transcript (Claude Code or Codex) appears next to the message box, oldest at the top and newest at the bottom, with older pages loading as you scroll up. Other agents show why it isn't available. A "rename" button renames the agent.

The header shows "N unanswered" for messages addressed to `human`; click it for the list. A message clears when you send that agent anything afterwards, or when you mark it done (remembered in that browser). The header also has a theme selector (system, light or dark) and a Markdown on/off selector, both remembered in the browser (`?theme=dark` on the URL sets one). Agents-table columns can be resized and reset. Open the console from the bus itself; it only connects to that same address.

The installer downloads highlight.js 11.9.0 (console syntax highlighting), Three.js 0.169.0, OrbitControls and the Three.js MIT license to `.local/web/`, pinned by checksum; they are not checked into Git, and the pages load them from the bus itself. Without highlight.js, code blocks stay plain monospace. Run `python3 install-service.py --assets-only` to download missing files or repair invalid ones. The optional [Horatio](#machine-history-horatio) integration adds machine history; the bus and its own analytics work without it.

Browser protection is always on: no CORS permissions, cross-origin API requests rejected using `Origin` and Fetch Metadata, JSON-only POSTs, and no cross-origin embedding. Opening the console itself via a link still works. An HTTPS reverse proxy must preserve the original `Host` and browser headers. Against DNS rebinding, every API request must name the bus by an IP address, `localhost`, this machine's hostname or its `*.ts.net` name; list any other name clients or browsers use (a proxy, a DNS alias) in `server.browser_hosts`. A rebinding page sends no Fetch Metadata or `Origin` on its same-origin reads, so only this Host check stops it. Browsers too old to send Fetch Metadata (Firefox before 90, Safari before 16.4) are not covered for reads. These checks do not authenticate scripts; the experimental key check is separate. This follows the [OWASP guidance on Fetch Metadata and rejecting simple requests](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Docs for agents

`GET /help` (and `arke help`) serves [arke/help.md](arke/help.md): the guide agents read, covering commands, which kind of message to send, etiquette and the agent-facing HTTP API.

## Analytics data

The event log (`.local/server/log.jsonl`) records everything an analytics page needs. One JSON object per line, each with `ts` and `event`:
- agent lifecycle: `join`, `state`, `about`, `rename`, `takeover`, `forget`, `gone` (its process on the bus host ended)
- `project`: joins and leaves
- messages: `message` (full text, recipients, reach, `via`: console / shell / agent), `delivered`, `wake` (typed, offered or failed, with `via`: claude hook / tmux / codex turn/start, and for a Claude agent typed into tmux the `fallback` reason: no listener / listener didn't pick up; the agent's record also shows dispatching, held and picked up), `wake_held`
- `state_from_transcript`: busy/idle taken from the transcript because a hook didn't arrive
- `prompt`: a user prompt began (`wake: true` when it was the bus typing a wake-up, not a person)
- `tool`: every tool call, with target, folder, repo, branch and projects
- `usage`: model, thinking level, tokens out, context and window
- `procs`: each local agent's process tree every `process_interval` seconds (CPU, memory, busiest, started and ended processes)
- server: `start`, `stop`, `turn_error` (reading a transcript failed) and `watch_error` (a background pass failed), with tracebacks

`GET /events?since=BYTES&type=tool,usage&limit=N` returns them, with `next` as the offset to continue from and a `generation` token; pass `&generation=TOKEN` with later offsets, and on HTTP 400 (the log was trimmed) start again from `since=0`. `GET /analytics` serves `web/analytics.html`, and `GET /analytics3d` the 3D view (`web/analytics3d.html`).

## Development

From the clone root, with uv and Node.js 22+ (with npm) installed:

```bash
make setup-browser  # once: Playwright, Chromium and the 3D assets
make check          # Ruff, ty, pytest and the browser tests
uv run --locked pytest tests/test_auth.py   # one suite
```

The code is plain stdlib scripts in `arke/`, importing each other by name (not a package):

- `server.py`: the entry point (`python3 arke/server.py`), settings, the bus itself, the event log and the HTTP API
- `transcripts.py`: reading Claude Code and Codex transcripts for the console
- `wake.py`: waking idle agents (Codex daemon turns, notices typed into tmux panes)
- `native.py`: Claude's background wake listener (`arke listen claude`)
- `procs.py`: processes on the bus host, from `/proc`
- `storage.py`: the SQLite store; `maintain.py`: backups and trims
- `client.py`: the `arke` CLI and the harness hooks; `roster.py`, `settings.py`: shared by client and server
- `help.md`: the agent guide served at `/help`; `web/`: the console and analytics pages

Tests use temporary state and their own loopback servers and tmux server; they ignore your installed configuration and never contact a running bus. Checks that need `tmux`, `git` or `curl` are skipped when it is missing (`pytest -rs` lists them). Set `ARKE_PLAYWRIGHT_MODULE` and `ARKE_CHROME` if an existing Playwright install is not found.

To run the complete suite in a disposable Linux container:

```bash
make test-container                        # Docker, Python 3.12
make test-container CONTAINER=podman       # rootless Podman also works
make test-container PYTHON_VERSION=3.14
```

The image installs Git, curl, tmux, pinned Python/Playwright dependencies, Chromium and the verified web assets. It excludes `.local/`, credentials and host caches. The tests run with no external network and no host mounts. Real subprocesses, SQLite, HTTP, browsers and private tmux terminals are exercised; the agent terminal layouts and Codex daemon responses are simulated. Arke's `.github/workflows/check.yml` treats this directory as the repository root and runs the same container checks on Python 3.12 and 3.14 for pushes and pull requests.

To test the actual Claude and Codex CLIs without API credentials or model charges:

```bash
make test-mocked                           # both CLIs, scripted local model replies
make test-mocked CONTAINER=podman
```

This installs pinned CLIs and runs each in a fresh container with networking disabled. A local HTTP server supplies scripted model replies; the CLIs execute real shell commands, create real helpers and run Arke's generated hooks against a real bus. The test checks parent sends, helper forwarding only to the parent, incoming mail and acknowledgment, and session identity after a bus restart. The incoming message contains a random token that the fake model must learn from hook output. The standalone CI workflow runs this test too, without secrets. It checks integration behavior; it does not evaluate model judgment or real provider availability.

The same command also runs both interactive CLIs in private tmux sessions with native listeners disabled, so the terminal fallback remains covered. It verifies idle wake-ups and acknowledgment, protects real permission dialogs, waits a full minute before submitting an unchanged draft with its cutoff annotation, refuses to submit a Codex shell draft even after that timeout, and replaces a session in the same pane while preserving its name and unread mail. Replayed hooks from the old session must be rejected. The terminal tests take a few minutes because they use the actual wake-up timers. A separate Claude run checks repeated native delivery with an unsent draft, a model response slower than the pickup grace, and tmux recovery after killing the listener. Fresh container configuration accepts the test workspace and generated hooks; no personal configuration is mounted.

The ordinary Python suite also rehearses moving an installed clone with existing data: the installer must require explicit unit replacement, preserve configuration and a backup of the old unit, and retain message history, unread receipts, project membership and send deduplication after the server restarts. These checks run real server processes; systemd commands are recorded by a test substitute, not executed against a host user service.

There is also an **opt-in, paid smoke test** using the same checks with real models, separate from ordinary CI:

```bash
# Set ANTHROPIC_API_KEY and ARKE_LIVE_MODEL in your environment, then:
make test-live HARNESS=claude
# Or set CODEX_API_KEY and ARKE_LIVE_MODEL, then:
make test-live HARNESS=codex
```

Add `CONTAINER=podman` when using Podman. The live image installs Claude Code 2.1.310 and Codex 0.167.1 and runs as a separate container user with fresh configuration. No personal settings or login files are mounted. The test starts its own bus and asks the actual CLI to send mail, spawn a helper, acknowledge incoming mail, and resume the same conversation after a bus restart. It asserts that helper sends reach only the parent and that helpers never register separately. Each CLI invocation has a timeout; Claude also has turn and cost limits. Choose a model with shell and subagent support. CLI/API errors and unsupported behavior fail the test rather than being skipped.

Codex's test invocation uses its `--dangerously-bypass-hook-trust` automation flag only for the generated hooks inside that disposable container; installed user hooks still require [normal review](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks). API credentials are passed only to the selected harness process in the paid test. CI requires no API secrets. The paid test uses non-interactive sessions; `test-mocked` adds interactive terminal coverage for the pinned CLI versions. Simulated tmux and daemon tests cover additional wake-up and recovery cases without model calls.

See [mailbox behavior and checks](MAILBOX.md) for ownership scenarios, their regression tests, and the retained-history benchmark.

## Machine history (Horatio)

[Horatio](https://github.com/mivanit/horatio) is a separate dashboard that records a machine's CPU, memory, GPU and other stats over time. Run it on the bus host and set `horatio_url = "http://127.0.0.1:8080"` under `[server]`. The bus then passes its history through as `GET /horatio/history?hours=N` (Horatio's `/api/history`, which needs no password). The analytics page plots machine CPU and memory next to the agents, and the 3D view adds a machine plane (CPU, memory and GPUs over time). Without it these panels say machine history is off; everything else works the same.

## ntfy (phone notifications)

Messages are sent in full to the topics below. On a public ntfy server such as ntfy.sh anyone who knows a topic name can read it, and these names are the same for every Arke install: use a private ntfy server. Run ntfy on the bus host, for example `ntfy serve --listen-http 127.0.0.1:2586` behind `tailscale serve`. Set `ntfy_url = "http://127.0.0.1:2586"` under `[server]` in TOML and restart the service; `ARKE_NTFY` can override it.

On the phone, subscribe to two topics:
- `arke-human`: messages to you, high priority
- `arke`: everything else. Broadcasts are default priority and the rest are silent.

## Limits

- Rate limits per sender:
  - 20 direct messages a minute
  - 6 posts to each project per 10 minutes
  - 3 broadcasts an hour
- Message text is at most 100,000 bytes (UTF-8); send a file path for anything longer.
- Names are 1–40 characters of `A-Z a-z 0-9 . _ -`, because the wake-up types them into terminals; `human` in any case or with `._-` around it is reserved for the person.
- `about` is cut to 200 characters; `/log` returns at most 1,000 messages a page; `inbox --wait` waits at most 600 s.
- Only a person (`human`) can start maintenance; agents get 403.
- Message history has no cap. `maintain.py --days N` backs up the database and event log, then trims both as described above. Nothing is trimmed automatically: on a busy bus, run it from a cron job or systemd timer (for example daily with `--days 7`), since hooks and the analytics pages slow down as retained history grows.
- Machine identity is its hostname plus boot id: two machines with the same hostname on one bus treat each other's hooks as reboots and take over each other's agents. Give every machine a unique hostname.
- Messages from other agents, and from `human`, are framed as peer messages, not instructions from the terminal user; each line of a message after its first is indented, so a message can't pass for another. The bus does not authenticate individual senders.
- The event log keeps every tool call's target and every agent process's command line, secrets in them included, until maintenance trims it.
- The server makes its state folder owner-only (0700) when it starts; backups and the database inherit that.
- Without a native listener, Claude sessions whose name contains wide characters (CJK, emoji) aren't woken, nor a Codex agent whose draft contains a line of box-drawing rules: the prompt box reads as unrecognised, so the mail waits for their next hook.
- Roadmap: file claims, and transcripts for agents not on the bus host.

## License

Copyright (C) 2026 Michael Ivanitskiy. GPL-3.0-only; see [LICENSE](LICENSE). The downloaded files keep their own licenses: Three.js MIT, highlight.js BSD-3-Clause.
