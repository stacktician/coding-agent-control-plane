"""Inspect saved diagnostic traces or send and verify new ones."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from coding_agent_control_plane.config import Settings


class Console(Protocol):
    def line(self, text: str) -> None: ...

    def check(self, name: str, ok: bool, detail: str) -> None: ...


def trace_found(destination: str, payload: object, trace_id: str) -> bool:
    """Never mistake an older trace with the same name for the newly sent trace."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return False
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        if destination == "Langfuse":
            if row.get("name") == "stack-check" and row.get("traceId") == trace_id:
                return True
        elif destination == "Jaeger":
            processes = row.get("processes", {})
            spans = row.get("spans", [])
            if not isinstance(processes, dict) or not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                process = processes.get(span.get("processID"), {})
                if (
                    span.get("traceID") == trace_id
                    and span.get("operationName") == "stack-check"
                    and isinstance(process, dict)
                    and process.get("serviceName") == "coding-agent-worker"
                ):
                    return True
    return False


async def wait_for_trace(
    probe: Callable[[], Awaitable[bool]], wait_seconds: float, interval: float = 5.0
) -> bool:
    """Bound both the requests and retries; missing receipt is a failure."""
    try:
        async with asyncio.timeout(wait_seconds):
            while True:
                if await probe():
                    return True
                await asyncio.sleep(interval)
    except TimeoutError:
        return False


def send_test_trace(destination: str, settings: Settings) -> tuple[str, bool]:
    """Send one diagnostic trace using the application's telemetry exporters."""
    from opentelemetry.trace import format_trace_id

    from coding_agent_control_plane.telemetry import configure_jaeger, langfuse_provider

    provider = (
        configure_jaeger(settings) if destination == "Jaeger" else langfuse_provider(settings)
    )
    with provider.get_tracer("stack_check").start_as_current_span("stack-check") as span:
        if destination == "Langfuse":
            span.set_attribute("langfuse.trace.name", "stack-check")
        trace_id = format_trace_id(span.get_span_context().trace_id)
    # A successful flush does not prove the server accepted or saved the trace.
    return trace_id, provider.force_flush(timeout_millis=15_000)


def jaeger_search_ids(body: str) -> set[str]:
    """Read Jaeger's v3 newline-delimited OTLP JSON, rejecting failed searches."""
    matches: set[str] = set()
    messages = [json.loads(line) for line in body.splitlines() if line.strip()]
    if not messages:
        raise ValueError("Empty search response")
    for message in messages:
        if not isinstance(message, dict) or "error" in message:
            raise ValueError("Invalid or failed search response")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ValueError("Missing search result")
        for resource in result.get("resourceSpans", []):
            attributes = resource.get("resource", {}).get("attributes", [])
            if not any(
                attr.get("key") == "service.name"
                and attr.get("value", {}).get("stringValue") == "coding-agent-worker"
                for attr in attributes
            ):
                continue
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    trace_id = span.get("traceId")
                    if (
                        span.get("name") == "stack-check"
                        and isinstance(trace_id, str)
                        and re.fullmatch(r"[0-9a-fA-F]{32}", trace_id)
                    ):
                        matches.add(trace_id.lower())
    return matches


async def verify_saved_jaeger_trace(
    console: Console,
    http: httpx.AsyncClient,
    lookback_hours: int,
    trace_id: str | None = None,
) -> None:
    """Inspect saved traces; report lookup failures separately from absent data."""
    try:
        if trace_id:
            trace_id = trace_id.lower()
            response = await http.get(f"http://localhost:16686/api/traces/{trace_id}")
        else:
            now = datetime.now(UTC)
            response = await http.get(
                "http://localhost:16686/api/v3/traces",
                params={
                    "query.serviceName": "coding-agent-worker",
                    "query.operationName": "stack-check",
                    "query.searchDepth": 20,
                    "query.startTimeMin": (now - timedelta(hours=lookback_hours)).isoformat(),
                    "query.startTimeMax": now.isoformat(),
                },
            )
        if response.status_code != 200:
            console.check(
                "Saved Jaeger trace lookup",
                False,
                f"lookup failed (HTTP {response.status_code}); could not verify saved traces",
            )
            return
        if trace_id:
            matches = {trace_id} if trace_found("Jaeger", response.json(), trace_id) else set()
        else:
            matches = jaeger_search_ids(response.text)
        console.check(
            "Saved Jaeger test trace found",
            bool(matches),
            "stack-check found; trace IDs: " + ", ".join(sorted(matches))
            if matches
            else "lookup succeeded; no matching saved test trace"
            + (f" for ID {trace_id}" if trace_id else f" in the last {lookback_hours}h"),
        )
    except Exception as exc:
        console.check(
            "Saved Jaeger trace lookup",
            False,
            f"lookup failed ({type(exc).__name__}); could not verify saved traces",
        )


async def verify_new_trace(
    destination: str,
    settings: Settings,
    console: Console,
    http: httpx.AsyncClient,
    wait_seconds: float,
    base_url: str = "",
    auth: tuple[str, str] | None = None,
) -> None:
    """Send one trace and read back its exact ID. Never attempt repairs."""
    started = datetime.now(UTC)
    last_status: int | None = None
    try:
        trace_id, flushed = await asyncio.to_thread(send_test_trace, destination, settings)
        console.line(f"[INFO] New {destination} test trace: {trace_id}; checking arrival")
        if not flushed:
            console.line(f"[INFO] {destination} export wait expired; checking server anyway")

        async def probe() -> bool:
            nonlocal last_status
            try:
                if destination == "Langfuse":
                    response = await http.get(
                        base_url + "/api/public/v2/observations",
                        auth=auth,
                        params={
                            "traceId": trace_id,
                            "name": "stack-check",
                            "fields": "core,basic",
                            "limit": 100,
                            "fromStartTime": (started - timedelta(minutes=5)).isoformat(),
                            "toStartTime": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                        },
                    )
                else:
                    response = await http.get(f"http://localhost:16686/api/traces/{trace_id}")
            except (httpx.TimeoutException, httpx.NetworkError):
                return False
            last_status = response.status_code
            if last_status == 200:
                return trace_found(destination, response.json(), trace_id)
            if last_status in (404, 429) or last_status >= 500:
                return False
            raise RuntimeError("Trace lookup rejected")

        found = await wait_for_trace(probe, wait_seconds)
        console.check(
            f"New {destination} test trace received",
            found,
            f"confirmed this run's trace {trace_id}"
            if found
            else f"not confirmed in {wait_seconds:g}s; last HTTP {last_status}; trace {trace_id}",
        )
    except Exception as exc:
        # Never print exception messages that could include credentials or headers.
        console.check(
            f"New {destination} test trace received",
            False,
            f"check failed ({type(exc).__name__}); last HTTP {last_status}",
        )
