"""Agent-commit detection for the fork reset script: pure functions, no network."""

from scripts.reset_training_fork import base_commit, is_agent_commit


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
