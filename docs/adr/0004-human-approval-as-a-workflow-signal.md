# 0004. Human approval as PR labels, delivered as a workflow signal

Status: Accepted

## Context

No agent change may merge without a human decision, and the reviewer works in GitHub, not in
the control plane's consoles. The decision must reach a workflow that may have been waiting
for days, and it must be recorded.

## Decision

- The pull request description ends with the review instruction: label `agent-approve` to
  merge or `agent-reject` to close.
- Each poll maps exactly one review label on an agent pull request to a `review` signal for
  workflow `issue-<n>`. No label, or both labels, is ignored.
- The workflow applies the decision: a squash merge or a close, then an outcome comment on the
  issue and removal of the agent labels.

## Alternatives

- **GitHub's review approvals:** richer, but a second integration path alongside the label
  polling the control plane already does.
- **Approve in the Temporal UI:** moves the reviewer out of GitHub, where the diff is.

## Consequences

- The human gate is explicit, and the decision is recorded in the workflow history.
- A decision takes effect within one poll interval (60 seconds).
- Anyone who can label pull requests on the fork can approve; the baseline does not check who
  applied the label.
