"""Regression checks for receipt verification, with no network or model calls."""

import asyncio

import httpx
import pytest

from coding_agent_control_plane.config import Settings
from scripts import trace_probe

TRACE_ID = "1" * 32
OLD_TRACE_ID = "2" * 32


class CaptureConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.results: list[tuple[str, bool, str]] = []

    def line(self, text: str) -> None:
        self.lines.append(text)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.results.append((name, ok, detail))


def payload(destination: str, trace_id: str) -> dict[str, object]:
    if destination == "Langfuse":
        return {"data": [{"name": "stack-check", "traceId": trace_id}]}
    return {
        "data": [
            {
                "spans": [
                    {
                        "operationName": "stack-check",
                        "traceID": trace_id,
                        "processID": "p1",
                    }
                ],
                "processes": {"p1": {"serviceName": "coding-agent-worker"}},
            }
        ],
    }


@pytest.mark.parametrize("destination", ["Langfuse", "Jaeger"])
def test_same_name_with_old_id_does_not_prove_delivery(destination: str) -> None:
    assert not trace_probe.trace_found(destination, payload(destination, OLD_TRACE_ID), TRACE_ID)
    assert trace_probe.trace_found(destination, payload(destination, TRACE_ID), TRACE_ID)


@pytest.mark.parametrize("bad_payload", [None, {}, {"data": None}, {"data": [None]}])
def test_bad_response_is_not_a_pass(bad_payload: object) -> None:
    assert not trace_probe.trace_found("Langfuse", bad_payload, TRACE_ID)


async def test_delayed_arrival_is_retried() -> None:
    calls = 0

    async def probe() -> bool:
        nonlocal calls
        calls += 1
        return calls == 3

    assert await trace_probe.wait_for_trace(probe, wait_seconds=1, interval=0)
    assert calls == 3


async def test_timeout_also_bounds_a_stuck_lookup() -> None:
    async def stuck() -> bool:
        await asyncio.Event().wait()
        return True

    assert not await trace_probe.wait_for_trace(stuck, wait_seconds=0.01)


def fake_export(destination: str, settings: Settings) -> tuple[str, bool]:
    return TRACE_ID, True


@pytest.mark.parametrize("destination", ["Langfuse", "Jaeger"])
async def test_new_trace_passes_only_when_read_back(
    monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    monkeypatch.setattr(trace_probe, "send_test_trace", fake_export)
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload(destination, TRACE_ID))

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_new_trace(
            destination,
            Settings.model_construct(),
            console,
            http,
            1,
            "https://cloud.langfuse.com",
            ("public", "secret-value"),
        )
    assert len(console.results) == 1
    assert console.results[0][1] is True
    assert len(seen) == 1
    if destination == "Langfuse":
        assert seen[0].url.params["traceId"] == TRACE_ID
    else:
        assert seen[0].url.path.endswith(TRACE_ID)
    assert "secret-value" not in str(console.lines + console.results)


async def test_successful_flush_with_only_old_trace_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trace_probe, "send_test_trace", fake_export)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload("Langfuse", OLD_TRACE_ID))

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_new_trace(
            "Langfuse",
            Settings.model_construct(),
            console,
            http,
            0.01,
            "https://cloud.langfuse.com",
            ("public", "secret-value"),
        )
    assert len(console.results) == 1
    assert console.results[0][1] is False
    assert "not confirmed" in console.results[0][2]


async def test_authentication_failure_stops_polling_without_leaking_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trace_probe, "send_test_trace", fake_export)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="secret-value")

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_new_trace(
            "Langfuse",
            Settings.model_construct(),
            console,
            http,
            1,
            "https://cloud.langfuse.com",
            ("public", "secret-value"),
        )
    assert calls == 1
    assert console.results[0][1] is False
    assert "401" in console.results[0][2]
    assert "secret-value" not in str(console.lines + console.results)
