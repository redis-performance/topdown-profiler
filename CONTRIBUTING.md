# Contributing

We treat this repo as "Open Source" within Redis: anyone who clears the bar below is welcome to contribute.

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

```
<type>/<short-description>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`

Example: `feat/add-pipeline-mode`

## Coding standards

- Keep changes focused; one logical change per PR.
- Follow the conventions already present in the codebase (formatting, naming, error handling).
- No dead code, no commented-out blocks.

## Submitting changes

1. Fork or create a branch from `main`.
2. Make your changes with clear, atomic commits.
3. Open a pull request against `main` with a descriptive title and summary.
4. Address review comments promptly; force-push to the same branch to update.

## Testing

- All new behaviour must be covered by tests.
- Existing tests must pass: run the test suite locally before opening a PR.
- Coverage should not decrease.

Run the full test suite:

```bash
make test
# equivalent: poetry run pytest tests/ -v
```

Run linting:

```bash
make lint
# equivalent: poetry run ruff check topdown/ tests/
```

CI runs the test matrix across Python 3.10, 3.11, 3.12, and 3.13. Both `test` and `lint` jobs must be green before a PR can merge.

## Review process

- At least one maintainer approval is required before merge.
- CI must be green.
- Maintainers may request changes or close PRs that don't meet the bar — this is normal and not personal.
