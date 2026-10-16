# 0002. Every agent tool call crosses one MCP gateway

Status: Accepted

## Context

An agent's tools are its attack surface. Claude Code ships built-in file and shell tools that
act directly on the machine it runs on, and by default it also loads the user's and project's
settings, hooks, MCP servers and claude.ai connectors. None of that passes through a point
where identity can be checked, policy applied or calls traced.

## Decision

The agent gets exactly one tool source: agentgateway, as a single MCP server.

- Built-in tools are off except `Task` (for the test-writer subagent).
- `strict_mcp_config`, no setting sources and claude.ai connectors disabled: nothing from
  the host machine is loaded.
- Permission mode `dontAsk` with an allow-list of the gateway and `Task`: anything else is
  denied, never prompted.
- Every call carries a bearer token and a `traceparent`. The gateway validates the JWT
  (issuer, audience `agentgateway`, Keycloak JWKS), routes `sandbox_*` and `github_*` tools
  to their MCP servers and exports spans to Jaeger.

## Alternatives

- **The agent connects to each MCP server directly:** no single place for authentication,
  policy, credential injection or tracing.
- **Built-in tools with permission prompts:** an unattended run cannot answer prompts, and the
  tools act on the host rather than in the sandbox.

## Consequences

- One enforcement point for identity, policy, credentials and traces; MCP servers can be
  added or replaced behind it without changing the agent.
- This is the proxy pattern in Anthropic's secure-deployment guidance for the Agent SDK: the
  component that holds credentials and enforces an allow-list sits outside the agent's
  boundary, and the agent runs with a restrictive permission mode and an explicit tool list.
- The gateway also absorbs protocol change: it translates between MCP revisions, so the
  2026-07-28 revision (stateless, no sessions) reaches the servers behind it only when they
  are ready for it.
- The gateway is a single point of failure and adds a network hop to every tool call.
- In the baseline its policy is deliberately naive: one shared token admits any caller to every
  tool on both servers ([ADR 0007](0007-a-deliberately-naive-baseline-first.md)).
