"""Pure detector functions for workflow analysis. No I/O."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from ..models import MergedChange, Review
from .workflow_models import (
    MechanismRates,
    ParticipantProfile,
    ReviewDepth,
    ReviewerInfo,
    TimingProfile,
    Transition,
    WindowProfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BOT_REVIEWERS = {
    "copilot", "github-copilot", "copilot-pull-request-reviewer",
    "copilot-swe-agent", "coderabbitai", "codium-ai",
    "sourcery-ai", "ellipsis-dev", "greptile-bot", "pantheon-ai",
    "promptfoo-scanner", "cubic-dev-ai", "devin-ai-integration",
}
_BOT_PREFIXES = ("copilot-", "coderabbit-", "sourcery-", "pantheon-", "devin-")


def _is_bot(review: Review) -> bool:
    """Check if a reviewer is a bot."""
    low = review.reviewer.lower()
    return (review.is_bot or review.reviewer.endswith("[bot]")
            or low in _BOT_REVIEWERS or low.startswith(_BOT_PREFIXES))


def _human_reviews(change: MergedChange) -> list[Review]:
    """Filter to human reviews only."""
    if not change.reviews:
        return []
    return [r for r in change.reviews if not _is_bot(r)]


def _sorted_by_time(reviews: list[Review]) -> list[Review]:
    return sorted(reviews, key=lambda r: r.submitted_at)


# ---------------------------------------------------------------------------
# B1: Approval Mechanism Detection
# ---------------------------------------------------------------------------

def classify_pr_mechanism(change: MergedChange) -> str:
    """Classify a single PR's approval mechanism.

    Priority: cr_approve > comment_approve > rubber_stamp > approve_only
              > self_approved > label_based > no_review
    """
    reviews = change.reviews or []

    # No reviews at all from any source
    if not reviews:
        return "no_review"

    human = _human_reviews(change)

    # Check cr_approve: has CHANGES_REQUESTED followed by APPROVED from humans
    human_sorted = _sorted_by_time(human)
    has_cr = any(r.state == "changes_requested" for r in human)
    has_approved = any(r.state == "approved" for r in human)

    if has_cr and has_approved:
        # Verify CR came before at least one APPROVED
        cr_times = [r.submitted_at for r in human_sorted if r.state == "changes_requested"]
        approve_times = [r.submitted_at for r in human_sorted if r.state == "approved"]
        if cr_times and approve_times and min(cr_times) < max(approve_times):
            return "cr_approve"

    # Check comment_approve: COMMENTED -> commits -> APPROVED
    has_comment = any(r.state == "commented" for r in human)
    if has_comment and has_approved:
        comment_times = [r.submitted_at for r in human_sorted if r.state == "commented"]
        approve_times = [r.submitted_at for r in human_sorted if r.state == "approved"]
        if comment_times and approve_times and min(comment_times) < max(approve_times):
            return "comment_approve"

    # Check rubber_stamp: approved within 5 min of PR creation, 0 prior comments
    if has_approved and change.created_at is not None:
        first_approve = min(r.submitted_at for r in human if r.state == "approved")
        time_delta = (first_approve - change.created_at).total_seconds()
        prior_comments = [
            r for r in human_sorted
            if r.state in ("commented", "changes_requested")
            and r.submitted_at < first_approve
        ]
        if time_delta <= 300 and len(prior_comments) == 0:
            return "rubber_stamp"

    # Check approve_only: APPROVED with no prior COMMENTED or CHANGES_REQUESTED
    if has_approved:
        has_prior_feedback = any(
            r.state in ("commented", "changes_requested") for r in human
        )
        if not has_prior_feedback:
            return "approve_only"

    # Check self_approved: only reviewer is the PR author
    human_reviewers = {r.reviewer for r in human}
    if human_reviewers == {change.author}:
        return "self_approved"

    # label_based: no APPROVED from humans but PR merged
    if not has_approved:
        return "label_based"

    # no_review fallback (shouldn't normally reach here)
    return "no_review"


def compute_mechanism_rates(changes: list[MergedChange]) -> MechanismRates:
    """Compute mechanism distribution across a set of changes."""
    if not changes:
        return MechanismRates(
            cr_approve=0.0, comment_approve=0.0, approve_only=0.0,
            label_based=0.0, no_review=0.0, self_approved=0.0,
            rubber_stamp=0.0, sample_size=0,
        )

    counts: Counter[str] = Counter()
    for c in changes:
        mechanism = classify_pr_mechanism(c)
        counts[mechanism] += 1

    n = len(changes)
    return MechanismRates(
        cr_approve=counts["cr_approve"] / n,
        comment_approve=counts["comment_approve"] / n,
        approve_only=counts["approve_only"] / n,
        label_based=counts["label_based"] / n,
        no_review=counts["no_review"] / n,
        self_approved=counts["self_approved"] / n,
        rubber_stamp=counts["rubber_stamp"] / n,
        sample_size=n,
    )


# ---------------------------------------------------------------------------
# B2: Review Depth
# ---------------------------------------------------------------------------

def _count_review_rounds(change: MergedChange) -> int:
    """Count review round-trips (back-and-forth cycles).

    A round = reviewer submits feedback, author responds (new commits),
    reviewer reviews again. Approximated by counting state transitions
    in review timeline.
    """
    human = _human_reviews(change)
    if not human:
        return 0

    sorted_reviews = _sorted_by_time(human)
    rounds = 0
    saw_feedback = False
    for r in sorted_reviews:
        if r.state in ("changes_requested", "commented"):
            if not saw_feedback:
                rounds += 1
                saw_feedback = True
        elif r.state == "approved":
            saw_feedback = False  # reset for potential next round

    return max(rounds, 1) if sorted_reviews else 0


def compute_review_depth(changes: list[MergedChange]) -> ReviewDepth:
    """Compute review depth metrics."""
    if not changes:
        return ReviewDepth(
            median_human_reviews=0.0,
            median_unique_reviewers=0.0,
            median_review_rounds=0.0,
            rubber_stamp_rate=0.0,
            substantive_review_rate=0.0,
        )

    human_review_counts = []
    unique_reviewer_counts = []
    round_counts = []
    rubber_stamp_count = 0
    substantive_count = 0

    for c in changes:
        human = _human_reviews(c)
        human_review_counts.append(len(human))
        unique_reviewer_counts.append(len({r.reviewer for r in human}))

        rounds = _count_review_rounds(c)
        round_counts.append(rounds)

        mechanism = classify_pr_mechanism(c)
        if mechanism == "rubber_stamp":
            rubber_stamp_count += 1

        # Substantive: 2+ review rounds OR 3+ human review submissions
        if rounds >= 2 or len(human) >= 3:
            substantive_count += 1

    n = len(changes)
    return ReviewDepth(
        median_human_reviews=statistics.median(human_review_counts),
        median_unique_reviewers=statistics.median(unique_reviewer_counts),
        median_review_rounds=statistics.median(round_counts),
        rubber_stamp_rate=rubber_stamp_count / n,
        substantive_review_rate=substantive_count / n,
    )


# ---------------------------------------------------------------------------
# B3: Participant Analysis
# ---------------------------------------------------------------------------

def compute_gini(counts: list[int]) -> float:
    """Gini coefficient for reviewer concentration."""
    if not counts or sum(counts) == 0:
        return 0.0
    sorted_counts = sorted(counts)
    n = len(sorted_counts)
    total = sum(sorted_counts)
    numerator = sum((2 * i - n + 1) * c for i, c in enumerate(sorted_counts))
    return numerator / (n * total)


def _classify_bot_role(reviews: list[Review]) -> str:
    """Classify a bot's role from its review states."""
    states = {r.state for r in reviews}
    if "approved" in states:
        return "review_bot"
    if "commented" in states:
        return "label_bot"
    return "ci_bot"


