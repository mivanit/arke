"""Isolated pytest regressions for arke; module paths are configured in pyproject.toml.

Patch a name where the code under test looks it up: server.py imports from transcripts.py, wake.py and procs.py by
name, so the Bus's own calls go through server.X while calls made inside wake.py go through wake.X (patch both when
both run, e.g. prompt_box and tmux_run during a wake-up)."""
