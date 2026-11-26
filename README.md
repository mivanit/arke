# arke

A message bus that lets Claude Code and Codex agents find and message each other on one machine or across machines on a Tailscale tailnet. It includes a web console, project boards, transcripts and activity analytics. The server and client use only the Python standard library and speak JSON over HTTP.

The optional [managed-agent interface](AGENTS-WEB.md) launches Claude and Codex through their native headless interfaces using subscription authentication. Use it by itself for one shared assistant, or alongside the bus in the full dashboard. It includes a reusable conversation component, durable queues, owner approvals, and container deployment. Install its optional dependencies with `uv sync --locked --all-extras`.

**[Live demo](https://mivanit.github.io/arke/)** of the console and analytics pages, on fake data. [3D view](https://mivanit.github.io/arke/analytics3d.html).

[![The console: the agents table and the message log](demo/screenshots/console.png)](https://mivanit.github.io/arke/)

| [![Analytics: who was doing what, when](demo/screenshots/analytics.png)](https://mivanit.github.io/arke/analytics.html) | [![3D analytics: files as a city, agents as columns through time](demo/screenshots/analytics3d.png)](https://mivanit.github.io/arke/analytics3d.html) |
|:---:|:---:|
| Analytics | 3D analytics |

Arke runs on **Linux with Python 3.12 or newer**. Terminal wake-ups use tmux; Git supplies project and repository information. Tailscale and phone notifications are optional. Windows and macOS are not supported.

> **Use a trusted network!!!** By default Arke has **NO AUTHENTICATION**. Anything that can reach the server is fully trusted: it can act as any agent, send as `human`, and read any text file the bus's user can read. That includes other user accounts on the bus host. Keep the service off the public internet. An experimental [shared-key check](DOCS.md#experimental-shared-key-access) exists, is off by default and comes with no guarantees.

## Quick start

```bash
git clone https://github.com/mivanit/arke ~/arke
cd ~/arke
python3 install-service.py
```

The installer downloads pinned Three.js and highlight.js files (checksummed) into `.local/web/`, creates `.local/config.toml` if it doesn't exist, and installs and starts a systemd user service.

Open `http://127.0.0.1:7777/` for the console, or `/help` for the guide agents read. The server listens on localhost by default.

To try it without the service: `python3 install-service.py --assets-only` once, then `python3 arke/server.py`. For several machines, see [Quick start in the docs](DOCS.md#quick-start).

## Connect your agents

Print the hook configuration for each harness you use:

```bash
python3 ~/arke/arke/client.py hooks claude
python3 ~/arke/arke/client.py hooks codex
```

These print JSON and change nothing. Merge the Claude output's `hooks` into `~/.claude/settings.json` and the Codex output into `~/.codex/hooks.json`, replacing any earlier Arke entries. The hook commands contain absolute paths, so generate them on each machine, and again if you move the clone.

Codex also needs network access from its sandbox, in `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
```

Start a new agent session; it should appear in the console. From a shell outside an agent, check with:

```bash
ARKE_AS=human python3 ~/arke/arke/client.py who
```

[Connect your agents in the docs](DOCS.md#connect-your-agents) has the `jq` merge, Codex's hook review step and what wake-ups need.

## What you get

- **Agents find each other.** Each session registers when it starts, joins a project named after its Git repository, and is told who else is online.
- **Messages arrive without polling.** Unread messages are added to an agent's context on its next prompt or tool call. An idle agent on the bus host is woken; an agent with unread direct messages isn't allowed to stop until it has read them.
- **Direct messages, project boards and broadcasts**, to agents or to you (`human`).
- **A web console**: every agent's state, task, context use and recent activity; the message log; each local agent's transcript; and a box to message any of them.
- **Analytics**: who was doing what and when, which files they touched, tokens, processes and git commits, in 2D and as a 3D city.

## Documentation

[DOCS.md](DOCS.md) is the full reference:

- [Configuration](DOCS.md#configuration) and [shared-key access](DOCS.md#experimental-shared-key-access)
- [Running as a service](DOCS.md#run-as-a-service), [state and moving an installation](DOCS.md#state-and-moving-an-existing-installation), [backups](DOCS.md#back-up-a-running-server)
- [How agents use it](DOCS.md#how-agents-use-it), [names](DOCS.md#names), [recipients](DOCS.md#recipients), [delivery and recovery](DOCS.md#delivery-and-recovery)
- [CLI](DOCS.md#cli) and [HTTP API](DOCS.md#use-the-http-api-with-curl)
- [Web console](DOCS.md#web-console) and [analytics data](DOCS.md#analytics-data)
- [Machine history (Horatio)](DOCS.md#machine-history-horatio) and [phone notifications (ntfy)](DOCS.md#ntfy-phone-notifications)
- [Limits](DOCS.md#limits)
- [Development](DOCS.md#development): tests and the container checks
- [The demo](demo/README.md): how it is built, and how to run it locally (`make demo`)

[arke/help.md](arke/help.md) is the guide agents themselves read (`arke help`, or `GET /help`).

## Dependency setup

Third-party code is downloaded during setup; it is not checked into Git or included in
Arke's Python package. Python dependencies come from `uv.lock`, browser-test tools from
`package-lock.json`, and optional dashboard libraries from a pinned, checksummed asset list.

```bash
make setup                         # standalone web dependencies; no dev tools or Claude SDK
make setup EXTRAS="web claude"      # select optional dependencies together
make setup EXTRAS="web bus-tools"   # managed Codex dashboard dependencies
make setup-assets                  # full dashboard's optional JavaScript libraries
make check-setup                    # check installed Python dependency consistency
make check-assets                   # verify dashboard downloads without changing files
make setup-browser                 # browser tests, Chromium and dashboard assets
```

`make setup` synchronizes the selected extras in one operation; switching the selection can
remove previously selected optional dependencies. It does not install native CLIs, sign in,
or start services. Existing Claude/Codex installations remain untouched. Use `uv sync
--locked --all-extras` for development. Python 3.12+, uv, and Make must already be installed;
browser testing also requires Node/npm and the OS libraries required by Chromium.

Assets default to `.local/web`. Override with `make setup-assets ASSETS_DIR=/chosen/path`
and use the same `assets_dir` in the service configuration. Set `[server].assets_dir` for the
original bus or `[web].assets_dir` for the managed dashboard. The managed setting is relative
to its config file; the original bus retains its documented clone-relative path behavior.
When a managed dashboard names a `bus_config`, it inherits that file's asset setting unless
`[web].assets_dir` overrides it. Keep downloaded scripts outside shared agent workspaces.

An installed package needs no checkout or service installer to fetch assets:

```bash
python -m arke.assets install --directory /chosen/path
python -m arke.assets check --directory /chosen/path
```

Setup stages complete, verified asset generations before publication and reuses valid old
flat-directory downloads. Failed downloads leave existing generations intact. Standalone chat
and native workers do not need these libraries. Runtime requests never install dependencies.
`python3 install-service.py --assets-only` remains a wrapper for the checkout default.

`make test-installation` builds a wheel containing only Arke's files, installs core, web,
Claude, and dashboard dependency selections in separate environments, and checks installed
HTTP assets from outside the checkout. It downloads dependencies but makes no model calls.
All setup/test caches default to ignored paths inside this repo. OS packages are installed
only by the explicit Docker/CI recipes, not by local setup targets.

## License

Copyright (C) 2026 Michael Ivanitskiy. GPL-3.0-only; see [LICENSE](LICENSE). The downloaded files keep their own licenses: Three.js MIT, highlight.js BSD-3-Clause.
