"""Set the stage for a demo: prepare the repository the control plane works on.

Run from the repository root; GITHUB_FORK, GITHUB_BASE_BRANCH and GITHUB_PAT come from .env
like the application. Nothing is changed without --apply; the token is never printed.

    uv run python -B scripts/prepare_demo_repo.py            # dry run: prints the plan
    uv run python -B scripts/prepare_demo_repo.py --apply    # performs it

One run does, in this order:

1. Repository: when GITHUB_FORK does not exist, fork --fork-from (default arun-0/boltons)
   into it, default branch only, and wait until GitHub has finished.
2. Settings: enable Issues, name the default branch after GITHUB_BASE_BRANCH and ensure the
   four agent labels. The first two need the token's Administration permission; without it
   the manual step is printed instead.
3. Branch: close open agent pull requests, move the default branch back to the newest commit
   that is not an agent commit (messages starting with "Agent change for #" or "[agent]")
   and delete the agent/issue-* branches.
4. Issues: delete every issue, open or closed. Pull requests are not issues; closed ones stay.
5. Seeds: recreate the seed issues from the source repository in order (default: upstream
   issues 289 299 282 301). GitHub never reuses an issue number, so each run gives the seeds
   new, higher numbers; the run ends by printing them.

Prerequisites, once. No gh CLI and no browser session are involved: the script calls the
GitHub API with the token from .env.

1. A GitHub account.
2. A fine-grained personal access token: github.com > Settings > Developer settings >
   Personal access tokens > Fine-grained tokens > Generate new token. Resource owner: your
   account. Repository access: All repositories (a repository that does not exist yet cannot
   be selected). Repository permissions, each "Read and write": Administration, Contents,
   Issues, Pull requests. Copy the token when it is shown; GitHub does not show it again.
3. uv: https://docs.astral.sh/uv/getting-started/installation/
4. From the repository root:

    cp .env.example .env   # set GITHUB_FORK=<your-login>/boltons and GITHUB_PAT=<the token>
    uv sync
    uv run python -B scripts/prepare_demo_repo.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

sys.dont_write_bytecode = True

API_URL = "https://api.github.com"
DEFAULT_FORK_FROM = "arun-0/boltons"
FORK_WAIT_S = 300  # GitHub calls a fork that takes longer than five minutes a support case
FORK_POLL_S = 5
AGENT_COMMIT_PREFIXES = ("Agent change for #", "[agent]")
AGENT_BRANCH_PREFIX = "agent/issue-"
LABELS = {
    "agent-ok": "0e8a16",
    "agent-claimed": "fbca04",
    "agent-approve": "1d76db",
    "agent-reject": "b60205",
}
DEFAULT_SEEDS = (289, 299, 282, 301)  # upstream issue numbers, in seed order
DELETE_ISSUE = "mutation($id: ID!) { deleteIssue(input: {issueId: $id}) { clientMutationId } }"


def is_agent_commit(message: str) -> bool:
    return message.startswith(AGENT_COMMIT_PREFIXES)


def base_commit(commits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest commit that is not an agent commit, or None if every commit is one."""
    for commit in commits:
        if not is_agent_commit(commit["commit"]["message"]):
            return commit
    return None


def seed_source(info: dict[str, Any]) -> str | None:
    """Where the seed issues live: the root of the fork network, else the parent, else None."""
    for key in ("source", "parent"):
        full_name = (info.get(key) or {}).get("full_name")
        if full_name:
            return str(full_name)
    return None


def fork_request(name: str, login: str) -> dict[str, Any]:
    """Body of the fork request that lands the repository at `name`, default branch only."""
    owner, repo = name.split("/", 1)
    body: dict[str, Any] = {"name": repo, "default_branch_only": True}
    if owner != login:
        body["organization"] = owner
    return body