def compute_participant_profile(changes: list[MergedChange]) -> ParticipantProfile:
    """Analyze reviewer distribution and bot ecosystem."""
    human_counts: Counter[str] = Counter()
    bot_reviews: dict[str, list[Review]] = {}

    for c in changes:
        for r in (c.reviews or []):
            if _is_bot(r):
                bot_reviews.setdefault(r.reviewer, []).append(r)
            else:
                human_counts[r.reviewer] += 1

    # Top reviewers (top 5 by count)
    top = [
        ReviewerInfo(login=login, review_count=count, role="human")
        for login, count in human_counts.most_common(5)
    ]

    # Gini on human reviewer counts
    gini = compute_gini(list(human_counts.values()))

    # Bot reviewers
    bots = [
        ReviewerInfo(
            login=login,
            review_count=len(revs),
            role=_classify_bot_role(revs),
        )
        for login, revs in sorted(bot_reviews.items(), key=lambda x: -len(x[1]))
    ]

    return ParticipantProfile(
        unique_reviewers=len(human_counts),
        top_reviewers=top,
        reviewer_concentration=gini,
        bot_reviewers=bots,
    )


# ---------------------------------------------------------------------------
# B4: Timing Profile
# ---------------------------------------------------------------------------

def compute_timing_profile(changes: list[MergedChange]) -> TimingProfile:
    """Analyze review timing patterns."""
    first_review_hours: list[float] = []
    ttm_by_mechanism: dict[str, list[float]] = {}

    for c in changes:
        mechanism = classify_pr_mechanism(c)

        # Time to merge (hours)
        if c.created_at is not None:
            ttm_hours = (c.merged_at - c.created_at).total_seconds() / 3600
            ttm_by_mechanism.setdefault(mechanism, []).append(ttm_hours)

        # Time to first human review
        human = _human_reviews(c)
        if human and c.created_at is not None:
            first = min(r.submitted_at for r in human)
            delta_hours = (first - c.created_at).total_seconds() / 3600
            first_review_hours.append(delta_hours)

    median_first = (
        statistics.median(first_review_hours) if first_review_hours else None
    )
    median_ttm = {
        mech: statistics.median(times)
        for mech, times in ttm_by_mechanism.items()
        if times
    }

    return TimingProfile(
        median_time_to_first_review_hours=median_first,
        median_ttm_by_mechanism=median_ttm,
    )


