"""Pure functions of the demo setup script: no network."""

from scripts.prepare_demo_repo import (
    base_commit,
    fork_request,
    is_agent_commit,
    issues_only,
    seed_source,
)


def _commit(sha: str, message: str) -> dict[str, object]:
    return {"sha": sha, "commit": {"message": message}}


def test_agent_commit_messages_are_recognised() -> None:
    assert is_agent_commit("Agent change for #2: Proposal: shorten long string (#9)")
    assert is_agent_commit("[agent] typename (#1)")
    assert not is_agent_commit("Fix spooled stream write compatibility (#513)")
    assert not is_agent_commit("Revert agent change for #2")


def test_base_is_the_newest_non_agent_commit() -> None:
    commits = [
        _commit("a1", "Agent change for #2: x (#9)"),
        _commit("a2", "Agent change for #4: y (#8)"),
        _commit("u1", "Fix spooled stream write compatibility (#513)"),
        _commit("u0", "bump version"),
    ]
    base = base_commit(commits)
    assert base is not None
    assert base["sha"] == "u1"


def test_no_agent_commits_means_head_is_the_base() -> None:
    commits = [_commit("u1", "upstream work"), _commit("u0", "older")]
    base = base_commit(commits)
    assert base is not None
    assert base["sha"] == "u1"


def test_all_agent_commits_means_no_base() -> None:
    assert base_commit([_commit("a1", "[agent] only (#1)")]) is None


def test_seed_source_is_the_root_of_the_fork_network() -> None:
    fork_of_a_fork = {
        "parent": {"full_name": "arun-0/boltons"},
        "source": {"full_name": "mahmoud/boltons"},
    }
    assert seed_source(fork_of_a_fork) == "mahmoud/boltons"
    assert seed_source({"parent": {"full_name": "mahmoud/boltons"}}) == "mahmoud/boltons"
    assert seed_source({"parent": None}) is None


def test_fork_request_lands_in_the_own_account_or_an_organisation() -> None:
    assert fork_request("nephew/boltons", "nephew") == {
        "name": "boltons",
        "default_branch_only": True,
    }
    assert fork_request("demo-org/boltons-demo", "nephew") == {
        "name": "boltons-demo",
        "default_branch_only": True,
        "organization": "demo-org",
    }


def test_issues_only_drops_pull_requests_and_orders_oldest_first() -> None:
    items = [
        {"number": 3, "state": "closed"},
        {"number": 5, "pull_request": {"url": "x"}},
        {"number": 1, "state": "open"},
    ]
    assert [item["number"] for item in issues_only(items)] == [1, 3]
