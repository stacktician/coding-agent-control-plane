# Connection diagnostics

Run from the repository root in Windows Git Bash.

## Combined check: connections plus new test traces

```bash
uv run python -B scripts/check_connections.py --send-test-traces
```

This checks configuration, Docker status, authentication, an actual GitHub read through the
gateway, labels, and Temporal. It sends one new `stack-check` trace to Langfuse and one to
Jaeger using the application's existing telemetry code, then verifies those exact IDs arrived.
A previous trace with the same name cannot make this test pass. The checker waits up to
60 seconds per destination; use `--trace-timeout 120` to allow more time.

## Inspect only

```bash
uv run python -B scripts/check_connections.py
```

Without `--send-test-traces`, it sends no new test traces. It looks for saved `stack-check`
traces from the last 24 hours. `--lookback-hours` changes that window.
`--jaeger-trace-id` and `--langfuse-trace-id` select particular saved traces. These options
cannot be combined with `--send-test-traces`.

Jaeger searches use its supported `/api/v3/traces` API. A failed lookup is reported separately
from a successful search with no matching saved trace. A saved trace proves earlier delivery;
only `--send-test-traces` tests new delivery during this run.

`--issue-number N` selects the existing GitHub issue to read through the gateway (default: 1).
`--check-seed-issues` also verifies the four original training issues remain open and unlabeled.
`--help` displays all options without contacting services.

## Service and container names

The six Compose services and explicit container names are `sandbox-mcp`,
`github-mcp`, `agentgateway`, `keycloak`, `jaeger` and `temporal`.
The checker compares Compose service labels against these names. MCP tool prefixes are
`sandbox_` and `github_`; per-issue sandboxes are `cacp-sandbox-issue-<n>`. The checker
verifies the running services and never repairs or recreates them.

Compose mounts `deploy/agentgateway/gateway.yaml`, whose sandbox backend is
`http://sandbox-mcp:8083/mcp`.

## Configuration and boundaries

The existing project settings load `.env`. `GITHUB_FORK` identifies the GitHub repository;
the Langfuse keys identify its project. No extra credential or project settings are needed.

Results go only to the console. Exit code 0 means all selected checks passed; 1 means a check
failed, and 2 means invalid command-line options. Failures are diagnosed, never repaired.
There are no report files, container restarts, GitHub writes, sandbox commands, model calls,
or Temporal workflow starts. In send mode, the two diagnostic traces are intentional writes
to the tracing services. Normal requests can also generate service access logs and telemetry.

Connection diagnostics live in `scripts/check_connections.py`; `scripts/trace_probe.py`
contains saved-trace lookup, trace sending and receipt verification. They reuse application
settings and telemetry exporters from `src/`; there is no separate application stack-check
command. The diagnostic trace name `stack-check` and tracer label `stack_check` are retained
so existing saved traces remain recognizable. These labels are not Python module imports.

A passing connection check does not prove GitHub write permissions, sandbox execution,
issue-to-PR behavior, or workflow recovery. Those belong to later functional testing.

## Resetting the training fork

`scripts/reset_training_fork.py` undoes the agent's work on the fork, or prepares a fresh
one, so the same seed issues can be run again. It reads `GITHUB_FORK` and `GITHUB_PAT` from
`.env`, never prints the token, and changes nothing without `--apply`.

```bash
uv run python -B scripts/reset_training_fork.py --reset            # dry run: shows the plan
uv run python -B scripts/reset_training_fork.py --reset --apply    # undo the agent's work in place
uv run python -B scripts/reset_training_fork.py --recreate --apply # prepare a freshly made fork
```

`--reset` moves the default branch back to the newest commit that is not an agent commit
(messages starting with `Agent change for #` or `[agent]`), deletes `agent/issue-*` branches,
closes open agent pull requests, reopens the seed issues and strips the agent labels.
`--recreate` enables issues, creates the four labels, renames the default branch and
recreates the seed issues from the upstream repository in order (default `289 299 282 301`,
so they become #1–#4). Enabling issues and renaming the branch need the token's
Administration permission; without it the script prints the manual step instead.
