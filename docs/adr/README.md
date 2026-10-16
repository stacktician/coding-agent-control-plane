# Architecture decision records

Each record captures one significant decision: its context, the decision, the alternatives
and the consequences, including the costs accepted. Records are immutable once accepted; a
changed decision gets a new record that supersedes the old one.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-durable-orchestration-with-temporal.md) | Durable per-issue orchestration with Temporal | Accepted |
| [0002](0002-every-agent-tool-call-crosses-one-mcp-gateway.md) | Every agent tool call crosses one MCP gateway | Accepted |
| [0003](0003-the-agent-holds-no-credentials.md) | The agent holds no credentials; the control plane publishes its work | Accepted |
| [0004](0004-human-approval-as-a-workflow-signal.md) | Human approval as PR labels, delivered as a workflow signal | Accepted |
| [0005](0005-poll-github-on-a-temporal-schedule.md) | Poll GitHub on a Temporal Schedule instead of webhooks | Accepted |
| [0006](0006-run-the-agent-on-the-host-claude-login.md) | Run the agent on the host, on the Claude subscription login | Accepted (Phase W) |
| [0007](0007-a-deliberately-naive-baseline-first.md) | Ship a deliberately naive security baseline first, then attack it | Accepted (Phase W) |
| [0008](0008-architecture-as-code.md) | Architecture as code: a C4 model with generated, drift-checked views | Accepted |

The architecture these decisions produce is in [`docs/architecture`](../architecture/README.md).
