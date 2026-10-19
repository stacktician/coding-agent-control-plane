# Coding agent control plane

[![CI](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/ci.yml)
[![Architecture](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/architecture.yml/badge.svg)](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/architecture.yml)
[![Images](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/images.yml/badge.svg)](https://github.com/stacktician/coding-agent-control-plane/actions/workflows/images.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![Architecture: C4](https://img.shields.io/badge/architecture-C4%20model-08427b)](docs/architecture/README.md)
[![Decisions: ADRs](https://img.shields.io/badge/decisions-8%20ADRs-08427b)](docs/adr/README.md)

A governed coding agent. A labelled GitHub issue becomes a reviewed pull request, and every
tool call the agent makes crosses an authenticated MCP gateway.

![System context](docs/architecture/SystemContext.svg)

## Why

Tool-using agents are easy to build and hard to trust. Before a regulated firm lets one touch
its code, it needs answers to five questions: who is the agent when it calls a tool, what may
it call, where does its code run, what did it actually do, and who approved the result. This
project answers them in the architecture, not in the prompt.

![Agent tool path](docs/architecture/AgentToolPath.svg)

## Controls

Each control is mapped to the risks it addresses in the
[OWASP Top 10 for Agentic Applications (2026)](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).
This is the **baseline**, the project's first milestone: the whole system running end to end
before any part is hardened. Its weaknesses are deliberate and documented
([ADR 0007](docs/adr/0007-a-deliberately-naive-baseline-first.md)); the
[roadmap](#roadmap) says which milestone closes each one.

| Control | Threat it addresses | OWASP | Enforced in | Baseline status |
|---|---|---|---|---|
| One MCP gateway; the agent's built-in tools are off | The agent acting on the host or around the controls | ASI02, ASI05 | `agent/run.py`, agentgateway | In place |
| A JWT on every tool call | Unauthenticated tool use | ASI03 | agentgateway, Keycloak | In place; one identity shared by every run |
| No credentials in the agent or the sandbox | Token theft through prompt injection | ASI01, ASI03 | Gateway credential injection; publishing through the Git Data API | In place; the agent's GitHub route is read-only (server flag plus per-tool allow and deny rules); a gateway rule follows in Harden ([ADR 0003](docs/adr/0003-the-agent-holds-no-credentials.md)) |
| One sandbox container per issue | Untrusted repository code running on the host | ASI04, ASI05 | `sandbox/containers.py` | In place; `sandbox-mcp` holds the Docker socket, and paths are not confined |
| Turn limit, timeouts and heartbeats on the agent run | A runaway or stuck agent | ASI08, ASI10 | `max_turns`, Temporal activity timeouts | In place |
| A human approval gate | Unreviewed agent changes merging | ASI09, ASI10 | `IssueWorkflow` review signal | In place; who applied the label is not checked |
| Durable workflow history | Lost or silently repeated work; no audit trail | ASI08 | Temporal | In place; a retry can post a duplicate comment |
| End-to-end tracing | Agent behaviour nobody can inspect | ASI10 | Jaeger and Langfuse, `traceparent` through the gateway | In place |
| Localhost-only ports | Network exposure of the control plane itself | — | `compose.yaml` | In place |

## How it works

1. A Temporal Schedule polls the fork every 60 seconds for open `agent-ok` issues.
2. One durable workflow per issue claims it and creates an isolated sandbox container.
3. The agent runs with a single tool source, the gateway, using a token from Keycloak.
4. It reads the issue through `github_*` tools and changes and tests the code through
   `sandbox_*` tools. A test-writer subagent adds the tests.
5. The control plane runs the full test suite, collects the changes and publishes them as
   branch `agent/issue-<n>` with a pull request. The agent never holds a credential.
6. A human labels the pull request `agent-approve` or `agent-reject`.
7. The workflow merges or closes it and reports the outcome on the issue.

Every step, in order: [issue to pull request](docs/architecture/README.md#one-issue-end-to-end).

## Quickstart

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Claude Code signed in to a Claude
subscription, a fine-grained personal access token for a fork of the target repository
(`scripts/prepare_demo_repo.py` creates it and its seed issues; see
[scripts/README.md](scripts/README.md#preparing-the-demo-repository)), and a
Langfuse project.

```bash
cp .env.example .env    # then fill in every empty value
docker compose up -d --build
uv sync
uv run python -B scripts/check_connections.py --send-test-traces
uv run python -m coding_agent_control_plane.worker
```

Then label an issue on the fork `agent-ok`. The worker refuses to start if
`ANTHROPIC_API_KEY` is set, so agent runs never switch silently to API billing
([ADR 0006](docs/adr/0006-run-the-agent-on-the-host-claude-login.md)).

| Console | Address | Shows |
|---|---|---|
| Temporal | http://localhost:8233 | Each issue's workflow and its current stage |
| Jaeger | http://localhost:16686 | Service traces: workflows, activities, gateway, sandbox |
| Langfuse | your Langfuse project | Agent traces: model turns, tool calls, test runs |

## Standards and references

- **OWASP Top 10 for Agentic Applications (2026):** the controls table above maps each
  control to the risks it addresses.
- **Anthropic's [secure-deployment guidance](https://code.claude.com/docs/en/agent-sdk/secure-deployment)
  for the Agent SDK:** this design follows its proxy pattern. Credentials are injected by a
  component outside the agent's boundary; the agent runs with a restrictive permission mode,
  an explicit tool allow-list, no local setting sources and a turn limit. The container
  hardening from the same guide is on the roadmap.
- **[Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28/changelog):**
  the gateway speaks MCP to the agent and to both servers and translates between protocol
  revisions, so the stateless 2026-07-28 revision reaches each server only when it is ready.
- **OpenTelemetry:** the agent run's trace ID travels as a W3C `traceparent` header through
  the gateway, so the gateway's spans in Jaeger share the run's Langfuse trace ID. Agent spans
  carry Langfuse's attributes today; the OpenTelemetry GenAI names (`invoke_agent`,
  `execute_tool`, `mcp.*`) come next, adopted as they stand, since the conventions are still
  in development with no stabilisation date, so renames are expected and handled in one
  mapping module.
- **[C4 model](https://c4model.com):** the architecture is kept as code and rendered in CI
  ([ADR 0008](docs/adr/0008-architecture-as-code.md)).

## Roadmap

| Milestone | What changes |
|---|---|
| **Baseline** (this) | The whole path works end to end; every weakness is known and written down. |
| **Quality gate** | Coverage above 90 %, a planted-fault drill, three issues run concurrently with one approval, one rejection and one worker kill, and a written map of every trust boundary. |
| **Production MCP** | Typed sandbox tools with limits and structured errors; a third MCP server (Sentry); a token per MCP server; MCP conformance for the 2026-07-28 revision; an explicit `GITHUB_TOOLS` allow-list on the read-only GitHub route. |
| **Agent policy** | One organisation policy compiled to every coding agent's native format (Claude Code managed settings, Copilot, Cursor, Codex, Antigravity), with a paired measurement plan: throughput next to change-failure rate and review depth. |
| **Red team** | Each baseline weakness reproduced as a working attack with a measured success rate, including the 2026 incident classes: poisoned issues and READMEs, tool poisoning, token passthrough, confused deputy, an in-process extension that auto-approves, repo-controlled git config, a planted STDIO command, exfiltration by repository creation. |
| **Harden** | Policy per tool at the gateway (`mcpAuthorization` on tool name and token claims); identity per run with short-lived, repository-scoped tokens instead of one shared client and one PAT; audience-bound tokens and token exchange through the gateway; approval verified by actor; an append-only audit keyed to the verified subject; a sandbox without the Docker socket, non-root, dropped capabilities, no network except the gateway. |
| **Durable** | Idempotent activities, so a retry never repeats a side effect; review waits that last days; worker restarts and kills tested at three points, not assumed. |
| **Evals** | An eval harness as a CI gate on pinned SWE-bench Pro V2 tasks under the locked protocol and Terminal-Bench 4.0, with trajectories and per-task cost; resolve rate and cost with and without the governance layer; token budgets per run; OpenTelemetry GenAI names on agent spans. |
| **Demo** | A 15-minute walk-through: an issue becomes a PR, a poisoned issue is blocked with its audit trail, the worker is killed and resumes, the eval gate stops a regression. |

## Documentation

- [Architecture](docs/architecture/README.md): a C4 model kept as code; every diagram is
  generated from it and checked for drift in CI.
- [Architecture decision records](docs/adr/README.md): why the system looks the way it does,
  including the costs accepted.
- [Security policy](SECURITY.md) and [contributing](CONTRIBUTING.md).

## How this was built

Designed, reviewed and verified by a human; implemented with Claude Code working under the
project conventions in [`CLAUDE.md`](CLAUDE.md). Every change passes the same gate locally
and in CI: `ruff check`, `ruff format --check`, `mypy --strict` and `pytest`, with the Agent
SDK faked in tests so the suite spends no model usage. Dependencies, container images (by
digest) and actions are pinned; Dependabot proposes the updates; Trivy scans the sandbox image
on every change and weekly, and the results land in code scanning.

## Contact

Built at [Stacktician](https://stacktician.com): architecture, security and reliability for
tool-using AI agents. hello@stacktician.com

## License

Copyright 2026 Stacktician Limited. Licensed under the [Apache License, Version 2.0](LICENSE).
