"""OpenTelemetry wiring: two exporters, two consoles.

- The global provider exports service spans (Temporal workflows and
  activities) to Jaeger.
- A separate provider exports agent spans (model turns, tool calls) to
  Langfuse's OTLP endpoint, which accepts HTTP only.
"""

import base64
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from temporalio.contrib.opentelemetry import ReplaySafeTracerProvider, create_tracer_provider

from coding_agent_control_plane.config import Settings

SERVICE_NAME = "coding-agent-worker"


def configure_jaeger(settings: Settings) -> ReplaySafeTracerProvider:
    # Replay-safe, so Temporal's OpenTelemetryPlugin does not duplicate workflow
    # spans when a workflow is replayed on another worker.
    provider = create_tracer_provider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.jaeger_otlp_endpoint, timeout=10))
    )
    trace.set_tracer_provider(provider)
    return provider


@lru_cache
def langfuse_provider(settings: Settings) -> TracerProvider:
    credentials = (
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key.get_secret_value()}"
    )
    exporter = OTLPSpanExporter(
        endpoint=settings.langfuse_otlp_endpoint,
        headers={
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(),
            "x-langfuse-ingestion-version": "4",
        },
        timeout=15,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def langfuse_tracer(settings: Settings) -> trace.Tracer:
    return langfuse_provider(settings).get_tracer("coding_agent_control_plane.agent")
