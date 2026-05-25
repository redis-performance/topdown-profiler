# Agent guidelines

Instructions for AI coding agents (Claude Code, Copilot, Cursor, etc.) working in this repo.

## Project overview

`topdown-profiler` is a Python tool and MCP (Model Context Protocol) server that collects, stores, and analyzes Intel Top-Down Microarchitecture Analysis (TMA) data for profiling CPU-bound workloads. The tool supports multiple storage backends (SQLite and PostgreSQL) and provides both CLI commands and an MCP server interface for analyzing CPU bottlenecks in Redis and other performance-critical services.

## Local setup

```bash
git clone https://github.com/redis-performance/topdown-profiler.git
cd topdown-profiler
poetry install
```

Python 3.10 or newer is required. [Poetry](https://python-poetry.org/docs/#installation) manages the virtual environment and dependencies — install it first if you don't have it:

```bash
pipx install poetry   # recommended
# or: curl -sSL https://install.python-poetry.org | python3 -
```

To add the optional PostgreSQL backend:

```bash
poetry install -E postgresql
```

## Branch naming

Same as human contributors: `<type>/<short-description>` (e.g. `fix/off-by-one-in-pipeline`).

## Coding standards

- Match the style already in the file you are editing.
- Prefer clear, minimal changes over large refactors unless explicitly asked.
- Do not add comments that describe *what* the code does — only add comments when the *why* is non-obvious.
- Do not introduce new dependencies without checking with the maintainer.

## Running tests

All new behaviour must be covered by tests. Existing tests must pass before declaring a task complete.

```bash
make test
# equivalent: poetry run pytest tests/ -v
```

To also run linting:

```bash
make lint
# equivalent: poetry run ruff check topdown/ tests/
```

Coverage should not decrease. CI runs the test matrix across Python 3.10, 3.11, 3.12, and 3.13.

## How to submit changes

1. Create a branch: `git checkout -b <type>/<description>`.
2. Commit with a clear message focused on *why*, not *what*.
3. Open a pull request against `main`.
4. Do **not** push directly to `main`.

## What to avoid

- Do not reformat files unrelated to your change.
- Do not remove error handling or tests.
- Do not commit secrets, credentials, or large binary files.
- Do not amend published commits.