def issues_only(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GitHub lists pull requests among issues; keep the real issues, oldest first."""
    issues = [item for item in items if "pull_request" not in item]
    return sorted(issues, key=lambda item: int(item["number"]))


@dataclass
class Plan:
    apply: bool
    actions: list[str]

    def do(self, text: str) -> None:
        self.actions.append(text)
        print(("APPLY  " if self.apply else "WOULD  ") + text)


class Repo:
    # Any: GitHub's JSON responses are untyped at this boundary.
    def __init__(self, http: httpx.AsyncClient, name: str, plan: Plan) -> None:
        self.http = http
        self.name = name  # owner/repo
        self.plan = plan

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def get_or_none(self, path: str) -> Any | None:
        response = await self.http.get(path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def write(self, method: str, path: str, **json: Any) -> Any:
        """Perform a write when --apply is set; raise a readable error on 403."""
        if not self.plan.apply:
            return None
        response = await self.http.request(method, path, json=json or None)
        if response.status_code == 403:
            raise PermissionError(
                f"{method} {path} refused: the token lacks the permission for this step"
            )
        response.raise_for_status()
        return response.json() if response.content else None

    async def graphql(self, query: str, **variables: Any) -> Any:
        """Run a GraphQL mutation when --apply is set; GraphQL reports refusals as errors."""
        if not self.plan.apply:
            return None
        response = await self.http.post("/graphql", json={"query": query, "variables": variables})
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            reasons = "; ".join(str(error.get("message", error)) for error in payload["errors"])
            raise PermissionError(f"GraphQL refused: {reasons}")
        return payload.get("data")

    async def ensure_exists(self, fork_from: str) -> Any | None:
        """The repository object, forking `fork_from` when it is missing; None in a dry run."""
        info = await self.get_or_none(f"/repos/{self.name}")
        if info is not None:
            return info
        login = (await self.get("/user"))["login"]
        self.plan.do(f"fork {fork_from} into {self.name} (default branch only)")
        if not self.plan.apply:
            return None
        await self.write("POST", f"/repos/{fork_from}/forks", **fork_request(self.name, login))
        deadline = time.monotonic() + FORK_WAIT_S
        while time.monotonic() < deadline:
            info = await self.get_or_none(f"/repos/{self.name}")
            if info is not None:
                branch = f"/repos/{self.name}/branches/{info['default_branch']}"
                if await self.get_or_none(branch) is not None:
                    return info
            await asyncio.sleep(FORK_POLL_S)
        raise RuntimeError(f"{self.name} is still not ready after {FORK_WAIT_S} s")

    async def ensure_labels(self) -> None:
        existing = {
            label["name"]: label["color"].lower()
            for label in await self.get(f"/repos/{self.name}/labels", per_page=100)
        }
        for name, color in LABELS.items():
            if name not in existing:
                self.plan.do(f"create label {name} #{color}")
                await self.write("POST", f"/repos/{self.name}/labels", name=name, color=color)
            elif existing[name] != color:
                self.plan.do(f"recolour label {name} to #{color}")
                await self.write("PATCH", f"/repos/{self.name}/labels/{name}", color=color)

    async def ensure_issues_enabled(self, info: Any) -> None:
        if info.get("has_issues"):
            return
        self.plan.do("enable Issues on the repository (needs Administration permission)")
        try:
            await self.write("PATCH", f"/repos/{self.name}", has_issues=True)
        except PermissionError:
            print("MANUAL Settings > General > Features > tick Issues")

    async def ensure_default_branch(self, info: Any, wanted: str) -> None:
        current = info["default_branch"]
        if current == wanted:
            return
        self.plan.do(f"rename default branch {current} -> {wanted} (needs Administration)")
        try:
            await self.write(
                "POST", f"/repos/{self.name}/branches/{current}/rename", new_name=wanted
            )
        except PermissionError:
            print(f"MANUAL Settings > Branches > rename {current} to {wanted}")

    async def reset_branch(self, branch: str) -> None:
        commits = await self.get(f"/repos/{self.name}/commits", sha=branch, per_page=50)
        agent_commits = [c for c in commits if is_agent_commit(c["commit"]["message"])]
        base = base_commit(commits)
        if base is None:
            raise RuntimeError("no non-agent commit in the last 50; refusing to guess")
        if not agent_commits or commits[0]["sha"] == base["sha"]:
            print(f"OK     {branch} has no agent commits on top")
            return
        for commit in agent_commits:
            print(f"       drop {commit['sha'][:7]} {commit['commit']['message'].splitlines()[0]}")
        self.plan.do(f"force {branch} back to {base['sha'][:7]} ({len(agent_commits)} commits)")
        await self.write(
            "PATCH", f"/repos/{self.name}/git/refs/heads/{branch}", sha=base["sha"], force=True
        )

    async def delete_agent_branches(self) -> None:
        for item in await self.get(f"/repos/{self.name}/branches", per_page=100):
            if item["name"].startswith(AGENT_BRANCH_PREFIX):
                self.plan.do(f"delete branch {item['name']}")
                await self.write("DELETE", f"/repos/{self.name}/git/refs/heads/{item['name']}")

    async def close_agent_pulls(self) -> None:
        for pull in await self.get(f"/repos/{self.name}/pulls", state="open", per_page=100):
            if pull["head"]["ref"].startswith(AGENT_BRANCH_PREFIX):
                self.plan.do(f"close PR #{pull['number']} {pull['title']}")
                await self.write(
                    "PATCH", f"/repos/{self.name}/pulls/{pull['number']}", state="closed"
                )

    async def all_issues(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self.get(
                f"/repos/{self.name}/issues", state="all", per_page=100, page=page
            )
            items.extend(batch)
            if len(batch) < 100:
                return issues_only(items)
            page += 1

    async def delete_all_issues(self) -> None:
        """Delete every issue, open or closed; only the repository's owning account may."""
        for issue in await self.all_issues():
            self.plan.do(f"delete {issue['state']} issue #{issue['number']}: {issue['title']}")
            await self.graphql(DELETE_ISSUE, id=issue["node_id"])

    async def recreate_issues(self, upstream: str, seeds: tuple[int, ...]) -> list[int]:
        """Create the seed issues from `upstream` in order; returns the numbers GitHub gave."""
        numbers: list[int] = []
        for upstream_number in seeds:
            source = await self.get(f"/repos/{upstream}/issues/{upstream_number}")
            body = (source.get("body") or "").rstrip()
            body += (
                f"\n\n---\nRecreated for the coding-agent control plane from {source['html_url']}"
            )
            self.plan.do(f"create issue from {upstream} #{upstream_number}: {source['title']}")
            created = await self.write(
                "POST", f"/repos/{self.name}/issues", title=source["title"], body=body
            )
            if created:
                numbers.append(int(created["number"]))
        return numbers


async def run(args: argparse.Namespace) -> int:
    from coding_agent_control_plane.config import get_settings

    settings = get_settings()
    default_branch = args.default_branch or settings.github_base_branch
    headers = {
        "Authorization": f"Bearer {settings.github_pat.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    plan = Plan(apply=args.apply, actions=[])
    numbers: list[int] = []
    async with httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=30) as http:
        repo = Repo(http, settings.github_fork, plan)
        print(
            f"repository {settings.github_fork}; base branch {default_branch}; "
            f"mode {'APPLY' if args.apply else 'DRY RUN'}"
        )
        info = await repo.ensure_exists(args.fork_from)
        if info is None:
            print("STOP   the remaining steps are planned once the repository exists; add --apply")
            return 0
        upstream = args.upstream or seed_source(info)
        if not upstream:
            raise SystemExit("--upstream is required when the repository is not a fork")
        print(f"default branch {info['default_branch']}; seed source {upstream}")
        await repo.ensure_issues_enabled(info)
        await repo.ensure_default_branch(info, default_branch)
        await repo.ensure_labels()
        branch = default_branch if plan.apply else info["default_branch"]
        await repo.close_agent_pulls()
        await repo.reset_branch(branch)
        await repo.delete_agent_branches()
        await repo.delete_all_issues()
        numbers = await repo.recreate_issues(upstream, tuple(args.seed))
    print(f"{len(plan.actions)} action(s) {'applied' if plan.apply else 'planned; add --apply'}")
    if numbers:
        print("seed issues: " + " ".join(f"#{number}" for number in numbers))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="perform the changes")
    parser.add_argument(
        "--fork-from",
        default=DEFAULT_FORK_FROM,
        help="owner/repo to fork when GITHUB_FORK does not exist (default: %(default)s)",
    )
    parser.add_argument("--default-branch", help="default: GITHUB_BASE_BRANCH from .env")
    parser.add_argument(
        "--upstream", help="owner/repo holding the seed issues; default: the fork network's source"
    )
    parser.add_argument(
        "--seed",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEEDS),
        help="upstream issue numbers in seed order (default: 289 299 282 301)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
