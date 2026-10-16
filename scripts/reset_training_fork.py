"""Reset or recreate the training fork the control plane works on.

Run from the repo root; reads GITHUB_FORK and GITHUB_PAT from .env like the application:

    uv run python -B scripts/reset_training_fork.py --reset              # dry run
    uv run python -B scripts/reset_training_fork.py --reset --apply
    uv run python -B scripts/reset_training_fork.py --recreate --apply   # on a fresh fork

--reset keeps the fork and undoes the agent's work: it moves the default branch back to the
first commit that is not an agent commit (messages starting with "Agent change for #" or
"[agent]"), deletes agent/issue-* branches, closes open agent pull requests, reopens the seed
issues and removes the agent labels.

--recreate prepares a fresh fork: enables issues, ensures the four agent labels, renames the
default branch, and recreates the seed issues from the upstream repository in order, so they
get numbers 1-4. Steps the token cannot perform (enabling issues and renaming the branch
need the Administration permission) are printed as manual steps instead.

Nothing is changed without --apply. The token is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import httpx

sys.dont_write_bytecode = True

API_URL = "https://api.github.com"
AGENT_COMMIT_PREFIXES = ("Agent change for #", "[agent]")
AGENT_BRANCH_PREFIX = "agent/issue-"
LABELS = {
    "agent-ok": "0e8a16",
    "agent-claimed": "fbca04",
    "agent-approve": "1d76db",
    "agent-reject": "b60205",
}
AGENT_LABELS = ("agent-ok", "agent-claimed")
DEFAULT_SEEDS = (289, 299, 282, 301)  # upstream issue numbers, in fork order 1-4


def is_agent_commit(message: str) -> bool:
    return message.startswith(AGENT_COMMIT_PREFIXES)


def base_commit(commits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The newest commit that is not an agent commit, or None if every commit is one."""
    for commit in commits:
        if not is_agent_commit(commit["commit"]["message"]):
            return commit
    return None


@dataclass
class Plan:
    apply: bool
    actions: list[str]

    def do(self, text: str) -> None:
        self.actions.append(text)
        print(("APPLY  " if self.apply else "WOULD  ") + text)


