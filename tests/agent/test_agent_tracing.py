"""AgentTrace maps the SDK message stream to Langfuse spans."""

import json

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from coding_agent_control_plane.agent.tracing import AgentTrace


def _run(messages: list[object]) -> tuple[AgentTrace, list[ReadableSpan]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = AgentTrace(provider.get_tracer("test"), 5, "Resolve issue #5", "model-x", 1)
    for message in messages:
        recorder.on_message(message)  # type: ignore[arg-type]
    recorder.close()
    return recorder, list(exporter.get_finished_spans())


def _assistant(
    message_id: str, blocks: list[object], parent: str | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,  # type: ignore[arg-type]
        model="model-x",
        parent_tool_use_id=parent,
        usage={"input_tokens": 10, "output_tokens": 5},
        message_id=message_id,
    )


def test_tools_nest_under_their_task_and_turns_carry_usage() -> None:
    task = ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "test-writer"})
    read = ToolUseBlock(id="t2", name="mcp__gateway__sandbox_read_file", input={"path": "a.py"})
    recorder, finished = _run(
        [
            _assistant("m1", [TextBlock(text="delegating"), task]),
            _assistant("m2", [read], parent="t1"),
            UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="print(1)")]),
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="added tests")]),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=2,
                session_id="s",
                result="Done",
            ),
        ]
    )

    spans = {span.name: span for span in finished}
    root = spans["issue-5 agent"]
    subagent = spans["subagent test-writer"]
    tool = spans["sandbox_read_file"]
    assert root.attributes is not None
    assert root.attributes["langfuse.session.id"] == "issue-5"
    assert root.attributes["langfuse.observation.output"] == "Done"
    assert subagent.parent is not None and tool.parent is not None
    assert subagent.parent.span_id == root.context.span_id
    assert tool.parent.span_id == subagent.context.span_id
    assert tool.attributes is not None
    assert tool.attributes["langfuse.observation.output"] == "print(1)"
    turn = spans["model turn"]
    assert turn.attributes is not None
    assert json.loads(str(turn.attributes["langfuse.observation.usage_details"])) == {
        "input": 10,
        "output": 5,
    }
    assert recorder.traceparent.startswith(f"00-{recorder.trace_id}-")


def test_split_messages_with_one_id_make_one_generation() -> None:
    _, spans = _run(
        [
            _assistant("m1", [TextBlock(text="part one")]),
            _assistant("m1", [TextBlock(text="part two")]),
        ]
    )
    generations = [s for s in spans if s.name == "model turn"]
    assert len(generations) == 1


def test_final_output_tokens_come_from_message_delta() -> None:
    start = StreamEvent(uuid="u1", session_id="s", event={"type": "message_start"})
    delta = StreamEvent(
        uuid="u2", session_id="s", event={"type": "message_delta", "usage": {"output_tokens": 42}}
    )
    _, spans = _run([start, _assistant("m1", [TextBlock(text="hello")]), delta])

    turn = next(s for s in spans if s.name == "model turn")
    assert turn.attributes is not None
    usage = json.loads(str(turn.attributes["langfuse.observation.usage_details"]))
    assert usage == {"input": 10, "output": 42}
