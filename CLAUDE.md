# coding-agent-control-plane — engineering conventions

An issue-to-PR coding agent (Claude Agent SDK) driven by Temporal, calling MCP servers
through an MCP gateway under IdP-issued identity, plus the governance layer around it.
Public repository; copyright Stacktician Limited; Apache-2.0.

## Architecture invariants
These hold in every phase. Changing one needs a new ADR in `docs/adr/`.
- The agent has exactly one tool source, agentgateway. Built-in tools stay off except
  `Task`; nothing from the host (settings, hooks, other MCP servers, claude.ai connectors)
  is loaded (ADR 0002).
- The agent and the sandbox hold no credentials. The gateway injects the GitHub token; the
  control plane publishes the agent's work through the Git Data API (ADR 0003).
- Every tool call carries an IdP-issued JWT and a W3C `traceparent` (ADR 0002).
- One sandbox container per issue; the agent never touches the host filesystem.
- No agent change merges without a human decision, recorded in the workflow (ADR 0004).
- One durable workflow per issue. Workflow code is deterministic; side effects live in
  activities with explicit timeouts and retry policies (ADR 0001).
- Agent runs use the host's Claude subscription login. `ANTHROPIC_API_KEY` is never set in a
  shell profile, settings file or `.env`: it would silently switch runs to API billing, so
  the worker refuses to start when it is present (ADR 0006).

## Milestones
Milestones are named, never numbered, in this repo: Baseline (the whole system running end to
end before any part is hardened), Quality gate, Production MCP, Agent policy, Red team,
Harden, Durable, Evals, Demo. A comment that defers a known weakness names the milestone that
closes it ("the red-team milestone attacks this; harden closes it"). The README's roadmap is
the public definition of each.

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
- Every external call has a timeout. Activities are idempotent or say why not.
- Small modules named by function. No `utils.py`.
- Log with the standard `logging` module; no `print` outside `scripts/` and `labs/`.

## Secrets and config
- No secrets in code, config, prompts, tests, fixtures or logs.
- Configuration comes from environment variables, validated at startup with
  `pydantic-settings`. `.env*` files are gitignored; `.env.example` lists every key,
  with secrets left empty.
- Defaults in `config.py` are generic. Anything specific to the target repository (install
  and test commands, base branch) lives in `.env`, with example values in `.env.example`.
- Model credentials are never read from a committed file or baked into an image.

## Layout
- `src/coding_agent_control_plane/`: application code.
- `scripts/check_connections.py`: combined connection diagnostics; use
  `uv run python -B scripts/check_connections.py --send-test-traces` to also test new
  trace delivery. No reports, automatic repairs or container restarts.
- `scripts/render_architecture.sh`: renders the C4 views (Docker only).
- `deploy/`: Docker Compose and service configs. Compose mounts
  `deploy/agentgateway/gateway.yaml`.
- `docs/architecture/`: the C4 model (`workspace.dsl`) and the SVGs generated from it. Edit
  only the DSL; the render script and the pre-commit hook re-render the SVGs.
- `docs/adr/`: architecture decision records. A significant design decision gets an ADR
  before or with the change; a changed decision gets a new record that supersedes the old.
- `labs/`: standalone learning scripts, not imported by the application (none committed yet).
- `tests/`: mirrors `src/`.

## Publishing hygiene
- Commit identity: author name `Arun`, email the GitHub noreply address, set repo-locally.
- No AI attribution trailers (`Co-Authored-By`, "Generated with") in commits or PRs.
- The only addresses allowed in tracked files are the GitHub noreply address and
  `hello@stacktician.com`.
- Nothing from chats, transcripts or planning notes is committed. A useful chat answer is
  rewritten in the project's own voice (README, docs, ADR) or left out.
- Tracked files never name private files: `.dockerignore` is an allowlist, `.gitignore`
  describes categories, and private working files are ignored machine-wide.
- Pinned versions everywhere: Compose images by tag, render images by digest, GitHub
  actions by commit SHA. Dependabot proposes the updates.
- After any history rewrite and before the next push: `git for-each-ref` shows only
  `refs/heads/main`, and `git log --all --name-only` is free of removed files.

## Before you call a change done
`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
