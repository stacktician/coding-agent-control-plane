# coding-agent-control-plane — engineering conventions

An issue-to-PR coding agent (Claude Agent SDK) driven by Temporal, calling MCP servers
through an MCP gateway under IdP-issued identity, plus the governance layer around it.

## Stack
- Python 3.12+, managed with `uv` (lockfile committed; `uv sync`, `uv run`, `uv build`).
- Lint and format: `ruff check` and `ruff format`. No other formatter.
- Types: `mypy --strict` must pass. No untyped `def`; `Any` only at third-party boundaries,
  with a comment saying why.
- Tests: `pytest`. Workflow tests use Temporal's time-skipping test environment; the Agent
  SDK run is faked in tests so the suite spends no model usage.
- Data models: pydantic v2 for anything crossing a process, network or workflow boundary.

## Service and container names
- Compose service keys and container names: `sandbox-mcp`, `github-mcp`, `agentgateway`,
  `keycloak`, `jaeger` and `temporal`. The gateway reaches the sandbox server at
  `http://sandbox-mcp:8083/mcp`; MCP tool prefixes are `sandbox_` and `github_`.
- Per-issue sandboxes are `cacp-sandbox-issue-<n>`, `<n>` being the GitHub issue number.
- The sandbox server's Python package is `mcp_sandbox`; its Dockerfile is `deploy/mcp-sandbox`.

## Code
- Async-first. No blocking I/O on the event loop; wrap unavoidable blocking calls in
  `asyncio.to_thread`.
- Temporal workflows stay deterministic: no I/O, clock, randomness or env reads inside
  workflow code; all side effects live in activities.
- Every external call has a timeout. Activities are idempotent or say why not.
- Small modules named by function. No `utils.py`.
- Log with the standard `logging` module; no `print` outside `labs/`.

## Secrets and config
- No secrets in code, config, prompts, tests, fixtures or logs.
- Configuration comes from environment variables, validated at startup with
  `pydantic-settings`. `.env*` files are gitignored; `.env.example` lists every key,
  with secrets left empty.
- Model credentials are never read from a committed file or baked into an image.

## Layout
- `src/coding_agent_control_plane/`: application code.
- `scripts/check_connections.py`: combined connection diagnostics; use
  `uv run python -B scripts/check_connections.py --send-test-traces` to also test new
  trace delivery. No reports, automatic repairs or container restarts.
- `deploy/`: Docker Compose and service configs.
- `labs/`: standalone learning scripts, not imported by the application.
- `docs/`: architecture and system map.
- `tests/`: mirrors `src/`.

## Before you call a change done
`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
