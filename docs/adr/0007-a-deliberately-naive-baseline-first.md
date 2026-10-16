# 0007. Ship a deliberately naive security baseline first, then attack it

Status: Accepted (baseline)

## Context

A security control is only credible if it can be shown to stop an attack that succeeds
without it. That needs a working baseline whose weaknesses are known, reproducible and
written down.

## Decision

The baseline is the project's first milestone: the whole system running end to
end before any part is hardened. It is functional but deliberately naive, and says so in the
code:

- One shared Keycloak client identity for every run.
- One gateway token admits any caller to every tool on both MCP servers.
- One GitHub token for all GitHub access, by both the worker and the gateway.
- `sandbox-mcp` can address any issue's container, does not confine paths to the workspace,
  and runs commands as root in the container.
- `sandbox-mcp` mounts the Docker socket, which is root-equivalent on the host.
- A retried claim or outcome activity can post a duplicate comment.

The red-team milestone attacks these weaknesses; the harden and durable milestones fix them,
each fix measured against the attack that motivated it.

## Consequences

- Every hardening step is justified by a demonstrated, reproducible attack rather than a
  checklist.
- The baseline must not process untrusted repositories or issues.
- Every published port is bound to `127.0.0.1`, so nothing is reachable from the network.
