"""Recommendation engine for tool configuration based on workflow profile."""

from __future__ import annotations

from .workflow_models import ToolRecommendations, Transition, WindowProfile


def generate_recommendations(
    current: WindowProfile,
    transitions: list[Transition],
) -> ToolRecommendations:
    """Generate tool configuration recommendations.

    Rules:
    - If comment_approve > 40%: recommend comment-based HSR detection
    - If rubber_stamp > 30%: warn MCR may include unreviewed PRs
    - If label_based > 30%: warn about label-based approval detection gap
    - If transition detected: warn about mixed-period numbers
    - If approve_only > 60%: warn friction metrics may undercount
    """
    m = current.mechanisms
    catchrate_signals: list[str] = []
    catchrate_warnings: list[str] = []
    upfront_warnings: list[str] = []

    # Determine HSR signals
    if m.cr_approve > 0.1:
        catchrate_signals.append("changes_requested")
    if m.comment_approve > 0.1:
        catchrate_signals.append("comment_then_commit")
        catchrate_signals.append("comment_then_approve")
    if not catchrate_signals:
        catchrate_signals.append("changes_requested")

    # Determine cycle method
    if m.comment_approve > 0.4:
        cycle_method = "comment_approve"
    elif m.cr_approve > 0.4:
        cycle_method = "cr_only"
    else:
        cycle_method = "combined"

    # Rubber stamp warning
    rs_pct = round(m.rubber_stamp * 100)
    if m.rubber_stamp > 0.30:
        catchrate_warnings.append(
            f"{rs_pct}% rubber stamp rate \u2014 MCR may include unreviewed PRs"
        )

    # Label-based warning
    lb_pct = round(m.label_based * 100)
    if m.label_based > 0.30:
        catchrate_warnings.append(
            f"{lb_pct}% label-based approval \u2014 HSR detection may miss label/bot approvals"
        )

    # Transition warning
    if transitions:
        catchrate_warnings.append(
            f"{len(transitions)} workflow transition(s) detected \u2014 "
            "per-PR classification recommended over aggregate rates"
        )

    # Approve-only warning for upfront
    ao_pct = round(m.approve_only * 100)
    if m.approve_only > 0.60:
        upfront_warnings.append(
            f"{ao_pct}% approve-only \u2014 friction metrics may undercount review effort"
        )

    # Determine friction baseline
    if m.comment_approve > 0.4:
        friction_baseline = "comment_approve TTM"
    elif m.cr_approve > 0.4:
        friction_baseline = "cr_approve TTM"
    else:
        friction_baseline = "overall TTM"

    return ToolRecommendations(
        catchrate_signals=catchrate_signals,
        catchrate_cycle_method=cycle_method,
        catchrate_warnings=catchrate_warnings,
        upfront_friction_baseline=friction_baseline,
        upfront_warnings=upfront_warnings,
    )
