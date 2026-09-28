"""Ingress: a Temporal Schedule polls the fork (no webhook, no tunnel).

Every poll starts one IssueWorkflow per open `agent-ok` issue, idempotent on
the workflow ID `issue-<n>`, and signals review decisions found as labels on
the agent's pull requests.
"""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
    )
    from temporalio.client import ScheduleOverlapPolicy as Overlap
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.service import RPCError

    from coding_agent_control_plane.config import Settings
    from coding_agent_control_plane.github.client import GitHubClient
    from coding_agent_control_plane.workflows.activities import LABEL_CLAIMED, LABEL_READY
    from coding_agent_control_plane.workflows.issue import IssueWorkflow, workflow_id
    from coding_agent_control_plane.workflows.models import PollSummary, Review

SCHEDULE_ID = "poll-fork"
REVIEW_LABELS = {"agent-approve": Review.APPROVE, "agent-reject": Review.REJECT}


class IngressActivities:
    def __init__(self, client: Client, settings: Settings, github: GitHubClient) -> None:
        self._client = client
        self._settings = settings
        self._github = github

    @activity.defn
    async def poll_fork(self) -> PollSummary:
        started = []
        for issue in await self._github.list_open_issues(LABEL_READY):
            if LABEL_CLAIMED in issue.labels:
                continue
            try:
                await self._client.start_workflow(
                    IssueWorkflow.run,
                    issue.number,
                    id=workflow_id(issue.number),
                    task_queue=self._settings.temporal_task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
                started.append(issue.number)
            except WorkflowAlreadyStartedError:
                pass

        signalled = []
        for pull in await self._github.list_agent_pulls():
            decisions = [REVIEW_LABELS[label] for label in pull.labels if label in REVIEW_LABELS]
            if len(decisions) != 1:  # none yet, or contradictory labels
                continue
            handle = self._client.get_workflow_handle(workflow_id(pull.issue))
            try:
                await handle.signal(IssueWorkflow.review, decisions[0])
                signalled.append(pull.issue)
            except RPCError:
                pass  # the workflow has already finished
        return PollSummary(started=started, signalled=signalled)


@workflow.defn
class PollWorkflow:
    @workflow.run
    async def run(self) -> PollSummary:
        return await workflow.execute_activity_method(
            IngressActivities.poll_fork,
            start_to_close_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=1),
            summary="poll the fork for agent-ok issues and review labels",
        )


async def ensure_schedule(client: Client, settings: Settings) -> None:
    try:
        await client.create_schedule(
            SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    PollWorkflow.run, id=SCHEDULE_ID, task_queue=settings.temporal_task_queue
                ),
                spec=ScheduleSpec(
                    intervals=[
                        ScheduleIntervalSpec(every=timedelta(seconds=settings.poll_interval_s))
                    ]
                ),
                policy=SchedulePolicy(overlap=Overlap.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass
