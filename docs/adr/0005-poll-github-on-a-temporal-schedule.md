# 0005. Poll GitHub on a Temporal Schedule instead of webhooks

Status: Accepted

## Context

The baseline runs on a workstation behind NAT. GitHub webhooks need a publicly reachable endpoint:
an exposed port or a tunnel, plus signature verification and replay handling.

## Decision

- A Temporal Schedule, `poll-fork`, runs `PollWorkflow` every 60 seconds and skips a run if
  the previous one is still going.
- Its single activity lists open `agent-ok` issues and starts `IssueWorkflow` for each, with
  duplicate workflow IDs rejected, then signals review labels found on agent pull requests.

## Alternatives

- **Webhooks through a tunnel:** an inbound path into the workstation and another moving part.
- **A GitHub Actions trigger:** puts the control plane's entry point inside CI.

## Consequences

- No inbound network exposure. Polling is idempotent and self-healing: issues labelled while
  the worker was down are picked up on the next poll.
- Up to 60 seconds of latency, and GitHub API calls on every poll whether or not anything
  changed.
- Each poll reads one page of 100 issues and 100 pull requests; there is no pagination.
