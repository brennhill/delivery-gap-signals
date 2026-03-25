"""Data models for workflow analysis.

All models are frozen dataclasses except WorkflowProfile (which accumulates results).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MechanismRates:
    """Approval mechanism distribution for a time window."""
    cr_approve: float          # 0.0-1.0
    comment_approve: float
    approve_only: float
    label_based: float
    no_review: float
    self_approved: float
    rubber_stamp: float
    sample_size: int

    def to_dict(self) -> dict:
        return {
            "cr_approve": self.cr_approve,
            "comment_approve": self.comment_approve,
            "approve_only": self.approve_only,
            "label_based": self.label_based,
            "no_review": self.no_review,
            "self_approved": self.self_approved,
            "rubber_stamp": self.rubber_stamp,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class ReviewDepth:
    median_human_reviews: float
    median_unique_reviewers: float
    median_review_rounds: float
    rubber_stamp_rate: float
    substantive_review_rate: float

    def to_dict(self) -> dict:
        return {
            "median_human_reviews": self.median_human_reviews,
            "median_unique_reviewers": self.median_unique_reviewers,
            "median_review_rounds": self.median_review_rounds,
            "rubber_stamp_rate": self.rubber_stamp_rate,
            "substantive_review_rate": self.substantive_review_rate,
        }


@dataclass(frozen=True)
class ReviewerInfo:
    login: str
    review_count: int
    role: str  # "human", "ci_bot", "review_bot", "label_bot"

    def to_dict(self) -> dict:
        return {
            "login": self.login,
            "review_count": self.review_count,
            "role": self.role,
        }


@dataclass(frozen=True)
class ParticipantProfile:
    unique_reviewers: int
    top_reviewers: list[ReviewerInfo]
    reviewer_concentration: float  # gini
    bot_reviewers: list[ReviewerInfo]

    def to_dict(self) -> dict:
        return {
            "unique_reviewers": self.unique_reviewers,
            "top_reviewers": [r.to_dict() for r in self.top_reviewers],
            "reviewer_concentration": self.reviewer_concentration,
            "bot_reviewers": [r.to_dict() for r in self.bot_reviewers],
        }


@dataclass(frozen=True)
class TimingProfile:
    median_time_to_first_review_hours: float | None
    median_ttm_by_mechanism: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "median_time_to_first_review_hours": self.median_time_to_first_review_hours,
            "median_ttm_by_mechanism": dict(self.median_ttm_by_mechanism),
        }


@dataclass(frozen=True)
class WindowProfile:
    """Full profile for a single time window."""
    period_start: str  # ISO 8601
    period_end: str    # ISO 8601
    mechanisms: MechanismRates
    depth: ReviewDepth
    participants: ParticipantProfile
    timing: TimingProfile
    workflow_type: str
    sample_size: int

    def to_dict(self) -> dict:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "mechanisms": self.mechanisms.to_dict(),
            "depth": self.depth.to_dict(),
            "participants": self.participants.to_dict(),
            "timing": self.timing.to_dict(),
            "workflow_type": self.workflow_type,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True)
class Transition:
    """Detected workflow shift between windows."""
    timestamp: str              # ISO 8601
    last_old_pr: int            # PR number of last PR in old workflow
    first_new_pr: int           # PR number of first PR in new workflow
    description: str
    dimension: str
    before_value: float
    after_value: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "last_old_pr": self.last_old_pr,
            "first_new_pr": self.first_new_pr,
            "description": self.description,
            "dimension": self.dimension,
            "before_value": self.before_value,
            "after_value": self.after_value,
        }


@dataclass(frozen=True)
class ToolRecommendations:
    catchrate_signals: list[str]
    catchrate_cycle_method: str
    catchrate_warnings: list[str]
    upfront_friction_baseline: str
    upfront_warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "catchrate_signals": list(self.catchrate_signals),
            "catchrate_cycle_method": self.catchrate_cycle_method,
            "catchrate_warnings": list(self.catchrate_warnings),
            "upfront_friction_baseline": self.upfront_friction_baseline,
            "upfront_warnings": list(self.upfront_warnings),
        }


@dataclass(frozen=True)
class PRWorkflowTag:
    """Per-PR workflow context."""
    pr_number: int
    approval_mechanism: str
    workflow_window: str
    active_workflow_type: str

    def to_dict(self) -> dict:
        return {
            "pr_number": self.pr_number,
            "approval_mechanism": self.approval_mechanism,
            "workflow_window": self.workflow_window,
            "active_workflow_type": self.active_workflow_type,
        }


@dataclass
class WorkflowProfile:
    """Complete workflow analysis for a repo."""
    repo: str
    lookback_days: int
    windows: list[WindowProfile]
    current: WindowProfile
    transitions: list[Transition]
    recommendations: ToolRecommendations
    pr_tags: list[PRWorkflowTag]

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "lookback_days": self.lookback_days,
            "windows": [w.to_dict() for w in self.windows],
            "current": self.current.to_dict(),
            "transitions": [t.to_dict() for t in self.transitions],
            "recommendations": self.recommendations.to_dict(),
            "pr_tags": [t.to_dict() for t in self.pr_tags],
        }
