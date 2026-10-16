# 0006. Run the agent on the host, on the Claude subscription login

Status: Accepted (baseline)

## Context

The Claude Agent SDK drives the Claude Code runtime, which authenticates either with the
logged-in Claude subscription or with an API key. If `ANTHROPIC_API_KEY` is present it takes
precedence silently, switching every run to API billing.

## Decision

- The Temporal worker runs on the host, not in Compose, so the runtime uses the host's Claude
  login. Every other service runs in Compose.
- The worker refuses to start, and the agent activity refuses to run, if `ANTHROPIC_API_KEY`
  is set.
- The agent's working directory is an empty per-issue folder. The repository exists only in
  the sandbox, reached through MCP ([ADR 0002](0002-every-agent-tool-call-crosses-one-mcp-gateway.md)).

## Alternatives

- **A containerised worker with an API key:** the production path, at API prices.
- **Claude Code inside each sandbox container:** the agent's built-in tools then act on the
  repository directly, with no gateway between the agent and its tools.

## Consequences

- Development runs cost no API spend, and a misconfigured environment fails fast instead of
  billing silently.
- The worker is not containerised, so the baseline deployment differs from production.
- The host's Claude login is a personal credential, not a production identity, and
  subscription limits cap throughput. Production needs a containerised worker with its own
  credentials.
- This route is development-only. The Agent SDK's terms do not allow third parties to offer
  claude.ai login for products built on it, so a pilot for anyone else runs on an API key.
- Since 7 October 2026 the Max plans carry a monthly API credit, spent through a Console key
  from an organisation linked to the plan. The red-team and eval harnesses use that credit;
  the key is set in the harness process only, and the worker's refusal to start with
  `ANTHROPIC_API_KEY` present stands.
