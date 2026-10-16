"""Sandbox MCP server: file and shell tools inside an issue's sandbox container.

Deliberately naive in the baseline: any caller can address any issue's container,
paths are not confined to the workspace, and commands run as root in the
container. The red-team milestone attacks these; the harden milestone fixes them.
"""

import asyncio
import logging
import os

from fastmcp import FastMCP
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from coding_agent_control_plane.sandbox import containers

mcp = FastMCP("sandbox")


def _format(result: containers.ExecResult) -> str:
    return f"exit_code: {result.exit_code}\n{result.output}"


@mcp.tool
async def run_command(issue: int, command: str, timeout_s: int = 300) -> str:
    """Run a shell command in the issue's sandbox, from the repo root (/workspace).

    Returns the exit code and combined stdout/stderr (truncated if long).
    """
    return _format(await asyncio.to_thread(containers.exec_in, issue, command, timeout_s))


@mcp.tool
async def read_file(issue: int, path: str) -> str:
    """Read a text file, path relative to the repo root."""
    return _format(await asyncio.to_thread(containers.read_file, issue, path))


@mcp.tool
async def write_file(issue: int, path: str, content: str) -> str:
    """Create or overwrite a text file, path relative to the repo root."""
    await asyncio.to_thread(containers.write_file, issue, path, content)
    return f"wrote {len(content)} chars to {path}"


@mcp.tool
async def edit_file(issue: int, path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of old_text with new_text in a file.

    Fails if old_text occurs zero times or more than once; include enough
    surrounding lines to make it unique.
    """
    current = await asyncio.to_thread(containers.read_file, issue, path)
    if current.exit_code != 0:
        return _format(current)
    count = current.output.count(old_text)
    if count != 1:
        return f"error: old_text occurs {count} times in {path}; expected exactly 1"
    updated = current.output.replace(old_text, new_text, 1)
    await asyncio.to_thread(containers.write_file, issue, path, updated)
    return f"edited {path}"


@mcp.tool
async def list_files(issue: int, path: str = ".") -> str:
    """List tracked and untracked (not ignored) files under a directory."""
    command = f"git ls-files --cached --others --exclude-standard -- '{path}'"
    return _format(await asyncio.to_thread(containers.exec_in, issue, command, 60))


def _configure_tracing() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "sandbox-mcp"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _configure_tracing()
    port = int(os.environ.get("PORT", "8083"))
    # All interfaces inside its container; Compose publishes it on 127.0.0.1 only.
    mcp.run(transport="http", host="0.0.0.0", port=port)  # noqa: S104


if __name__ == "__main__":
    main()
