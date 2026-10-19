"""Inspect the existing control-plane setup; never restart or repair it.

Run from Git Bash: uv run python -B scripts/check_connections.py
Configuration comes from the existing project .env. Console output only.
Langfuse API keys identify the project; no separate project ID is needed.
Default: inspect saved stack-check traces from the last 24 hours.
Add --send-test-traces to send two new traces and confirm those exact IDs arrived.
Optional exact IDs inspect previous runs and cannot be combined with sending.
Normal read requests can generate service access logs and gateway telemetry.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from coding_agent_control_plane.config import Settings

# Set before any project or third-party imports, even when launched without -B.
sys.dont_write_bytecode = True

# Avoid third-party diagnostic output accidentally disclosing request details.
logging.disable(logging.CRITICAL)


class Console:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def line(self, text: str) -> None:
        print(text, flush=True)

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.line(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            self.failures.append(name)

    def error(self, name: str, exc: Exception) -> None:
        # Exception messages can contain URLs/headers. Only disclose the class.
        self.check(name, False, f"check could not complete ({type(exc).__name__})")


def docker(*args: str) -> bytes:
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker executable unavailable")
    result = subprocess.run(  # noqa: S603 - fixed read-only subcommands, shell disabled
        [executable, *args],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Docker read command failed")
    return result.stdout


def docker_rows() -> list[dict[str, object]]:
    raw = docker("compose", "ps", "--all", "--format", "json").decode().strip()
    return (
        json.loads(raw)
        if raw.startswith("[")
        else [json.loads(line) for line in raw.splitlines() if line.strip()]
    )


def fingerprint(project: Path) -> dict[str, bytes]:
    """Hash project files, not dependency installs or Git internals."""
    result = {}
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in files:
            path = Path(root) / name
            result[str(path.relative_to(project))] = hashlib.sha256(path.read_bytes()).digest()
    return result


def error_category(text: str) -> str:
    if "401" in text or "bad credentials" in text.lower():
        return "GitHub authentication failed (401 / bad credentials)"
    if "403" in text:
        return "GitHub access denied (403)"
    if "404" in text:
        return "GitHub resource unavailable (404)"
    if "400" in text:
        return "backend rejected the request (400; possibly malformed credentials)"
    return "tool returned an error; raw response suppressed to protect credentials"


def contains_issue(value: object, number: int, repo: str) -> bool:
    if isinstance(value, dict):
        if value.get("number") == number and value.get("title"):
            url = value.get("html_url", value.get("url", ""))
            if f"/{repo}/issues/{number}" in url:
                return True
        return any(contains_issue(v, number, repo) for v in value.values())
    if isinstance(value, list):
        return any(contains_issue(v, number, repo) for v in value)
    return False


async def checks(
    args: argparse.Namespace,
    report: Console,
    settings: Settings,
    values: Mapping[str, str | None],
    containers: list[dict[str, object]] | None,
) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from trace_probe import verify_new_trace, verify_saved_jaeger_trace

    expected = {"agentgateway", "github-mcp", "jaeger", "keycloak", "sandbox-mcp", "temporal"}
    if containers is not None:
        running = {r.get("Service") for r in containers if r.get("State") == "running"}
        missing = sorted(expected - running)
        report.check(
            "Docker services",
            not missing,
            "all six services running" if not missing else "not running: " + ", ".join(missing),
        )
        try:
            gateway = next(r for r in containers if r.get("Service") == "agentgateway")
            cid = gateway.get("ID") or gateway["Name"]
            data = docker("cp", f"{cid}:/run/secrets/github_pat", "-")
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                member = next(m for m in archive.getmembers() if m.isfile())
                stored = archive.extractfile(member).read().decode().strip()
            current = (values.get("GITHUB_PAT") or "").strip()
            same = bool(current) and hmac.compare_digest(stored, current)
            report.check(
                "Gateway PAT matches .env",
                same,
                "same credential (values hidden)"
                if same
                else "different PAT; recreate gateway separately to load current .env",
            )
        except Exception as exc:
            report.error("Gateway PAT matches .env", exc)

    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        pat = settings.github_pat.get_secret_value()
        headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
        fork = settings.github_fork
        issue_number = args.issue_number
        valid_fork = bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fork))
        if not valid_fork:
            report.check(
                "Configured fork",
                False,
                "GITHUB_FORK must be owner/repository; GitHub reads skipped",
            )
        else:
            report.check("Configured fork", True, fork)
            try:
                response = await http.get("https://api.github.com/user", headers=headers)
                login = response.json().get("login") if response.status_code == 200 else None
                report.check(
                    ".env PAT authentication",
                    response.status_code == 200 and bool(login),
                    f"HTTP {response.status_code}; "
                    + (
                        "authenticated account: " + login
                        if login
                        else "authentication not verified"
                    ),
                )
                response = await http.get(f"https://api.github.com/repos/{fork}", headers=headers)
                obj = response.json() if response.status_code == 200 else {}
                report.check(
                    ".env PAT repository read",
                    obj.get("full_name") == fork,
                    f"HTTP {response.status_code}; expected fork "
                    + ("confirmed" if obj.get("full_name") == fork else "not confirmed"),
                )
                report.check(
                    "Base branch",
                    obj.get("default_branch") == settings.github_base_branch,
                    "configured base matches repository default"
                    if obj.get("default_branch") == settings.github_base_branch
                    else "configured base/default not confirmed",
                )
                response = await http.get(
                    f"https://api.github.com/repos/{fork}/labels",
                    headers=headers,
                    params={"per_page": 100},
                )
                labels = (
                    {x["name"]: x["color"].lower() for x in response.json()}
                    if response.status_code == 200
                    else {}
                )
                wanted = {
                    "agent-ok": "0e8a16",
                    "agent-claimed": "fbca04",
                    "agent-approve": "1d76db",
                    "agent-reject": "b60205",
                }
                report.check(
                    "Four agent labels and colors",
                    all(labels.get(k) == v for k, v in wanted.items()),
                    "all exact names/colors present"
                    if all(labels.get(k) == v for k, v in wanted.items())
                    else "one or more names/colors not confirmed",
                )
                response = await http.get(
                    f"https://api.github.com/repos/{fork}/issues",
                    headers=headers,
                    params={"state": "open", "per_page": 100},
                )
                open_issues = [
                    x
                    for x in (response.json() if response.status_code == 200 else [])
                    if "pull_request" not in x
                ]
                if issue_number is None and open_issues:
                    issue_number = min(x["number"] for x in open_issues)
                # The seeds are found by the upstream issue they reference, not by number: the
                # demo setup script deletes and recreates them, and GitHub never reuses a number.
                for upstream in (289, 299, 282, 301) if args.check_seed_issues else ():
                    url = f"https://github.com/mahmoud/boltons/issues/{upstream}"
                    found = [x for x in open_issues if url in (x.get("body") or "")]
                    good = len(found) == 1 and found[0].get("labels") == []
                    report.check(
                        f"Seed issue from upstream #{upstream}",
                        good,
                        f"fork issue #{found[0]['number']} open and unlabeled"
                        if good
                        else "not exactly one open, unlabeled fork issue references it",
                    )
            except Exception as exc:
                report.error("GitHub direct reads", exc)

        token = None
        try:
            response = await http.post(
                settings.keycloak_token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.keycloak_client_id,
                    "client_secret": settings.kc_agent_worker_secret.get_secret_value(),
                },
            )
            if response.status_code == 200:
                token = response.json().get("access_token")
            report.check(
                "Keycloak authentication",
                bool(token),
                f"HTTP {response.status_code}; token value hidden",
            )
        except Exception as exc:
            report.error("Keycloak authentication", exc)
        try:
            response = await http.post(settings.gateway_mcp_url, json={})
            report.check(
                "Gateway rejects missing token",
                response.status_code == 401,
                f"HTTP {response.status_code} (expected 401)",
            )
        except Exception as exc:
            report.error("Gateway rejects missing token", exc)
        if token and valid_fork and issue_number:
            try:
                transport = StreamableHttpTransport(
                    settings.gateway_mcp_url, headers={"Authorization": f"Bearer {token}"}
                )
                async with Client(transport) as client:
                    tools = await asyncio.wait_for(client.list_tools(), 25)
                    counts = {
                        prefix: sum(t.name.startswith(prefix) for t in tools)
                        for prefix in ("github_", "sandbox_")
                    }
                    report.check(
                        "Gateway tool discovery",
                        all(counts.values()),
                        f"GitHub: {counts['github_']}; sandbox: {counts['sandbox_']}",
                    )
                    owner, repo = fork.split("/", 1)
                    result = await client.call_tool(
                        "github_issue_read",
                        {
                            "method": "get",
                            "owner": owner,
                            "repo": repo,
                            "issue_number": issue_number,
                        },
                        timeout=25,
                        raise_on_error=False,
                    )
                    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                    if result.is_error:
                        report.check(
                            "Actual GitHub read THROUGH gateway", False, error_category(text)
                        )
                    else:
                        payloads = [result.structured_content, result.data]
                        for block in result.content:
                            if hasattr(block, "text"):
                                try:
                                    payloads.append(json.loads(block.text))
                                except (ValueError, TypeError):
                                    pass
                        good = any(contains_issue(p, issue_number, fork) for p in payloads)
                        report.check(
                            "Actual GitHub read THROUGH gateway",
                            good,
                            f"{fork} issue #{issue_number} returned using Docker's credential"
                            if good
                            else "issue not verified; raw response suppressed",
                        )
            except Exception as exc:
                report.error("Actual GitHub read THROUGH gateway", exc)
        else:
            report.check(
                "Actual GitHub read THROUGH gateway",
                False,
                "blocked by Keycloak authentication, invalid GITHUB_FORK or no open issue to read",
            )

        try:
            endpoint = urlsplit(settings.langfuse_otlp_endpoint)
            correct_endpoint = (
                endpoint.path == "/api/public/otel/v1/traces"
                and bool(endpoint.hostname)
                and not endpoint.username
                and not endpoint.password
                and not endpoint.query
                and not endpoint.fragment
                and (
                    endpoint.scheme == "https"
                    or (
                        endpoint.scheme == "http"
                        and endpoint.hostname in ("localhost", "127.0.0.1", "::1")
                    )
                )
            )
            report.check(
                "Langfuse trace endpoint",
                correct_endpoint,
                "valid configured OTLP trace endpoint"
                if correct_endpoint
                else "invalid endpoint format; no Langfuse credentials transmitted",
            )
            if correct_endpoint:
                base_url = f"{endpoint.scheme}://{endpoint.netloc}"
                auth = (
                    settings.langfuse_public_key,
                    settings.langfuse_secret_key.get_secret_value(),
                )
                response = await http.get(base_url + "/api/public/projects", auth=auth)
                rows = response.json().get("data", []) if response.status_code == 200 else []
                good = bool(rows) and all(p.get("id") and p.get("name") for p in rows)
                report.check(
                    "Langfuse keys and project",
                    good,
                    f"HTTP {response.status_code}; "
                    + (
                        "API keys accepted; project: " + ", ".join(p["name"] for p in rows)
                        if good
                        else "API-key project not verified"
                    ),
                )
                if args.send_test_traces:
                    if good:
                        await verify_new_trace(
                            "Langfuse", settings, report, http, args.trace_timeout, base_url, auth
                        )
                    else:
                        report.check(
                            "New Langfuse test trace received",
                            False,
                            "not sent because API-key authentication failed",
                        )
                else:
                    now = datetime.now(UTC)
                    params = {
                        "name": "stack-check",
                        "limit": 100,
                        "fields": "core,basic",
                        "fromStartTime": (now - timedelta(hours=args.lookback_hours)).isoformat(),
                        "toStartTime": now.isoformat(),
                    }
                    if args.langfuse_trace_id:
                        params["traceId"] = args.langfuse_trace_id
                    response = await http.get(
                        base_url + "/api/public/v2/observations", auth=auth, params=params
                    )
                    rows = response.json().get("data", []) if response.status_code == 200 else []
                    matches = [
                        r
                        for r in rows
                        if r.get("name") == "stack-check"
                        and (
                            not args.langfuse_trace_id or r.get("traceId") == args.langfuse_trace_id
                        )
                    ]
                    ids = sorted({r.get("traceId", "") for r in matches})
                    report.check(
                        "Saved Langfuse test trace found",
                        bool(matches),
                        "stack-check found; trace IDs: " + ", ".join(ids)
                        if matches
                        else f"not found in {args.lookback_hours}h; HTTP {response.status_code}",
                    )
        except Exception as exc:
            report.error("Langfuse verification", exc)

        if args.send_test_traces:
            await verify_new_trace("Jaeger", settings, report, http, args.trace_timeout)
        else:
            await verify_saved_jaeger_trace(report, http, args.lookback_hours, args.jaeger_trace_id)

        try:
            response = await http.get("http://localhost:8233")
            report.check("Temporal UI", response.status_code == 200, f"HTTP {response.status_code}")
        except Exception as exc:
            report.error("Temporal UI", exc)
    try:
        from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
        from temporalio.client import Client as TemporalClient

        client = await asyncio.wait_for(TemporalClient.connect(settings.temporal_address), 20)
        response = await asyncio.wait_for(
            client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace="default")
            ),
            20,
        )
        report.check(
            "Temporal backend",
            response.namespace_info.name == "default",
            "default namespace responds over gRPC",
        )
    except Exception as exc:
        report.error("Temporal backend", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--issue-number",
        type=int,
        help="Existing issue to read through the gateway (default: the lowest open issue)",
    )
    parser.add_argument(
        "--check-seed-issues",
        action="store_true",
        help="Also verify the original four training issues remain open and unlabeled",
    )
    parser.add_argument(
        "--send-test-traces",
        action="store_true",
        help="Send new traces to Jaeger and Langfuse and verify their exact IDs",
    )
    parser.add_argument(
        "--trace-timeout",
        type=float,
        default=60.0,
        help="Maximum seconds to wait for each new trace (default: 60; maximum: 300)",
    )
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--jaeger-trace-id")
    parser.add_argument("--langfuse-trace-id")
    args = parser.parse_args()
    if args.send_test_traces and (args.jaeger_trace_id or args.langfuse_trace_id):
        parser.error("--send-test-traces cannot be combined with saved trace IDs")
    if not 0 < args.trace_timeout <= 300:
        parser.error("--trace-timeout must be greater than 0 and at most 300 seconds")
    for trace in (args.jaeger_trace_id, args.langfuse_trace_id):
        if trace and not re.fullmatch(r"[0-9a-fA-F]{32}", trace):
            parser.error("trace IDs must contain exactly 32 hexadecimal characters")
    if args.issue_number is not None and args.issue_number <= 0:
        parser.error("--issue-number must be positive")
    if args.lookback_hours <= 0:
        parser.error("--lookback-hours must be positive")
    report = Console()
    project = Path(args.project).resolve()
    report.line("Connection check")
    report.line("Console only. No reports, repairs, container restarts, or GitHub writes.")
    report.line(
        "Mode: send new test traces and confirm their arrival."
        if args.send_test_traces
        else "Mode: inspect saved test traces; no new test traces sent."
    )
    before = None
    containers = None
    try:
        before = fingerprint(project)
        os.chdir(project)
        sys.path.insert(0, str(project / "src"))
        from dotenv import dotenv_values

        from coding_agent_control_plane.config import Settings

        values = dotenv_values(project / ".env")
        required = (
            "GITHUB_FORK",
            "GITHUB_PAT",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "KC_AGENT_WORKER_SECRET",
        )
        missing = [key for key in required if not values.get(key)]
        report.check(
            "Required .env entries",
            not missing,
            "present (values hidden)" if not missing else "missing: " + ", ".join(missing),
        )
        settings = Settings(_env_file=str(project / ".env"))
        overrides = []
        for key in (*required, "LANGFUSE_OTLP_ENDPOINT"):
            value = getattr(settings, key.lower())
            value = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
            if values.get(key) is not None and value != values[key]:
                overrides.append(key)
        report.check(
            "Environment overrides",
            not overrides,
            "no conflicting overrides"
            if not overrides
            else "runtime differs from .env for: " + ", ".join(overrides),
        )
        try:
            containers = docker_rows()
        except Exception as exc:
            report.error("Docker access", exc)
        asyncio.run(
            asyncio.wait_for(
                checks(args, report, settings, values, containers),
                240 + (2 * args.trace_timeout if args.send_test_traces else 0),
            )
        )
    except Exception as exc:
        report.error("Checker completion", exc)
    finally:
        if before is not None:
            try:
                after = fingerprint(project)
                report.check(
                    "Project file contents unchanged",
                    before == after,
                    f"compared {len(before)} files; dependency/cache/Git directories excluded",
                )
            except Exception as exc:
                report.error("Project file comparison", exc)
        if containers is not None:
            try:
                after = docker_rows()
                ids_before = {r.get("Service"): r.get("ID") for r in containers}
                ids_after = {r.get("Service"): r.get("ID") for r in after}
                report.check(
                    "Container identities unchanged",
                    ids_before == ids_after,
                    "compared container IDs before and after",
                )
            except Exception as exc:
                report.error("Container identity comparison", exc)
    report.line("")
    if report.failures:
        report.line("RESULT: NOT READY - " + "; ".join(report.failures))
    else:
        report.line("RESULT: BASELINE CONNECTION CHECKS PASSED")
    report.line("Not tested: GitHub writes, sandbox commands, agent workflows, or recovery.")
    if not args.send_test_traces:
        report.line(
            "This mode only finds saved traces; use --send-test-traces to test sending now."
        )
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
