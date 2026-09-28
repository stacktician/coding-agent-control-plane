"""GitHub REST calls the control plane makes itself, outside the agent.

Phase W: one fine-grained PAT scoped to the fork. The agent never holds this
client; it reaches GitHub only through the MCP gateway.
"""

import base64
from typing import Any

import httpx
from pydantic import BaseModel

from coding_agent_control_plane.config import Settings
from coding_agent_control_plane.sandbox.containers import FileChange

API_URL = "https://api.github.com"
AGENT_BRANCH_PREFIX = "agent/issue-"


class Issue(BaseModel):
    number: int
    title: str
    body: str
    labels: list[str]


class AgentPull(BaseModel):
    number: int
    issue: int
    labels: list[str]


class PullRequest(BaseModel):
    number: int
    url: str


def agent_branch(issue: int) -> str:
    return f"{AGENT_BRANCH_PREFIX}{issue}"


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self._repo = settings.github_fork
        self._owner = settings.github_fork.split("/")[0]
        self._base_branch = settings.github_base_branch
        self._headers = {
            "Authorization": f"Bearer {settings.github_pat.get_secret_value()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # Any: GitHub's JSON responses are untyped at this boundary.
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=API_URL, headers=self._headers, timeout=30) as http:
            response = await http.request(method, f"/repos/{self._repo}{path}", **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

    @staticmethod
    def _issue(raw: dict[str, Any]) -> Issue:
        return Issue(
            number=raw["number"],
            title=raw["title"],
            body=raw.get("body") or "",
            labels=[label["name"] for label in raw["labels"]],
        )

    async def get_issue(self, number: int) -> Issue:
        return self._issue(await self._request("GET", f"/issues/{number}"))

    async def list_open_issues(self, label: str) -> list[Issue]:
        raw = await self._request(
            "GET", "/issues", params={"state": "open", "labels": label, "per_page": 100}
        )
        return [self._issue(item) for item in raw if "pull_request" not in item]

    async def comment(self, number: int, body: str) -> None:
        await self._request("POST", f"/issues/{number}/comments", json={"body": body})

    async def add_labels(self, number: int, labels: list[str]) -> None:
        await self._request("POST", f"/issues/{number}/labels", json={"labels": labels})

    async def remove_label(self, number: int, label: str) -> None:
        try:
            await self._request("DELETE", f"/issues/{number}/labels/{label}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    async def list_agent_pulls(self) -> list[AgentPull]:
        raw = await self._request("GET", "/pulls", params={"state": "open", "per_page": 100})
        pulls = []
        for item in raw:
            ref: str = item["head"]["ref"]
            suffix = ref.removeprefix(AGENT_BRANCH_PREFIX)
            if ref.startswith(AGENT_BRANCH_PREFIX) and suffix.isdigit():
                pulls.append(
                    AgentPull(
                        number=item["number"],
                        issue=int(suffix),
                        labels=[label["name"] for label in item["labels"]],
                    )
                )
        return pulls

    async def publish_branch(
        self, branch: str, parent_sha: str, message: str, changes: list[FileChange]
    ) -> str:
        """Commit full-file changes on top of parent_sha and point branch at it.

        Uses the Git Data API, so no credential ever enters the sandbox.
        Re-running replaces the branch (force), which keeps retries idempotent.
        """
        parent = await self._request("GET", f"/git/commits/{parent_sha}")
        entries = []
        for change in changes:
            entry: dict[str, Any] = {"path": change.path, "mode": "100644", "type": "blob"}
            if change.content is None:
                entry["sha"] = None
            else:
                blob = await self._request(
                    "POST",
                    "/git/blobs",
                    json={
                        "content": base64.b64encode(change.content).decode(),
                        "encoding": "base64",
                    },
                )
                entry["sha"] = blob["sha"]
            entries.append(entry)
        tree = await self._request(
            "POST", "/git/trees", json={"base_tree": parent["tree"]["sha"], "tree": entries}
        )
        commit = await self._request(
            "POST",
            "/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        try:
            await self._request(
                "PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"], "force": True}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:  # 422: the ref does not exist yet
                raise
            await self._request(
                "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]}
            )
        sha: str = commit["sha"]
        return sha

    async def open_or_update_pull(self, branch: str, title: str, body: str) -> PullRequest:
        existing = await self._request(
            "GET", "/pulls", params={"state": "open", "head": f"{self._owner}:{branch}"}
        )
        if existing:
            number = existing[0]["number"]
            raw = await self._request("PATCH", f"/pulls/{number}", json={"body": body})
        else:
            raw = await self._request(
                "POST",
                "/pulls",
                json={"title": title, "head": branch, "base": self._base_branch, "body": body},
            )
        return PullRequest(number=raw["number"], url=raw["html_url"])

    async def merge_pull(self, number: int) -> None:
        pull = await self._request("GET", f"/pulls/{number}")
        if pull["merged"]:
            return
        await self._request("PUT", f"/pulls/{number}/merge", json={"merge_method": "squash"})

    async def close_pull(self, number: int) -> None:
        await self._request("PATCH", f"/pulls/{number}", json={"state": "closed"})
