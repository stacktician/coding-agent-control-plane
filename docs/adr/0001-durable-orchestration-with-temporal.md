# 0001. Durable per-issue orchestration with Temporal

Status: Accepted

## Context

Resolving one issue spans minutes to days: sandbox setup (up to 20 minutes), an agent run (up
to 60 minutes) and an open-ended wait for a human review. Each step can fail independently:
the GitHub API, Docker, the model. A restart of the worker must not lose progress, and a
retried step must not silently repeat work that already happened.

## Decision

- One Temporal workflow per issue, ID `issue-<n>`; a duplicate start is rejected.
- Workflow code is deterministic. Every side effect is an activity with an explicit timeout
  and retry policy: GitHub calls up to 5 attempts; the agent run up to 3 attempts with a
  2-minute heartbeat; sandbox setup and tests up to 2.
- The review decision arrives as a signal; the workflow waits for it without a timeout.
- The current stage is exposed as a query and in the Temporal Web UI.

## Alternatives

- **A task queue plus a database state machine** (for example Celery and Postgres):
  retries, timers and resumable state all become hand-written code.
- **One CI job per issue:** no durable wait for a human review that may take days.
- **A plain async service:** no durability; a crash loses the run.

## Consequences

- A crash or redeploy resumes each issue where it stopped.
- Retries and timeouts are declarative, and the workflow history is an audit trail of every
  step and decision.
- Temporal is one more service to run. Phase W uses the single-node dev server with SQLite,
  which is not highly available.
- Activities must be idempotent. In Phase W they are not all: a retried claim or outcome can
  post a duplicate comment (Phase 4 hardens this).