# ---------------------------------------------------------------------------
# B7: Workflow Type Classification
# ---------------------------------------------------------------------------

def classify_workflow_type(
    mechanisms: MechanismRates,
    bot_reviewers: list[ReviewerInfo] | None = None,
    total_reviews: int = 0,
) -> str:
    """Classify the overall workflow type from mechanism rates."""
    # minimal-review check first
    minimal = mechanisms.no_review + mechanisms.rubber_stamp + mechanisms.self_approved
    if minimal > 0.5:
        return "minimal-review"

    if mechanisms.cr_approve > 0.4:
        return "github-native"
    if mechanisms.comment_approve > 0.4:
        return "comment-driven"
    if mechanisms.approve_only > 0.6:
        return "approve-direct"
    if mechanisms.label_based > 0.3:
        return "label-based"

    # bot-reviewed: review bots submit >20% of all reviews
    if bot_reviewers and total_reviews > 0:
        bot_review_count = sum(
            b.review_count for b in bot_reviewers if b.role == "review_bot"
        )
        if bot_review_count / total_reviews > 0.2:
            return "bot-reviewed"

    return "mixed"


# ---------------------------------------------------------------------------
# B6: Windowed Analysis
# ---------------------------------------------------------------------------

def _build_window_profile(
    changes: list[MergedChange],
    period_start: str,
    period_end: str,
) -> WindowProfile:
    """Build a full WindowProfile from a slice of changes."""
    mechanisms = compute_mechanism_rates(changes)
    depth = compute_review_depth(changes)
    participants = compute_participant_profile(changes)
    timing = compute_timing_profile(changes)

    total_reviews = sum(len(c.reviews or []) for c in changes)
    wf_type = classify_workflow_type(
        mechanisms, participants.bot_reviewers, total_reviews
    )

    return WindowProfile(
        period_start=period_start,
        period_end=period_end,
        mechanisms=mechanisms,
        depth=depth,
        participants=participants,
        timing=timing,
        workflow_type=wf_type,
        sample_size=len(changes),
    )


def detect_transitions_coarse(
    windows: list[WindowProfile],
) -> list[tuple[int, str, float, float]]:
    """Pass 1: detect candidate transitions between adjacent fixed windows.

    Returns (window_index, dimension, before_rate, after_rate) for shifts >20pp.
    window_index is the index of the SECOND window in the pair.
    """
    dimensions = [
        "cr_approve", "comment_approve", "approve_only",
        "label_based", "no_review", "self_approved", "rubber_stamp",
    ]
    transitions = []

    for i in range(1, len(windows)):
        prev = windows[i - 1].mechanisms
        curr = windows[i].mechanisms
        for dim in dimensions:
            before = getattr(prev, dim)
            after = getattr(curr, dim)
            if abs(after - before) > 0.20:
                transitions.append((i, dim, before, after))

    return transitions


