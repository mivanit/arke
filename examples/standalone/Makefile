# Run with: make -C examples/standalone <target>
ROOT := $(abspath ../..)
DATA ?= $(ROOT)/.local/standalone
UV ?= uv
DOCKER ?= docker
export UV_CACHE_DIR := $(ROOT)/.local/uv-cache
export PYTHONDONTWRITEBYTECODE := 1
export TMPDIR := $(ROOT)/.temp/work

.PHONY: setup image login run
.DEFAULT_GOAL := setup

# Host needs only Arke's web extra; tools run inside the worker image.
setup:
	$(MAKE) -C "$(ROOT)" setup EXTRAS=web UV="$(UV)"
	install -d -m 700 "$(DATA)" "$(DATA)/state" "$(DATA)/workspace" "$(DATA)/native"
	@if [ ! -e "$(DATA)/agent.toml" ]; then install -m 600 agent.toml "$(DATA)/agent.toml"; fi

# Both builds download/install their dependencies; neither needs a native login.
image:
	$(DOCKER) build -f "$(ROOT)/Dockerfile.agent" -t arke-agent:local "$(ROOT)"
	$(DOCKER) build -f "$(ROOT)/examples/standalone/Dockerfile" -t arke-standalone:local "$(ROOT)"

# Native credentials remain in the dedicated home; no API key or owner-home mount.
login:
	$(DOCKER) run --rm -it --user "$$(id -u):$$(id -g)" --env HOME=/home/agent --env CODEX_HOME=/home/agent/.codex --mount "type=bind,src=$(DATA)/native,dst=/home/agent" --entrypoint codex arke-standalone:local login --device-auth

run:
	"$(ROOT)/.venv/bin/arke-web" --config "$(DATA)/agent.toml"
