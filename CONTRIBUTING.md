# Contributing

This is a single-maintainer project under active development. Issues and pull requests are
welcome; small, focused changes are easiest to review.

## Setup

Docker, [uv](https://docs.astral.sh/uv/) and a Claude subscription login. Then:

```bash
uv sync
git config core.hooksPath .githooks
```

## Before opening a pull request

Run the gate; CI runs the same commands:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

- A change to a design decision needs an [ADR](docs/adr/README.md): a new record that
  supersedes the old one.
- A change to the architecture is made in `docs/architecture/workspace.dsl`; the SVGs are
  generated, never edited by hand.
- Workflow code stays deterministic; side effects live in activities. See
  [`CLAUDE.md`](CLAUDE.md) for the full conventions.
