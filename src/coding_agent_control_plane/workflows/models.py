"""Payloads that cross the Temporal workflow/activity boundary."""

from enum import StrEnum

from pydantic import BaseModel


class Review(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class IssueRef(BaseModel):
    number: int
    title: str


class SandboxResult(BaseModel):
    ok: bool
    output_tail: str


class AgentRequest(BaseModel):
    issue: IssueRef


class AgentResult(BaseModel):
    ok: bool
    summary: str
    turns: int
    langfuse_trace_id: str
    api_equivalent_cost_usd: float | None


class SuiteResult(BaseModel):
    passed: bool
    exit_code: int
    output_tail: str


class PullRequestRequest(BaseModel):
    issue: IssueRef
    agent: AgentResult
    suite: SuiteResult


class OpenedPull(BaseModel):
    number: int
    url: str


class ReviewAction(BaseModel):
    pull: int
    decision: Review


class OutcomeReport(BaseModel):
    issue: int
    message: str


class IssueOutcome(BaseModel):
    issue: int
    pull: int | None
    decision: Review | None
    message: str


class PollSummary(BaseModel):
    started: list[int]
    signalled: list[int]
