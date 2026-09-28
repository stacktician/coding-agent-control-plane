"""Per-issue sandbox containers, driven through the Docker API.

Synchronous (docker-py is blocking); async callers wrap each call in
``asyncio.to_thread``. Used by the worker's sandbox activities and by the
sandbox MCP server.
"""

import io
import tarfile
import time
from pathlib import PurePosixPath

import docker
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import Container
from pydantic import BaseModel

SANDBOX_IMAGE = "cacp-sandbox:py312"
WORKDIR = "/workspace"
MAX_OUTPUT_CHARS = 20_000

_DOCKERFILE = b"""\
FROM python:3.12-slim
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git \\
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
"""


class ExecResult(BaseModel):
    exit_code: int
    output: str


def container_name(issue: int) -> str:
    return f"cacp-sandbox-issue-{issue}"


def _client() -> docker.DockerClient:
    return docker.from_env(timeout=600)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def ensure_image() -> None:
    client = _client()
    try:
        client.images.get(SANDBOX_IMAGE)
    except ImageNotFound:
        client.images.build(fileobj=io.BytesIO(_DOCKERFILE), tag=SANDBOX_IMAGE, rm=True)


def _get(issue: int) -> Container:
    return _client().containers.get(container_name(issue))


def exec_in(issue: int, command: str, timeout_s: int = 300) -> ExecResult:
    result = _get(issue).exec_run(["timeout", str(timeout_s), "sh", "-c", command], workdir=WORKDIR)
    output = result.output.decode(errors="replace") if result.output else ""
    return ExecResult(exit_code=result.exit_code or 0, output=_truncate(output))


def create(issue: int, repo_url: str, install_command: str) -> ExecResult:
    """Start the issue's container, clone the repo and install it. Idempotent."""
    ensure_image()
    client = _client()
    try:
        container = client.containers.get(container_name(issue))
        if container.status != "running":
            container.start()
    except NotFound:
        client.containers.run(
            SANDBOX_IMAGE,
            ["sleep", "infinity"],
            name=container_name(issue),
            labels={"cacp.issue": str(issue)},
            detach=True,
        )
    cloned = exec_in(issue, "test -d .git", timeout_s=10).exit_code == 0
    if not cloned:
        clone = exec_in(issue, f"git clone --depth 50 {repo_url} .", timeout_s=300)
        if clone.exit_code != 0:
            return clone
    return exec_in(issue, install_command, timeout_s=600)


def remove(issue: int) -> None:
    try:
        _get(issue).remove(force=True)
    except NotFound:
        pass


class FileChange(BaseModel):
    path: str
    content: bytes | None  # None means the file was deleted


def head_sha(issue: int) -> str:
    return exec_in(issue, "git rev-parse HEAD", timeout_s=10).output.strip()


def _read_bytes(container: Container, path: str) -> bytes:
    stream, _ = container.get_archive(str(PurePosixPath(WORKDIR) / path))
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
        member = tar.next()
        extracted = tar.extractfile(member) if member else None
        if extracted is None:
            raise ValueError(f"{path} is not a regular file")
        return extracted.read()


def staged_changes(issue: int) -> list[FileChange]:
    """Stage everything in the working tree and return it as full-file changes."""
    listing = exec_in(
        issue, "git add -A && git diff --cached --name-status --no-renames -z", timeout_s=60
    )
    if listing.exit_code != 0:
        raise RuntimeError(f"git diff failed: {listing.output}")
    fields = [f for f in listing.output.split("\0") if f]
    container = _get(issue)
    changes = []
    for status, path in zip(fields[0::2], fields[1::2], strict=True):
        content = None if status == "D" else _read_bytes(container, path)
        changes.append(FileChange(path=path, content=content))
    return changes


def read_file(issue: int, path: str) -> ExecResult:
    return exec_in(issue, f"cat -- '{path}'", timeout_s=30)


def write_file(issue: int, path: str, content: str) -> None:
    target = PurePosixPath(WORKDIR) / path
    data = content.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=target.name)
        info.size = len(data)
        info.mtime = int(time.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    container = _get(issue)
    container.exec_run(["mkdir", "-p", str(target.parent)])
    container.put_archive(str(target.parent), buf.getvalue())
