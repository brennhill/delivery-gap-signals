"""Core workflow analyzer — three-pass algorithm and report output."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..models import MergedChange
from .workflow_detect import (
    _build_window_profile,
    build_adaptive_windows,
    classify_pr_mechanism,
    compute_mechanism_rates,
    detect_transitions_coarse,
    refine_transition,
)
from .workflow_models import (
    PRWorkflowTag,
    Transition,
    WindowProfile,
    WorkflowProfile,
)
from .workflow_recommend import generate_recommendations


def analyze_workflow(
    changes: list[MergedChange],
    *,
    repo: str = "",
    window_size_days: int = 30,
    lookback_days: int = 90,
) -> WorkflowProfile:
    """Full workflow analysis with adaptive time windows.

    Three-pass algorithm:

    Pass 1 -- Coarse detection:
      Split changes into fixed windows (window_size_days).
      Compute mechanism rates for each.
      Identify candidate transitions (>20pp shift on any dimension).

    Pass 2 -- Binary search refinement:
      For each candidate transition, binary search within the window
      to find the inflection point to PR-level precision.

    Pass 3 -- Adaptive recomputation:
      Use transition timestamps as window boundaries.
      Recompute full profiles for each adaptive window.

    If no transitions detected, the entire period is one window.
    """
    if not changes:
        empty = _build_empty_window()
        recs = generate_recommendations(empty, [])
        return WorkflowProfile(
            repo=repo,
            lookback_days=lookback_days,
            windows=[empty],
            current=empty,
            transitions=[],
            recommendations=recs,
            pr_tags=[],
        )

    # Infer repo from first change if not provided
    if not repo:
        repo = changes[0].repo

    sorted_changes = sorted(changes, key=lambda c: c.merged_at)

    # --- Pass 1: Fixed windows ---
    fixed_windows, window_change_groups = _split_fixed_windows(
        sorted_changes, window_size_days
    )

    # --- Pass 2: Detect and refine transitions ---
    candidates = detect_transitions_coarse(fixed_windows)

    transitions: list[Transition] = []
    seen_dimensions: set[str] = set()

    for win_idx, dimension, before_rate, after_rate in candidates:
        if dimension in seen_dimensions:
            continue
        seen_dimensions.add(dimension)

        # Get changes spanning the two adjacent windows
        prev_changes = window_change_groups[win_idx - 1]
        curr_changes = window_change_groups[win_idx]
        combined = prev_changes + curr_changes

        transition = refine_transition(combined, dimension)
        transitions.append(transition)

    # --- Pass 3: Adaptive recomputation ---
    windows = build_adaptive_windows(sorted_changes, transitions)

    if not windows:
        # Fallback: single window
        windows = [_build_window_profile(
            sorted_changes,
            sorted_changes[0].merged_at.isoformat(),
            sorted_changes[-1].merged_at.isoformat(),
        )]

    current = windows[-1]  # Most recent window

    # --- Recommendations ---
    recommendations = generate_recommendations(current, transitions)

    # --- Per-PR tags ---
    pr_tags = _tag_prs(sorted_changes, windows)

    return WorkflowProfile(
        repo=repo,
        lookback_days=lookback_days,
        windows=windows,
        current=current,
        transitions=transitions,
        recommendations=recommendations,
        pr_tags=pr_tags,
    )


def _split_fixed_windows(
    sorted_changes: list[MergedChange],
    window_size_days: int,
) -> tuple[list[WindowProfile], list[list[MergedChange]]]:
    """Split changes into fixed-size time windows."""
    if not sorted_changes:
        return [], []

    start = sorted_changes[0].merged_at
    end = sorted_changes[-1].merged_at
    delta = timedelta(days=window_size_days)

    windows: list[WindowProfile] = []
    groups: list[list[MergedChange]] = []
    window_start = start

    while window_start <= end:
        window_end = window_start + delta
        group = [
            c for c in sorted_changes
            if window_start <= c.merged_at < window_end
        ]
        if group:
            wp = _build_window_profile(
                group,
                window_start.isoformat(),
                window_end.isoformat(),
            )
            windows.append(wp)
            groups.append(group)
        window_start = window_end

    # If last window missed the final PR (edge: merged_at == window_end)
    if not windows or (groups and sorted_changes[-1] not in groups[-1]):
        # Check for stragglers
        all_grouped = {id(c) for g in groups for c in g}
        stragglers = [c for c in sorted_changes if id(c) not in all_grouped]
        if stragglers:
            wp = _build_window_profile(
                stragglers,
                stragglers[0].merged_at.isoformat(),
                stragglers[-1].merged_at.isoformat(),
            )
            windows.append(wp)
            groups.append(stragglers)

    return windows, groups


def _tag_prs(
    sorted_changes: list[MergedChange],
    windows: list[WindowProfile],
) -> list[PRWorkflowTag]:
    """Tag each PR with its mechanism and active workflow window."""
    tags = []

    # Parse window boundaries
    window_bounds = []
    for w in windows:
        ws = datetime.fromisoformat(w.period_start)
        we = datetime.fromisoformat(w.period_end)
        window_bounds.append((ws, we, w))

    for c in sorted_changes:
        mechanism = classify_pr_mechanism(c)
        pr_num = c.pr_number or int(c.id)

        # Find the window this PR belongs to
        active_window = windows[-1]  # default to latest
        for ws, we, w in window_bounds:
            if ws <= c.merged_at <= we:
                active_window = w
                break

        window_label = f"{active_window.period_start} to {active_window.period_end}"

        tags.append(PRWorkflowTag(
            pr_number=pr_num,
            approval_mechanism=mechanism,
            workflow_window=window_label,
            active_workflow_type=active_window.workflow_type,
        ))

    return tags


def _build_empty_window() -> WindowProfile:
    """Build an empty WindowProfile for repos with no changes."""
    from .workflow_models import (
        MechanismRates,
        ParticipantProfile,
        ReviewDepth,
        TimingProfile,
    )

    return WindowProfile(
        period_start="",
        period_end="",
        mechanisms=MechanismRates(
            cr_approve=0.0, comment_approve=0.0, approve_only=0.0,
            label_based=0.0, no_review=0.0, self_approved=0.0,
            rubber_stamp=0.0, sample_size=0,
        ),
        depth=ReviewDepth(
            median_human_reviews=0.0, median_unique_reviewers=0.0,
            median_review_rounds=0.0, rubber_stamp_rate=0.0,
            substantive_review_rate=0.0,
        ),
        participants=ParticipantProfile(
            unique_reviewers=0, top_reviewers=[], reviewer_concentration=0.0,
            bot_reviewers=[],
        ),
        timing=TimingProfile(
            median_time_to_first_review_hours=None,
            median_ttm_by_mechanism={},
        ),
        workflow_type="mixed",
        sample_size=0,
    )


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

_MECHANISM_LABELS = {
    "cr_approve": "changes_requested \u2192 approve",
    "comment_approve": "commented \u2192 approve",
    "approve_only": "approve only",
    "rubber_stamp": "rubber stamp (<5min)",
    "self_approved": "self-approved",
    "label_based": "label-based",
    "no_review": "no review",
}


def print_workflow_report(profile: WorkflowProfile) -> str:
    """Human-readable workflow report for terminal output."""
    lines: list[str] = []
    c = profile.current
    total = c.sample_size

    header = f"Review Workflow Analysis: {profile.repo} ({profile.lookback_days} days, {total} PRs)"
    lines.append(header)
    lines.append("\u2500" * len(header))
    lines.append(f"Workflow type: {c.workflow_type}")
    lines.append("")

    # Approval mechanisms
    lines.append("Approval mechanisms:")
    mechanisms = [
        ("cr_approve", c.mechanisms.cr_approve),
        ("comment_approve", c.mechanisms.comment_approve),
        ("approve_only", c.mechanisms.approve_only),
        ("rubber_stamp", c.mechanisms.rubber_stamp),
        ("self_approved", c.mechanisms.self_approved),
        ("label_based", c.mechanisms.label_based),
        ("no_review", c.mechanisms.no_review),
    ]
    for key, rate in mechanisms:
        if rate > 0 or key in ("cr_approve", "comment_approve", "approve_only"):
            label = _MECHANISM_LABELS[key]
            count = round(rate * total)
            pct = round(rate * 100)
            lines.append(f"  {label + ':':<35} {pct:>3}%  ({count} PRs)")
    lines.append("")

    # Review depth
    lines.append("Review depth:")
    lines.append(f"  Median reviews/PR:     {c.depth.median_human_reviews:.1f}")
    lines.append(f"  Median reviewers/PR:   {c.depth.median_unique_reviewers:.1f}")
    lines.append(f"  Substantive review:    {round(c.depth.substantive_review_rate * 100)}%")
    lines.append(f"  Rubber stamp rate:     {round(c.depth.rubber_stamp_rate * 100):>3}%")
    lines.append("")

    # Top reviewers
    if c.participants.top_reviewers:
        lines.append("Top reviewers:")
        for r in c.participants.top_reviewers:
            lines.append(f"  {r.login:<18}{r.review_count} reviews")
        lines.append("")

    # Transitions
    if profile.transitions:
        lines.append("Transitions:")
        for t in profile.transitions:
            lines.append(f"  {t.timestamp}: {t.description}")
    else:
        lines.append("Transitions: none detected")
    lines.append("")

    # Recommendations
    rec = profile.recommendations
    lines.append(f"Recommendations for CATCHRATE:")
    lines.append(f"  Cycle method: {rec.catchrate_cycle_method}")
    lines.append(f"  HSR signals: {', '.join(rec.catchrate_signals)}")
    for w in rec.catchrate_warnings:
        lines.append(f"  \u26a0 {w}")
    if not rec.catchrate_warnings:
        lines.append("  \u2713 Standard detection is appropriate")

    if rec.upfront_warnings:
        lines.append(f"Recommendations for UPFRONT:")
        lines.append(f"  Friction baseline: {rec.upfront_friction_baseline}")
        for w in rec.upfront_warnings:
            lines.append(f"  \u26a0 {w}")

    return "\n".join(lines)
