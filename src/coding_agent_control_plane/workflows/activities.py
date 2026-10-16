"""IssueWorkflow's side effects: GitHub writes, sandbox lifecycle, tests, PR.

Deliberately naive in the baseline (the durable milestone adds idempotency): a retried
claim or outcome activity can post a duplicate comment.
"""

import asyncio

from opentelemetry import context as otel_context
from opentelemetry.trace import format_trace_id
from temporalio import activity

from coding_agent_control_plane.config import Settings
from coding_agent_control_plane.github.client import GitHubClient, agent_branch
from coding_agent_control_plane.sandbox import containers
from coding_agent_control_plane.telemetry import langfuse_provider, langfuse_tracer
from coding_agent_control_plane.workflows.models import (
    IssueRef,
    OpenedPull,
    OutcomeReport,
    PullRequestRequest,
    Review,
    ReviewAction,
    SandboxResult,
    SuiteResult,
)

LABEL_READY = "agent-ok"
LABEL_CLAIMED = "agent-claimed"
TAIL_CHARS = 3_000


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:]


def session_id(issue: int) -> str:
    return f"issue-{issue}"


class IssueActivities:
    def __init__(self, settings: Settings, github: GitHubClient) -> None:
        self._settings = settings
        self._github = github

    @activity.defn
    async def claim_issue(self, number: int) -> IssueRef:
        issue = await self._github.get_issue(number)
        if LABEL_CLAIMED not in issue.labels:
            await self._github.add_labels(number, [LABEL_CLAIMED])
            await self._github.comment(
                number,
                f"Claimed by the coding agent. Temporal workflow: `issue-{number}`; "
                f"Langfuse session: `{session_id(number)}`.",
            )
        return IssueRef(number=issue.number, title=issue.title)

    @activity.defn
    async def create_sandbox(self, number: int) -> SandboxResult:
        result = await asyncio.to_thread(
            containers.create,
            number,
            self._settings.fork_clone_url,
            self._settings.target_install_command,
        )
        return SandboxResult(ok=result.exit_code == 0, output_tail=_tail(result.output))

    @activity.defn
    async def run_tests(self, number: int) -> SuiteResult:
        command = self._settings.target_test_command
        # A new root: Langfuse never receives the Temporal activity span (that goes to Jaeger).
        tracer = langfuse_tracer(self._settings)
        with tracer.start_as_current_span("tests", context=otel_context.Context()) as span:
            span.set_attributes(
                {
                    "langfuse.trace.name": f"issue-{number} tests",
                    "langfuse.session.id": session_id(number),
                    "langfuse.observation.type": "tool",
                    "langfuse.observation.input": command,
                }
            )
            result = await asyncio.to_thread(containers.exec_in, number, command, 900)
            span.set_attribute("langfuse.observation.output", _tail(result.output))
            if result.exit_code != 0:
                span.set_attribute("langfuse.observation.level", "ERROR")
            activity.logger.info(
                "tests exit %s, Langfuse trace %s",
                result.exit_code,
                format_trace_id(span.get_span_context().trace_id),
            )
        await asyncio.to_thread(langfuse_provider(self._settings).force_flush)
        return SuiteResult(
            passed=result.exit_code == 0,
            exit_code=result.exit_code,
            output_tail=_tail(result.output),
        )

    @activity.defn
    async def open_pull(self, request: PullRequestRequest) -> OpenedPull | None:
        number = request.issue.number
        changes = await asyncio.to_thread(containers.staged_changes, number)
        if not changes:
            return None
        parent = await asyncio.to_thread(containers.head_sha, number)
        branch = agent_branch(number)
        await self._github.publish_branch(
            branch, parent, f"Agent change for #{number}: {request.issue.title}", changes
        )
        verdict = "passed" if request.suite.passed else f"FAILED (exit {request.suite.exit_code})"
        body = "\n".join(
            [
                f"Closes #{number}",
                "",
                request.agent.summary,
                "",
                "---",
                f"Tests: `{self._settings.target_test_command}` {verdict}",
                f"Files: {', '.join(c.path for c in changes)}",
                f"Agent turns: {request.agent.turns}; "
                f"Langfuse trace `{request.agent.langfuse_trace_id}` "
                f"(session `{session_id(number)}`).",
                "",
                "Review: add the label `agent-approve` to merge or `agent-reject` to close.",
            ]
        )
        pull = await self._github.open_or_update_pull(
            branch, f"[agent] {request.issue.title} (#{number})", body
        )
        return OpenedPull(number=pull.number, url=pull.url)

    @activity.defn
    async def remove_sandbox(self, number: int) -> None:
        await asyncio.to_thread(containers.remove, number)

    @activity.defn
    async def apply_review(self, action: ReviewAction) -> None:
        if action.decision == Review.APPROVE:
            await self._github.merge_pull(action.pull)
        else:
            await self._github.close_pull(action.pull)

    @activity.defn
    async def report_outcome(self, report: OutcomeReport) -> None:
        await self._github.comment(report.issue, report.message)
        # Remove the agent's labels so the poller never picks this issue up again.
        for label in (LABEL_CLAIMED, LABEL_READY):
            await self._github.remove_label(report.issue, label)
