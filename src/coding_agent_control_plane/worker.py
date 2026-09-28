"""The Temporal worker: runs the poller, IssueWorkflow and every activity.

    uv run python -m coding_agent_control_plane.worker

Runs on the host, not in Compose, because the agent activity drives the local
Claude Code runtime on this machine's Claude login.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from coding_agent_control_plane.agent.run import AgentActivities, ensure_subscription_auth
from coding_agent_control_plane.config import get_settings
from coding_agent_control_plane.github.client import GitHubClient
from coding_agent_control_plane.ingress.poller import (
    IngressActivities,
    PollWorkflow,
    ensure_schedule,
)
from coding_agent_control_plane.telemetry import configure_jaeger
from coding_agent_control_plane.workflows.activities import IssueActivities
from coding_agent_control_plane.workflows.issue import IssueWorkflow

logger = logging.getLogger(__name__)


async def run() -> None:
    ensure_subscription_auth()
    settings = get_settings()
    configure_jaeger(settings)
    client = await Client.connect(
        settings.temporal_address,
        data_converter=pydantic_data_converter,
        plugins=[OpenTelemetryPlugin()],
    )
    await ensure_schedule(client, settings)
    github = GitHubClient(settings)
    issues = IssueActivities(settings, github)
    agent = AgentActivities(settings)
    ingress = IngressActivities(client, settings, github)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[IssueWorkflow, PollWorkflow],
        activities=[
            ingress.poll_fork,
            issues.claim_issue,
            issues.create_sandbox,
            issues.run_tests,
            issues.open_pull,
            issues.remove_sandbox,
            issues.apply_review,
            issues.report_outcome,
            agent.run_agent,
        ],
        max_concurrent_activities=10,
    )
    logger.info(
        "worker polling task queue %r; fork %s every %ss",
        settings.temporal_task_queue,
        settings.github_fork,
        settings.poll_interval_s,
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