class Fork:
    # Any: GitHub's JSON responses are untyped at this boundary.
    def __init__(self, http: httpx.AsyncClient, fork: str, plan: Plan) -> None:
        self.http = http
        self.fork = fork
        self.plan = plan

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.http.get(path, params=params)
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

    async def repo(self) -> Any:
        return await self.get(f"/repos/{self.fork}")

    async def ensure_labels(self) -> None:
        existing = {
            label["name"]: label["color"].lower()
            for label in await self.get(f"/repos/{self.fork}/labels", per_page=100)
        }
        for name, color in LABELS.items():
            if name not in existing:
                self.plan.do(f"create label {name} #{color}")
                await self.write("POST", f"/repos/{self.fork}/labels", name=name, color=color)
            elif existing[name] != color:
                self.plan.do(f"recolour label {name} to #{color}")
                await self.write("PATCH", f"/repos/{self.fork}/labels/{name}", color=color)

    async def ensure_issues_enabled(self, repo: Any) -> None:
        if repo.get("has_issues"):
            return
        self.plan.do("enable Issues on the fork (needs Administration permission)")
        try:
            await self.write("PATCH", f"/repos/{self.fork}", has_issues=True)
        except PermissionError:
            print("MANUAL Settings > General > Features > tick Issues")

    async def ensure_default_branch(self, repo: Any, wanted: str) -> None:
        current = repo["default_branch"]
        if current == wanted:
            return
        self.plan.do(f"rename default branch {current} -> {wanted} (needs Administration)")
        try:
            await self.write(
                "POST", f"/repos/{self.fork}/branches/{current}/rename", new_name=wanted
            )
        except PermissionError:
            print(f"MANUAL Settings > Branches > rename {current} to {wanted}")

    async def recreate_issues(self, upstream: str, seeds: tuple[int, ...]) -> None:
        existing = await self.get(f"/repos/{self.fork}/issues", state="all", per_page=100)
        if any("pull_request" not in item for item in existing):
            print("SKIP   the fork already has issues; use --reset to reopen them")
            return
        for number, upstream_number in enumerate(seeds, start=1):
            source = await self.get(f"/repos/{upstream}/issues/{upstream_number}")
            body = (source.get("body") or "").rstrip()
            body += (
                f"\n\n---\nRecreated for the coding-agent control plane from {source['html_url']}"
            )
            self.plan.do(f"create issue #{number}: {source['title']} (upstream #{upstream_number})")
            await self.write("POST", f"/repos/{self.fork}/issues", title=source["title"], body=body)

    async def reset_branch(self, branch: str) -> None:
        commits = await self.get(f"/repos/{self.fork}/commits", sha=branch, per_page=50)
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
            "PATCH", f"/repos/{self.fork}/git/refs/heads/{branch}", sha=base["sha"], force=True
        )

    async def delete_agent_branches(self) -> None:
        for item in await self.get(f"/repos/{self.fork}/branches", per_page=100):
            if item["name"].startswith(AGENT_BRANCH_PREFIX):
                self.plan.do(f"delete branch {item['name']}")
                await self.write("DELETE", f"/repos/{self.fork}/git/refs/heads/{item['name']}")

    async def close_agent_pulls(self) -> None:
        for pull in await self.get(f"/repos/{self.fork}/pulls", state="open", per_page=100):
            if pull["head"]["ref"].startswith(AGENT_BRANCH_PREFIX):
                self.plan.do(f"close PR #{pull['number']} {pull['title']}")
                await self.write(
                    "PATCH", f"/repos/{self.fork}/pulls/{pull['number']}", state="closed"
                )

    async def reopen_seed_issues(self, count: int) -> None:
        for number in range(1, count + 1):
            issue = await self.get(f"/repos/{self.fork}/issues/{number}")
            if "pull_request" in issue:
                continue
            labels = [label["name"] for label in issue["labels"]]
            if issue["state"] != "open":
                self.plan.do(f"reopen issue #{number}: {issue['title']}")
                await self.write("PATCH", f"/repos/{self.fork}/issues/{number}", state="open")
            for label in labels:
                if label in AGENT_LABELS or label == "agent-approve" or label == "agent-reject":
                    self.plan.do(f"remove label {label} from issue #{number}")
                    await self.write("DELETE", f"/repos/{self.fork}/issues/{number}/labels/{label}")


async def run(args: argparse.Namespace) -> int:
    from coding_agent_control_plane.config import get_settings

    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {settings.github_pat.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    plan = Plan(apply=args.apply, actions=[])
    async with httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=30) as http:
        fork = Fork(http, settings.github_fork, plan)
        repo = await fork.repo()
        upstream = args.upstream or (repo.get("parent") or {}).get("full_name")
        print(
            f"fork {settings.github_fork}; default branch {repo['default_branch']}; "
            f"upstream {upstream or 'unknown'}; mode {'APPLY' if args.apply else 'DRY RUN'}"
        )
        if args.recreate:
            if not upstream:
                raise SystemExit("--upstream is required when the fork has no parent")
            await fork.ensure_issues_enabled(repo)
            await fork.ensure_default_branch(repo, args.default_branch)
            await fork.ensure_labels()
            await fork.recreate_issues(upstream, tuple(args.seed))
        if args.reset:
            await fork.ensure_default_branch(repo, args.default_branch)
            branch = args.default_branch if plan.apply else repo["default_branch"]
            await fork.close_agent_pulls()
            await fork.reset_branch(branch)
            await fork.delete_agent_branches()
            await fork.ensure_labels()
            await fork.reopen_seed_issues(len(args.seed))
    print(f"{len(plan.actions)} action(s) {'applied' if plan.apply else 'planned; add --apply'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="undo the agent's work in place")
    parser.add_argument("--recreate", action="store_true", help="prepare a fresh fork")
    parser.add_argument("--apply", action="store_true", help="perform the changes")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--upstream", help="owner/repo; default: the fork's parent")
    parser.add_argument(
        "--seed",
        type=int,
        nargs="+",
        default=list(DEFAULT_SEEDS),
        help="upstream issue numbers in fork order (default: 289 299 282 301)",
    )
    args = parser.parse_args()
    if not (args.reset or args.recreate):
        parser.error("choose --reset or --recreate")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
