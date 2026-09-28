"""The agent's isolation settings: regression tests, no model calls."""

from pathlib import Path

import pytest

from coding_agent_control_plane.agent.run import (
    GATEWAY,
    build_options,
    build_prompt,
    ensure_subscription_auth,
)
from coding_agent_control_plane.config import Settings
from coding_agent_control_plane.workflows.models import AgentRequest, IssueRef


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init kwarg
        github_fork="someone/boltons",
        github_pat="ghp_test",
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",  # noqa: S106
        kc_agent_worker_secret="secret",  # noqa: S106
    )


def test_only_the_gateway_and_task_are_reachable(settings: Settings) -> None:
    options = build_options(settings, 1, "jwt", "00-" + "a" * 32 + "-" + "b" * 16 + "-01", Path())

    assert options.tools == ["Task"]
    assert list(options.mcp_servers) == [GATEWAY]  # type: ignore[arg-type]
    assert options.strict_mcp_config is True
    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert options.setting_sources == []
    assert options.permission_mode == "dontAsk"
    assert options.allowed_tools == [f"mcp__{GATEWAY}", "Task"]
    assert options.include_partial_messages is True  # needed for true per-turn token counts


def test_gateway_receives_token_and_traceparent(settings: Settings) -> None:
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    options = build_options(settings, 1, "jwt", traceparent, Path())

    server = options.mcp_servers[GATEWAY]  # type: ignore[index]
    assert server["url"] == settings.gateway_mcp_url
    assert server["headers"] == {"Authorization": "Bearer jwt", "traceparent": traceparent}


def test_subagent_gets_sandbox_tools_only(settings: Settings) -> None:
    options = build_options(settings, 1, "jwt", "tp", Path())

    tools = options.agents["test-writer"].tools  # type: ignore[index]
    assert tools
    assert options.agents["test-writer"].background is False  # type: ignore[index]
    assert all(tool.startswith(f"mcp__{GATEWAY}__sandbox_") for tool in tools)


def test_api_key_in_environment_blocks_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ensure_subscription_auth()


def test_retry_prompt_mentions_partial_work() -> None:
    request = AgentRequest(issue=IssueRef(number=3, title="Fix it"))
    assert "attempt" not in build_prompt(request, 1)
    assert "git status" in build_prompt(request, 2)
