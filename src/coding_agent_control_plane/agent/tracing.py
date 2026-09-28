"""Turns the Agent SDK message stream into Langfuse spans.

One trace per agent run (session ``issue-<n>``):
- root ``agent`` observation: the prompt in, the final summary out;
- one ``generation`` per model turn, with model and token usage;
- one ``tool`` span per tool call, from the tool_use block to its result.
Subagent turns carry ``parent_tool_use_id`` and nest under their Task call.

Per-turn usage on AssistantMessage is a start-of-message snapshot (output
tokens near zero); the final output count arrives in the raw ``message_delta``
stream event, so the run must set ``include_partial_messages=True``.
"""

import json
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, Tracer, format_span_id, format_trace_id

MCP_PREFIX = "mcp__gateway__"
SUBAGENT_TOOLS = {"Task", "Agent"}  # the CLI lists "Task" but emits tool_use blocks as "Agent"
MAX_ATTRIBUTE_CHARS = 8_000


def _clip(text: str) -> str:
    return text if len(text) <= MAX_ATTRIBUTE_CHARS else text[:MAX_ATTRIBUTE_CHARS] + " [...]"


def _tool_output(content: str | list[dict[str, Any]] | None) -> str:
    if content is None or isinstance(content, str):
        return content or ""
    return "\n".join(str(part.get("text", part)) for part in content)


class AgentTrace:
    def __init__(self, tracer: Tracer, issue: int, prompt: str, model: str, attempt: int) -> None:
        self._tracer = tracer
        self._root = tracer.start_span(
            f"issue-{issue} agent",
            context=otel_context.Context(),  # a new root, not the Temporal activity span
            attributes={
                "langfuse.trace.name": f"issue-{issue} agent",
                "langfuse.session.id": f"issue-{issue}",
                "langfuse.observation.type": "agent",
                "langfuse.observation.input": _clip(prompt),
                "langfuse.trace.metadata.model": model,
                "langfuse.trace.metadata.attempt": attempt,
            },
        )
        self._root_context = trace.set_span_in_context(self._root)
        self._tools: dict[str, Span] = {}
        self._generation: Span | None = None
        self._generation_id: str | None = None
        self._generation_usage: dict[str, int] = {}
        self._generation_text: list[str] = []
        self._turn_start_ns: int | None = None
        self._last_event_ns = time.time_ns()

    @property
    def trace_id(self) -> str:
        return format_trace_id(self._root.get_span_context().trace_id)

    @property
    def traceparent(self) -> str:
        """W3C header, so gateway spans in Jaeger share this Langfuse trace ID."""
        ctx = self._root.get_span_context()
        return f"00-{format_trace_id(ctx.trace_id)}-{format_span_id(ctx.span_id)}-01"

    def _parent(self, parent_tool_use_id: str | None) -> otel_context.Context:
        task_span = self._tools.get(parent_tool_use_id or "")
        return trace.set_span_in_context(task_span) if task_span else self._root_context

    def _end_generation(self) -> None:
        if self._generation is not None:
            self._generation.end()
        self._generation = None
        self._generation_id = None

    def on_message(self, message: Message) -> None:
        now = time.time_ns()
        if isinstance(message, StreamEvent):
            self._on_stream_event(message.event, now)
            return  # raw stream events do not move the turn clock
        if isinstance(message, AssistantMessage):
            self._on_assistant(message, now)
        elif isinstance(message, UserMessage):
            self._end_generation()
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        self._end_tool(block, now)
        elif isinstance(message, ResultMessage):
            self._end_generation()
            self._root.set_attribute("langfuse.observation.output", _clip(message.result or ""))
            self._root.set_attribute("langfuse.trace.metadata.turns", message.num_turns)
            # Authoritative run totals; subagent turns under-report output tokens because
            # the SDK does not forward their message_delta events.
            for key, value in _usage(message.usage or {}).items():
                self._root.set_attribute(f"langfuse.trace.metadata.total_{key}", value)
            if message.total_cost_usd is not None:
                # Notional API-equivalent cost; runs are billed to the subscription.
                self._root.set_attribute(
                    "langfuse.trace.metadata.api_equivalent_cost_usd", message.total_cost_usd
                )
            if message.is_error:
                self._root.set_attribute("langfuse.observation.level", "ERROR")
        self._last_event_ns = now

    # Any: a raw Anthropic API stream event, untyped in the SDK.
    def _on_stream_event(self, event: dict[str, Any], now: int) -> None:
        kind = event.get("type")
        if kind == "message_start":
            self._turn_start_ns = now
        elif kind == "message_delta" and self._generation is not None:
            output = (event.get("usage") or {}).get("output_tokens")
            if isinstance(output, int):
                self._generation_usage["output"] = output
                self._set_usage()

    def _set_usage(self) -> None:
        if self._generation is not None and self._generation_usage:
            self._generation.set_attribute(
                "langfuse.observation.usage_details", json.dumps(self._generation_usage)
            )

    def _on_assistant(self, message: AssistantMessage, now: int) -> None:
        parent = self._parent(message.parent_tool_use_id)
        # The CLI can split one model turn into several messages sharing an ID.
        if self._generation is None or message.message_id != self._generation_id:
            self._end_generation()
            self._generation = self._tracer.start_span(
                "subagent turn" if message.parent_tool_use_id else "model turn",
                context=parent,
                start_time=self._turn_start_ns or self._last_event_ns,
                attributes={
                    "langfuse.observation.type": "generation",
                    "langfuse.observation.model.name": message.model,
                },
            )
            self._generation_id = message.message_id
            self._generation_usage = {}
            self._generation_text = []
            self._turn_start_ns = None
        if message.usage:
            snapshot = _usage(message.usage)
            final_output = self._generation_usage.get("output", 0)
            snapshot["output"] = max(snapshot.get("output", 0), final_output)
            self._generation_usage = snapshot
            self._set_usage()
        text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
        if text:
            self._generation_text.append(text)
            output = "\n".join(self._generation_text)
            self._generation.set_attribute("langfuse.observation.output", _clip(output))
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                self._start_tool(block, parent, now)

    def _start_tool(self, block: ToolUseBlock, parent: otel_context.Context, now: int) -> None:
        name = block.name.removeprefix(MCP_PREFIX)
        if block.name in SUBAGENT_TOOLS:
            name = f"subagent {block.input.get('subagent_type', '')}".strip()
        self._tools[block.id] = self._tracer.start_span(
            name,
            context=parent,
            start_time=now,
            attributes={
                "langfuse.observation.type": "tool",
                "langfuse.observation.input": _clip(json.dumps(block.input)),
            },
        )

    def _end_tool(self, block: ToolResultBlock, now: int) -> None:
        span = self._tools.get(block.tool_use_id)
        if span is None:
            return
        span.set_attribute("langfuse.observation.output", _clip(_tool_output(block.content)))
        if block.is_error:
            span.set_attribute("langfuse.observation.level", "ERROR")
        span.end(end_time=now)

    def close(self, error: BaseException | None = None) -> None:
        self._end_generation()
        for span in self._tools.values():
            if span.is_recording():
                span.end()
        if error is not None:
            self._root.set_attribute("langfuse.observation.level", "ERROR")
            self._root.set_attribute("langfuse.observation.status_message", repr(error))
        self._root.end()


def _usage(usage: dict[str, Any]) -> dict[str, int]:
    keys = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_read_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens",
    }
    return {name: int(usage[key]) for key, name in keys.items() if isinstance(usage.get(key), int)}
