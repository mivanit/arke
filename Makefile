# Python 3.12+, Node.js, Git, and tmux are required for the full test suite.
# Python development tools come from uv.lock; make setup-browser installs browser dependencies.
UV ?= uv
NODE ?= node
PYTHON ?= python3
EXTRAS ?= web
ASSETS_DIR ?= $(CURDIR)/.local/web
export UV_CACHE_DIR ?= $(CURDIR)/.local/uv-cache
export npm_config_cache ?= $(CURDIR)/.local/npm-cache
export PLAYWRIGHT_BROWSERS_PATH ?= $(CURDIR)/.local/playwright
export TMPDIR := $(CURDIR)/.temp/work
export PYTHONDONTWRITEBYTECODE := 1
export ARKE_TEST_ASSETS := $(ASSETS_DIR)
CONTAINER ?= docker
PYTHON_VERSION ?= 3.12
TEST_IMAGE ?= arke-test:$(PYTHON_VERSION)
AGENT_IMAGE ?= arke-agent:local
# extra `run` flags for test-mocked, e.g. to let Codex's bubblewrap sandbox create namespaces (see the CI workflow)
RUN_FLAGS ?=
HARNESS ?= claude
LIVE_KEY = $(if $(filter claude,$(HARNESS)),ANTHROPIC_API_KEY,CODEX_API_KEY)

.DEFAULT_GOAL := check
.PHONY: setup setup-assets check-assets check-setup test-installation prepare check format check-format typing test test-python test-browser setup-browser test-container test-mocked test-deployment test-live demo demo-build

# Runtime dependencies are selected together; dev/test setup remains separate.
setup: prepare
	$(UV) sync --locked --no-dev $(foreach extra,$(EXTRAS),--extra $(extra))

prepare:
	mkdir -p "$(TMPDIR)"

setup-assets: prepare
	$(PYTHON) -m arke.assets install --directory "$(ASSETS_DIR)"

check-assets:
	$(PYTHON) -m arke.assets check --directory "$(ASSETS_DIR)"

# Checks the installed Python environment without syncing it or contacting a model.
check-setup:
	$(UV) pip check --python .venv/bin/python

check: check-format typing test

format: prepare
	$(UV) run --locked --all-extras ruff check --fix .
	$(UV) run --locked --all-extras ruff format .

check-format: prepare
	$(UV) run --locked --all-extras ruff check .
	$(UV) run --locked --all-extras ruff format --check .

typing: prepare
	$(UV) run --locked --all-extras ty check --error-on-warning

test: test-python test-browser

test-python: prepare
	$(UV) run --locked --all-extras pytest

# under uv run, the bus it spawns (`python3`) is the project's Python, not whatever PATH has
test-browser: prepare
	$(UV) run --locked --all-extras $(NODE) tests/test_console.cjs
	$(UV) run --locked --all-extras $(NODE) tests/test_chat.cjs
	$(UV) run --locked --all-extras python -m tests.deployment_ci --local-fixtures

# Downloads test dependencies and 3D assets; does not install or start the service.
setup-browser: setup-assets
	npm ci
	npx playwright install chromium

# No live state, credentials, host mounts, or external network during the tests.
test-container:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) run --rm --network=none --shm-size=1g $(TEST_IMAGE)

# Actual CLIs and hooks, with scripted local model replies and no credentials.
test-mocked:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) build --build-arg TEST_IMAGE=$(TEST_IMAGE) -f Dockerfile.live -t arke-live-test .
	$(CONTAINER) run --rm --network=none --shm-size=1g $(RUN_FLAGS) arke-live-test claude --mock-model
	$(CONTAINER) run --rm --network=none --shm-size=1g $(RUN_FLAGS) arke-live-test codex --mock-model
	$(CONTAINER) run --rm --network=none --shm-size=1g $(RUN_FLAGS) --entrypoint python3 arke-live-test -m tests.tui_smoke claude
	$(CONTAINER) run --rm --network=none --shm-size=1g $(RUN_FLAGS) --entrypoint python3 arke-live-test -m tests.tui_smoke claude --native
	$(CONTAINER) run --rm --network=none --shm-size=1g $(RUN_FLAGS) --entrypoint python3 arke-live-test -m tests.tui_smoke codex

# Production Docker restrictions, fake native conversations, real browser and
# offline CLI contract/tool probes. Requires Docker, uv, Node and setup-browser.
# No RUN_FLAGS escape hatch: the production security boundary is the assertion.
test-deployment: prepare
	docker build -f Dockerfile.agent -t $(AGENT_IMAGE) .
	$(UV) run --locked --all-extras python -m tests.deployment_ci --image $(AGENT_IMAGE)

# Explicit opt-in: real model calls, separate from the credential-free CI suite.
test-live:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) build --build-arg TEST_IMAGE=$(TEST_IMAGE) -f Dockerfile.live -t arke-live-test .
	$(CONTAINER) run --rm --shm-size=1g -e $(LIVE_KEY) -e ARKE_LIVE_MODEL arke-live-test $(HARNESS)

# The consoles on invented data, no bus needed (demo/README.md). Run setup-assets first.
demo-build:
	$(PYTHON) demo/build.py _site --assets-directory "$(ASSETS_DIR)"

demo: demo-build
	python3 -m http.server -d _site 8000

# Builds a wheel and tests isolated installations, with no native logins or model calls.
test-installation: setup-assets
	$(UV) run --locked --all-extras python -m tests.installed_package --uv "$(UV)" --assets-directory "$(ASSETS_DIR)"
