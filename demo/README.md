# Demo

The console, the analytics page and the 3D page running on invented data, with no bus behind them.

```bash
make setup-assets                          # once: pinned dashboard libraries
make demo                                  # builds _site/ and serves it on http://localhost:8000/
```

Every push to `main` also publishes it to GitHub Pages (`.github/workflows/pages.yml`; the repository's Pages source must be set to "GitHub Actions").

The README's screenshots are taken from this demo. To retake them: `make demo-build`, then `node demo/screenshots.cjs` (uses the test suite's Playwright; `make setup-browser` installs it).

## How it works

`build.py` copies the pages from `web/` into a static site. It changes only their paths (absolute to relative, so the site also works under a subpath) and loads `fake-api.js` before everything else. If a page changes in a way a rewrite no longer matches, the build fails and says which one.

`fake-api.js` replaces `fetch`. It simulates a few agents working in three projects and answers every API request from that one simulation, so the agent table, messages, transcripts, event log, git history, process samples and machine history agree with each other. The simulation is seeded from the current 12-hour period: all three pages show the same history, and it keeps moving with the clock.

Messages you send, renames and colors work but live in the page's memory: they are gone on reload and the other pages don't see them.

## When the API changes

A request the fake has no route for gets a 404 and a console warning, and the `demoSite` test in `tests/test_console.cjs` fails. Add the route to `ROUTES` in `fake-api.js`.

The test does not compare the fake's fields with the real server's. If you rename or add a field the pages read, update the fake by hand (the shapes come from `public()`, `log_view()` and `read_events()` in `arke/server.py`, and from `arke/transcripts.py`).
