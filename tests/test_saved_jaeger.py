"""Saved-trace regressions: current Jaeger API, empty searches and API failures."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from scripts import trace_probe

TRACE_ID = "a" * 32
OTHER_ID = "b" * 32


class CaptureConsole:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def line(self, text: str) -> None:
        pass

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.results.append((name, ok, detail))


def search_payload(
    trace_id: str = TRACE_ID,
    service: str = "coding-agent-worker",
    operation: str = "stack-check",
) -> str:
    return json.dumps(
        {
            "result": {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": service}}
                            ]
                        },
                        "scopeSpans": [{"spans": [{"traceId": trace_id, "name": operation}]}],
                    }
                ]
            }
        }
    )


async def test_search_uses_v3_and_finds_trace_across_stream_messages() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v3/traces"
        params = request.url.params
        assert params["query.serviceName"] == "coding-agent-worker"
        assert params["query.operationName"] == "stack-check"
        assert int(params["query.searchDepth"]) > 0
        assert (
            datetime.fromisoformat(params["query.startTimeMax"])
            - datetime.fromisoformat(params["query.startTimeMin"])
        ) == timedelta(hours=72)
        body = "\n".join(
            [
                search_payload(OTHER_ID, service="unrelated-service"),
                search_payload(OTHER_ID, operation="unrelated-operation"),
                search_payload(),
                search_payload(),
            ]
        )
        return httpx.Response(200, text=body)

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_saved_jaeger_trace(console, http, 72)
    assert console.results == [
        ("Saved Jaeger test trace found", True, f"stack-check found; trace IDs: {TRACE_ID}")
    ]


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_unavailable_api_is_not_reported_as_absent_trace(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="sensitive-response-do-not-print")

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_saved_jaeger_trace(console, http, 24)
    name, ok, detail = console.results[0]
    assert name == "Saved Jaeger trace lookup"
    assert not ok
    assert f"HTTP {status}" in detail
    assert "lookup failed" in detail
    assert "not found" not in detail
    assert "sensitive-response" not in str(console.results)


@pytest.mark.parametrize("body", ['{"result":{}}', '{"result":{"resourceSpans":[]}}'])
async def test_successful_empty_search_is_reported_as_no_saved_trace(body: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_saved_jaeger_trace(console, http, 24)
    name, ok, detail = console.results[0]
    assert name == "Saved Jaeger test trace found"
    assert not ok
    assert "lookup succeeded; no matching saved test trace" in detail
    assert "24h" in detail


@pytest.mark.parametrize(
    "body",
    [
        "",
        "invalid-json",
        "{}",
        '{"result":{"resourceSpans":null}}',
        search_payload() + '\n{"error":{"message":"sensitive-response-do-not-print"}}',
    ],
)
async def test_invalid_or_partially_failed_stream_cannot_pass(body: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_saved_jaeger_trace(console, http, 24)
    assert console.results[0][0] == "Saved Jaeger trace lookup"
    assert not console.results[0][1]
    assert "lookup failed" in console.results[0][2]
    assert "sensitive-response" not in str(console.results)


@pytest.mark.parametrize("returned_id", [TRACE_ID, OTHER_ID])
async def test_explicit_saved_id_requires_the_requested_trace(returned_id: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/traces/{TRACE_ID}"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "processes": {"p1": {"serviceName": "coding-agent-worker"}},
                        "spans": [
                            {
                                "processID": "p1",
                                "operationName": "stack-check",
                                "traceID": returned_id,
                            }
                        ],
                    }
                ]
            },
        )

    console = CaptureConsole()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await trace_probe.verify_saved_jaeger_trace(console, http, 24, TRACE_ID.upper())
    assert console.results[0][1] is (returned_id == TRACE_ID)
