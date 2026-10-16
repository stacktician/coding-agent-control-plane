"""The agent activity: one Claude Agent SDK run per issue.

The SDK drives the local Claude Code runtime on the host's Claude login. All
tools reach the agent over MCP through agentgateway; the only built-in tool
left on is Task, which launches the test-writer subagent.
"""

import asyncio
import contextlib
import os
from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ResultMessage, query
from temporalio import activity

from coding_agent_control_plane.agent.tracing import AgentTrace
from coding_agent_control_plane.config import Settings
from coding_agent_control_plane.identity import fetch_gateway_token
from coding_agent_control_plane.telemetry import langfuse_provider, langfuse_tracer
from coding_agent_control_plane.workflows.models import AgentRequest, AgentResult

GATEWAY = "gateway"
SANDBOX_TOOLS = ["run_command", "read_file", "write_file", "edit_file", "list_files"]
# The agent may only read GitHub: the control plane publishes its work (ADR 0003). The
# server runs read-only as well (GITHUB_READ_ONLY in compose.yaml); this is the second lock.
GITHUB_READ_PATTERNS = ["get_*", "list_*", "search_*", "*_read"]  # issue_read, pull_request_read
GITHUB_WRITE_PREFIXES = [
    "create_",
    "update_",
    "add_",
    "push_",
    "merge_",
    "delete_",
    "fork_",
    "dismiss_",
    "request_",
    "assign_",
    "submit_",
    "remove_",
    "set_",
    "mark_",
    "run_",
    "cancel_",
    "rerun_",
]
HEARTBEAT_EVERY_S = 20

SYSTEM_PROMPT = """\
You are a coding agent resolving one GitHub issue in the repository {repo}.
You act only through MCP tools:
- sandbox_* tools operate on a clean checkout of the repository inside an isolated
  container. Always pass issue={issue}. Paths are relative to the repository root.
- github_* tools read GitHub (owner "{owner}", repo "{name}"). Use them to read the issue
  and its comments. Never create branches, commits, pull requests, comments or labels:
  the control plane publishes your work after you finish.

Process:
1. Read issue #{issue} and its comments with the github tools.
2. Explore the code with sandbox_list_files, sandbox_read_file and sandbox_run_command
   (grep, git log).
3. Make the smallest change that resolves the issue. Prefer sandbox_edit_file for edits.
4. Delegate the tests to the test-writer subagent (Task tool): give it the files you
   changed and the behaviour to cover.
5. Run the full test suite with sandbox_run_command: `{test_command}`. Iterate until it
   passes.
6. Do not commit; leave the changes in the working tree. Finish with a short pull request
   description in Markdown: what changed, why, and the test result."""

TEST_WRITER_PROMPT = """\
You write or update pytest tests (and doctests where the module already uses them) for a
change another agent made in the sandbox. Use only the sandbox_* tools and always pass the
issue number you are given. Follow the repository's existing test layout and style. Run
the tests you touched with sandbox_run_command and report which tests you added."""


def ensure_subscription_auth() -> None:
    """Refuse to run if an API key would silently take over from the Claude login."""
    if "ANTHROPIC_API_KEY" in os.environ:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set; it would bill agent runs to the API instead of the "
            "Claude subscription login. Unset it before starting the worker."
        )


def build_prompt(request: AgentRequest, attempt: int) -> str:
    prompt = f"Resolve issue #{request.issue.number}: {request.issue.title}"
    if attempt > 1:
        prompt += (
            f"\n\nThis is attempt {attempt}: an earlier attempt was interrupted. The sandbox "
            "may already contain partial changes; check `git status` and `git diff` first."
        )
    return prompt


def build_options(
    settings: Settings, issue: int, gateway_token: str, traceparent: str, cwd: Path
) -> ClaudeAgentOptions:
    owner, name = settings.github_fork.split("/")
    return ClaudeAgentOptions(
        model=settings.agent_model,
        system_prompt=SYSTEM_PROMPT.format(
            repo=settings.github_fork,
            owner=owner,
            name=name,
            issue=issue,
            test_command=settings.target_test_command,
        ),
        # Built-in file and shell tools off: every file or command crosses the gateway.
        tools=["Task"],
        mcp_servers={
            GATEWAY: {
                "type": "http",
                "url": settings.gateway_mcp_url,
                "headers": {"Authorization": f"Bearer {gateway_token}", "traceparent": traceparent},
            }
        },
        # Only the gateway: no .mcp.json, user or plugin servers, and no claude.ai
        # connectors (Gmail, Drive, Calendar), which the Claude login loads otherwise.
        strict_mcp_config=True,
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        # No user or project settings, hooks or CLAUDE.md from this machine.
        setting_sources=[],
        # Per-tool allow rules: every sandbox tool, GitHub reads only, and Task. With
        # dontAsk, any other tool call is denied rather than prompted.
        allowed_tools=[
            f"mcp__{GATEWAY}__sandbox_*",
            *[f"mcp__{GATEWAY}__github_{pattern}" for pattern in GITHUB_READ_PATTERNS],
            "Task",
        ],
        # Deny rules hold in every permission mode, so GitHub writes stay blocked even if
        # the mode or the allow list changes later.
        disallowed_tools=[f"mcp__{GATEWAY}__github_{prefix}*" for prefix in GITHUB_WRITE_PREFIXES],
        permission_mode="dontAsk",  # anything not allowed above is denied, never prompted
        agents={
            "test-writer": AgentDefinition(
                description="Writes or updates pytest tests for a change in the sandbox.",
                prompt=TEST_WRITER_PROMPT,
                tools=[f"mcp__{GATEWAY}__sandbox_{tool}" for tool in SANDBOX_TOOLS],
                # Foreground: the main agent waits for the tests instead of ending its
                # turn while the subagent is still editing the sandbox.
                background=False,
            )
        },
        max_turns=settings.agent_max_turns,
        include_partial_messages=True,  # final per-turn token counts; see agent/tracing.py
        cwd=cwd,
    )


class AgentActivities:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @activity.defn
    async def run_agent(self, request: AgentRequest) -> AgentResult:
        ensure_subscription_auth()
        settings = self._settings
        issue = request.issue.number
        attempt = activity.info().attempt
        prompt = build_prompt(request, attempt)
        recorder = AgentTrace(
            langfuse_tracer(settings), issue, prompt, settings.agent_model, attempt
        )
        activity.logger.info("agent attempt %s, Langfuse trace %s", attempt, recorder.trace_id)
        cwd = Path(".data") / "agent-cwd" / f"issue-{issue}"
        cwd.mkdir(parents=True, exist_ok=True)
        token = await fetch_gateway_token(settings)
        options = build_options(settings, issue, token, recorder.traceparent, cwd.resolve())

        heartbeat = asyncio.create_task(_heartbeat())
        result: ResultMessage | None = None
        error: BaseException | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                recorder.on_message(message)
                if isinstance(message, ResultMessage):
                    result = message
        except BaseException as exc:
            error = exc
            raise
        finally:
            heartbeat.cancel()
            recorder.close(error)
            await asyncio.to_thread(langfuse_provider(settings).force_flush)

        if result is None:
            raise RuntimeError("the agent run ended without a result message")
        return AgentResult(
            ok=not result.is_error,
            summary=result.result or "(the agent returned no summary)",
            turns=result.num_turns,
            langfuse_trace_id=recorder.trace_id,
            api_equivalent_cost_usd=result.total_cost_usd,
        )


async def _heartbeat() -> None:
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            activity.heartbeat()
            await asyncio.sleep(HEARTBEAT_EVERY_S)
