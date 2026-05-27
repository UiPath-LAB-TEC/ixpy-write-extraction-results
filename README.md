# ixpy-write-results

Agent for writing UiPath Document Understanding extraction results into UiPath Data Service.

## Logging
- Runtime logging is enabled in `main.py` with timestamped lifecycle events and durations.
- Set `IXPY_LOG_LEVEL` to control verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`), default is `INFO`.
- Optional: set `IXPY_RUN_ID` to tag all log lines for one run.
- The first log event is `module.loaded`. If that line never appears in published runs, the timeout is happening before agent code starts (typically dependency/bootstrap time).

## Project Layout
.
├── AGENTS.md - Guide for building new UiPath SDK agents in this repo
├── bindings.json - UiPath bindings configuration
├── entry-points.json - Entry-point schema for the coded agent
├── extraction_results.json - Sample extraction results payload for local runs
├── main.py - Agent implementation and Data Service writer
├── pyproject.toml - Python project metadata and dependencies
├── README.md - Project overview and usage notes
├── uipath.json - UiPath package configuration and entry-point wiring
└── uv.lock - Locked dependency versions for uv
