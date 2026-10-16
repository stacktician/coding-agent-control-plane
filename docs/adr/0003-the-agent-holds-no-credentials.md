# 0003. The agent holds no credentials; the control plane publishes its work

Status: Accepted

## Context

Issue text is untrusted input. A prompt injection could direct an agent to exfiltrate any
credential within its reach or push arbitrary code. Whatever the agent can touch must be
assumed reachable by whoever wrote the issue.

## Decision

- The agent reads GitHub only through `github-mcp`. The gateway injects the GitHub token on
  that route from a Docker secret; the agent never sees it.
- The sandbox clones the fork anonymously and holds no credentials.
- The agent does not commit. When it finishes, the control plane stages the sandbox's working
  tree, reads the changed files and publishes them from the worker through the Git Data API
  (blobs, tree, commit, branch `agent/issue-<n>`), then opens the pull request.

## Alternatives

- **A token inside the sandbox and `git push` from the agent:** the credential sits within
  reach of injected instructions and every command the agent runs.
- **The agent opens the pull request itself through `github_*` tools:** writes depend on the
  agent following its prompt rather than on architecture.

## Consequences

- A compromised agent run cannot read or leak the GitHub token, and nothing it does is
  published without passing through the control plane and a human review.
- Re-publishing replaces the branch, so a retried publish is idempotent.
- Gap in Phase W: the gateway exposes the full `repos`, `issues` and `pull_requests`
  toolsets, write tools included, and the agent is only *told* not to use them. Restricting
  the `github_*` route to read-only tools is the policy to add.