def refine_transition(
    changes: list[MergedChange],
    dimension: str,
) -> Transition:
    """Pass 2: binary search within a set of changes to find the transition point.

    Recurses until sub-window is <=10 PRs or delta <10pp.
    """
    sorted_changes = sorted(changes, key=lambda c: c.merged_at)

    def _get_rate(subset: list[MergedChange]) -> float:
        if not subset:
            return 0.0
        rates = compute_mechanism_rates(subset)
        return getattr(rates, dimension)

    def _search(prs: list[MergedChange]) -> tuple[int, int]:
        """Return (last_old_index, first_new_index) in the prs list."""
        if len(prs) <= 10:
            # Find the split point that maximizes the delta
            best_split = len(prs) // 2
            best_delta = 0.0
            for s in range(1, len(prs)):
                left_rate = _get_rate(prs[:s])
                right_rate = _get_rate(prs[s:])
                delta = abs(right_rate - left_rate)
                if delta > best_delta:
                    best_delta = delta
                    best_split = s
            return best_split - 1, best_split

        mid = len(prs) // 2
        left_rate = _get_rate(prs[:mid])
        right_rate = _get_rate(prs[mid:])

        full_left = _get_rate(prs[: len(prs) // 4])
        full_right = _get_rate(prs[3 * len(prs) // 4 :])

        # Determine which half contains the transition
        left_delta = abs(_get_rate(prs[:mid // 2]) - _get_rate(prs[mid // 2:mid]))
        right_delta = abs(_get_rate(prs[mid:mid + (len(prs) - mid) // 2]) -
                         _get_rate(prs[mid + (len(prs) - mid) // 2:]))

        if left_delta >= right_delta:
            result = _search(prs[:mid])
        else:
            idx_old, idx_new = _search(prs[mid:])
            result = (mid + idx_old, mid + idx_new)

        return result

    overall_left = _get_rate(sorted_changes[:len(sorted_changes) // 2])
    overall_right = _get_rate(sorted_changes[len(sorted_changes) // 2:])

    if abs(overall_right - overall_left) < 0.10 or len(sorted_changes) < 2:
        # Not a real transition, but return best guess
        mid = len(sorted_changes) // 2
        idx_old, idx_new = max(0, mid - 1), min(mid, len(sorted_changes) - 1)
    else:
        idx_old, idx_new = _search(sorted_changes)

    # Clamp indices
    idx_old = max(0, min(idx_old, len(sorted_changes) - 1))
    idx_new = max(0, min(idx_new, len(sorted_changes) - 1))
    if idx_new <= idx_old:
        idx_new = min(idx_old + 1, len(sorted_changes) - 1)

    last_old = sorted_changes[idx_old]
    first_new = sorted_changes[idx_new]

    # Midpoint timestamp
    ts_old = last_old.merged_at
    ts_new = first_new.merged_at
    midpoint = ts_old + (ts_new - ts_old) / 2

    before_rate = _get_rate(sorted_changes[:idx_new])
    after_rate = _get_rate(sorted_changes[idx_new:])

    return Transition(
        timestamp=midpoint.isoformat(),
        last_old_pr=last_old.pr_number or int(last_old.id),
        first_new_pr=first_new.pr_number or int(first_new.id),
        description=f"{dimension} shifted from {before_rate:.0%} to {after_rate:.0%}",
        dimension=dimension,
        before_value=before_rate,
        after_value=after_rate,
    )


def build_adaptive_windows(
    changes: list[MergedChange],
    transitions: list[Transition],
) -> list[WindowProfile]:
    """Pass 3: recompute profiles using transition timestamps as boundaries."""
    if not changes:
        return []

    sorted_changes = sorted(changes, key=lambda c: c.merged_at)

    if not transitions:
        return [_build_window_profile(
            sorted_changes,
            sorted_changes[0].merged_at.isoformat(),
            sorted_changes[-1].merged_at.isoformat(),
        )]

    # Parse transition timestamps and sort
    boundaries = sorted(
        datetime.fromisoformat(t.timestamp) for t in transitions
    )

    windows = []
    remaining = list(sorted_changes)

    for boundary in boundaries:
        before = [c for c in remaining if c.merged_at <= boundary]
        remaining = [c for c in remaining if c.merged_at > boundary]

        if before:
            windows.append(_build_window_profile(
                before,
                before[0].merged_at.isoformat(),
                before[-1].merged_at.isoformat(),
            ))

    # Last window
    if remaining:
        windows.append(_build_window_profile(
            remaining,
            remaining[0].merged_at.isoformat(),
            remaining[-1].merged_at.isoformat(),
        ))

    return windows
