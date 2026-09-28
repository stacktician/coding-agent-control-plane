"""IssueWorkflow paths on Temporal's time-skipping test server, with fake activities."""

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from coding_agent_control_plane.workflows.issue import IssueWorkflow
from coding_agent_control_plane.workflows.models import (
    AgentRequest,
    AgentResult,
    IssueRef,
    OpenedPull,
    OutcomeReport,
    PullRequestRequest,
    Review,
    ReviewAction,
    SandboxResult,
    SuiteResult,
)

TASK_QUEUE = "test-issues"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def fake_activities(
    calls: list[tuple[str, Any]],
    *,
    sandbox_ok: bool = True,
    agent_fails: bool = False,
    pull: OpenedPull | None = None,
) -> list[Callable[..., Any]]:
    @activity.defn(name="claim_issue")
    async def claim_issue(number: int) -> IssueRef:
        calls.append(("claim_issue", number))
        return IssueRef(number=number, title="A bug")

    @activity.defn(name="create_sandbox")
    async def create_sandbox(number: int) -> SandboxResult:
        calls.append(("create_sandbox", number))
        return SandboxResult(ok=sandbox_ok, output_tail="pip output")

    @activity.defn(name="run_agent")
    async def run_agent(request: AgentRequest) -> AgentResult:
        calls.append(("run_agent", request.issue.number))
        if agent_fails:
            raise ApplicationError("model unavailable", non_retryable=True)
        return AgentResult(
            ok=True, summary="Fixed", turns=4, langfuse_trace_id="t", api_equivalent_cost_usd=0.1
        )

    @activity.defn(name="run_tests")
    async def run_tests(number: int) -> SuiteResult:
        calls.append(("run_tests", number))
        return SuiteResult(passed=True, exit_code=0, output_tail="ok")

    @activity.defn(name="open_pull")
    async def open_pull(request: PullRequestRequest) -> OpenedPull | None:
        calls.append(("open_pull", request.issue.number))
        return pull

    @activity.defn(name="remove_sandbox")
    async def remove_sandbox(number: int) -> None:
        calls.append(("remove_sandbox", number))

    @activity.defn(name="apply_review")
    async def apply_review(action: ReviewAction) -> None:
        calls.append(("apply_review", action))

    @activity.defn(name="report_outcome")
    async def report_outcome(report: OutcomeReport) -> None:
        calls.append(("report_outcome", report.message))

    return [
        claim_issue,
        create_sandbox,
        run_agent,
        run_tests,
        open_pull,
        remove_sandbox,
        apply_review,
        report_outcome,
    ]


async def _run(
    env: WorkflowEnvironment,
    activities: list[Callable[..., Any]],
    review: Review | None = None,
) -> Any:
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[IssueWorkflow], activities=activities
    ):
        handle = await env.client.start_workflow(
            IssueWorkflow.run, 7, id=f"issue-7-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        if review is not None:
            await handle.signal(IssueWorkflow.review, review)
        return await handle.result()


async def test_approved_pull_is_merged(env: WorkflowEnvironment) -> None:
    calls: list[tuple[str, Any]] = []
    pull = OpenedPull(number=12, url="https://example/pull/12")
    outcome = await _run(env, fake_activities(calls, pull=pull), Review.APPROVE)

    assert outcome.decision == Review.APPROVE
    assert outcome.pull == 12
    assert ("apply_review", ReviewAction(pull=12, decision=Review.APPROVE)) in calls
    assert [name for name, _ in calls] == [
        "claim_issue",
        "create_sandbox",
        "run_agent",
        "run_tests",
        "open_pull",
        "remove_sandbox",
        "apply_review",
        "report_outcome",
    ]


async def test_rejected_pull_is_closed(env: WorkflowEnvironment) -> None:
    calls: list[tuple[str, Any]] = []
    pull = OpenedPull(number=13, url="https://example/pull/13")
    outcome = await _run(env, fake_activities(calls, pull=pull), Review.REJECT)

    assert outcome.decision == Review.REJECT
    assert ("apply_review", ReviewAction(pull=13, decision=Review.REJECT)) in calls
    assert "closed without merging" in outcome.message


async def test_no_changes_means_no_review_wait(env: WorkflowEnvironment) -> None:
    calls: list[tuple[str, Any]] = []
    outcome = await _run(env, fake_activities(calls, pull=None))

    assert outcome.pull is None
    assert "apply_review" not in [name for name, _ in calls]


async def test_agent_failure_cleans_up_and_reports(env: WorkflowEnvironment) -> None:
    calls: list[tuple[str, Any]] = []
    outcome = await _run(env, fake_activities(calls, agent_fails=True))

    names = [name for name, _ in calls]
    assert "run_tests" not in names
    assert names[-2:] == ["remove_sandbox", "report_outcome"]
    assert "agent failed" in outcome.message


async def test_failed_sandbox_skips_the_agent(env: WorkflowEnvironment) -> None:
    calls: list[tuple[str, Any]] = []
    outcome = await _run(env, fake_activities(calls, sandbox_ok=False))

    assert "run_agent" not in [name for name, _ in calls]
    assert "Sandbox setup failed" in outcome.message
