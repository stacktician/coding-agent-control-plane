"""IssueWorkflow: one durable workflow per issue, ID ``issue-<n>``.

claim -> sandbox -> agent -> tests -> PR -> wait for review -> merge/close -> outcome.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from coding_agent_control_plane.agent.run import AgentActivities
    from coding_agent_control_plane.workflows.activities import IssueActivities
    from coding_agent_control_plane.workflows.models import (
        AgentRequest,
        IssueOutcome,
        IssueRef,
        OpenedPull,
        OutcomeReport,
        PullRequestRequest,
        Review,
        ReviewAction,
    )

GITHUB_RETRY = RetryPolicy(maximum_attempts=5)
AGENT_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10))


def workflow_id(issue: int) -> str:
    return f"issue-{issue}"


@workflow.defn
class IssueWorkflow:
    def __init__(self) -> None:
        self._decision: Review | None = None

    @workflow.signal
    def review(self, decision: Review) -> None:
        if self._decision is None:
            self._decision = decision

    @workflow.query
    def stage(self) -> str:
        return workflow.get_current_details()

    def _stage(self, text: str) -> None:
        workflow.set_current_details(text)

    @workflow.run
    async def run(self, number: int) -> IssueOutcome:
        self._stage("claiming the issue")
        issue = await workflow.execute_activity_method(
            IssueActivities.claim_issue,
            number,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=GITHUB_RETRY,
            summary="1 claim: label + comment",
        )

        self._stage("creating the sandbox")
        sandbox = await workflow.execute_activity_method(
            IssueActivities.create_sandbox,
            number,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=2),
            summary="2 sandbox: container, clone, install",
        )
        if not sandbox.ok:
            await self._remove_sandbox(number)
            return await self._finish(
                issue, None, f"Sandbox setup failed:\n```\n{sandbox.output_tail}\n```"
            )

        self._stage("agent running")
        try:
            agent = await workflow.execute_activity_method(
                AgentActivities.run_agent,
                AgentRequest(issue=issue),
                start_to_close_timeout=timedelta(minutes=60),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=AGENT_RETRY,
                summary="3 agent: Claude Agent SDK run",
            )
        except ActivityError as exc:
            await self._remove_sandbox(number)
            return await self._finish(issue, None, f"The agent failed: {exc.cause or exc}")

        self._stage("running the test suite")
        suite = await workflow.execute_activity_method(
            IssueActivities.run_tests,
            number,
            start_to_close_timeout=timedelta(minutes=20),
            retry_policy=RetryPolicy(maximum_attempts=2),
            summary="4 tests: full suite in the sandbox",
        )

        self._stage("opening the pull request")
        pull = await workflow.execute_activity_method(
            IssueActivities.open_pull,
            PullRequestRequest(issue=issue, agent=agent, suite=suite),
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=GITHUB_RETRY,
            summary="5 PR: branch + pull request",
        )
        await self._remove_sandbox(number)
        if pull is None:
            return await self._finish(issue, None, "The agent finished without changing any file.")

        self._stage(f"waiting for review on PR #{pull.number} (agent-approve / agent-reject)")
        await workflow.wait_condition(lambda: self._decision is not None)
        decision = self._decision
        assert decision is not None

        self._stage(f"applying review: {decision.value}")
        await workflow.execute_activity_method(
            IssueActivities.apply_review,
            ReviewAction(pull=pull.number, decision=decision),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=GITHUB_RETRY,
            summary=f"7 {'merge' if decision == Review.APPROVE else 'close'} PR #{pull.number}",
        )
        verb = "merged" if decision == Review.APPROVE else "closed without merging"
        return await self._finish(
            issue, pull, f"PR #{pull.number} was {verb} after human review.", decision
        )

    async def _remove_sandbox(self, number: int) -> None:
        await workflow.execute_activity_method(
            IssueActivities.remove_sandbox,
            number,
            start_to_close_timeout=timedelta(minutes=2),
            summary="remove the sandbox container",
        )

    async def _finish(
        self,
        issue: IssueRef,
        pull: OpenedPull | None,
        message: str,
        decision: Review | None = None,
    ) -> IssueOutcome:
        self._stage("reporting the outcome")
        await workflow.execute_activity_method(
            IssueActivities.report_outcome,
            OutcomeReport(issue=issue.number, message=message),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=GITHUB_RETRY,
            summary="8 outcome: comment on the issue",
        )
        self._stage(f"done: {message}")
        return IssueOutcome(
            issue=issue.number,
            pull=pull.number if pull else None,
            decision=decision,
            message=message,
        )
