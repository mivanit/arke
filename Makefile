# Python 3.12+, Node.js, Git, and tmux are required for the full test suite.
# Python development tools come from uv.lock; make setup-browser installs browser dependencies.
UV ?= uv
NODE ?= node
CONTAINER ?= docker
PYTHON_VERSION ?= 3.12
TEST_IMAGE ?= arke-test:$(PYTHON_VERSION)
HARNESS ?= claude
LIVE_KEY = $(if $(filter claude,$(HARNESS)),ANTHROPIC_API_KEY,CODEX_API_KEY)

.DEFAULT_GOAL := check
.PHONY: check format check-format typing test test-python test-browser setup-browser test-container test-mocked test-live

check: check-format typing test

format:
	$(UV) run --locked ruff check --fix .
	$(UV) run --locked ruff format .

check-format:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

typing:
	$(UV) run --locked ty check --error-on-warning

test: test-python test-browser

test-python:
	$(UV) run --locked pytest

# under uv run, the bus it spawns (`python3`) is the project's Python, not whatever PATH has
test-browser:
	$(UV) run --locked $(NODE) tests/test_console.cjs

# Downloads test dependencies and 3D assets; does not install or start the service.
setup-browser:
	npm ci
	npx playwright install chromium
	python3 install-service.py --assets-only

# No live state, credentials, host mounts, or external network during the tests.
test-container:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) run --rm --network=none --shm-size=1g $(TEST_IMAGE)

# Actual CLIs and hooks, with scripted local model replies and no credentials.
test-mocked:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) build --build-arg TEST_IMAGE=$(TEST_IMAGE) -f Dockerfile.live -t arke-live-test .
	$(CONTAINER) run --rm --network=none --shm-size=1g arke-live-test claude --mock-model
	$(CONTAINER) run --rm --network=none --shm-size=1g arke-live-test codex --mock-model
	$(CONTAINER) run --rm --network=none --shm-size=1g --entrypoint python3 arke-live-test -m tests.tui_smoke claude
	$(CONTAINER) run --rm --network=none --shm-size=1g --entrypoint python3 arke-live-test -m tests.tui_smoke claude --native
	$(CONTAINER) run --rm --network=none --shm-size=1g --entrypoint python3 arke-live-test -m tests.tui_smoke codex

# Explicit opt-in: real model calls, separate from the credential-free CI suite.
test-live:
	$(CONTAINER) build --build-arg PYTHON_VERSION=$(PYTHON_VERSION) -f Dockerfile.test -t $(TEST_IMAGE) .
	$(CONTAINER) build --build-arg TEST_IMAGE=$(TEST_IMAGE) -f Dockerfile.live -t arke-live-test .
	$(CONTAINER) run --rm --shm-size=1g -e $(LIVE_KEY) -e ARKE_LIVE_MODEL arke-live-test $(HARNESS)
