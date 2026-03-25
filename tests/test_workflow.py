"""Tests for workflow analyzer: mechanism classification, gini, windowing, transitions."""

import unittest
from datetime import datetime, timedelta, timezone

from delivery_gap_signals.models import MergedChange, Review
from delivery_gap_signals.analysis.workflow_detect import (
    classify_pr_mechanism,
    compute_gini,
    compute_mechanism_rates,
    compute_review_depth,
    compute_participant_profile,
    compute_timing_profile,
    classify_workflow_type,
    detect_transitions_coarse,
    refine_transition,
    build_adaptive_windows,
)
from delivery_gap_signals.analysis.workflow_models import MechanismRates, WindowProfile
from delivery_gap_signals.analysis.workflow_recommend import generate_recommendations
from delivery_gap_signals.analysis.workflow import (
    analyze_workflow,
    print_workflow_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def _make_change(
    pr_num: int,
    author: str = "alice",
    reviews: list[Review] | None = None,
    created_at: datetime | None = None,
    merged_at: datetime | None = None,
) -> MergedChange:
    """Build a minimal MergedChange for testing."""
    if created_at is None:
        created_at = T0
    if merged_at is None:
        merged_at = created_at + timedelta(hours=4)
    return MergedChange(
        id=str(pr_num),
        source="github",
        repo="test/repo",
        title=f"PR #{pr_num}",
        body="",
        author=author,
        merged_at=merged_at,
        created_at=created_at,
        reviews=reviews,
        pr_number=pr_num,
    )


def _review(
    reviewer: str,
    state: str,
    offset_minutes: int = 60,
    is_bot: bool = False,
) -> Review:
    return Review(
        reviewer=reviewer,
        state=state,
        submitted_at=T0 + timedelta(minutes=offset_minutes),
        is_bot=is_bot,
    )


# ===================================================================
# B1: Mechanism Classification
# ===================================================================


class TestClassifyPRMechanism(unittest.TestCase):

    def test_no_review(self):
        """PR with zero reviews -> no_review."""
        c = _make_change(1, reviews=[])
        self.assertEqual(classify_pr_mechanism(c), "no_review")

    def test_no_review_none(self):
        """PR with reviews=None -> no_review."""
        c = _make_change(1, reviews=None)
        self.assertEqual(classify_pr_mechanism(c), "no_review")

    def test_cr_approve(self):
        """CHANGES_REQUESTED then APPROVED -> cr_approve."""
        c = _make_change(1, reviews=[
            _review("bob", "changes_requested", 30),
            _review("bob", "approved", 120),
        ])
        self.assertEqual(classify_pr_mechanism(c), "cr_approve")

    def test_comment_approve(self):
        """COMMENTED then APPROVED -> comment_approve."""
        c = _make_change(1, reviews=[
            _review("bob", "commented", 30),
            _review("bob", "approved", 120),
        ])
        self.assertEqual(classify_pr_mechanism(c), "comment_approve")

    def test_rubber_stamp(self):
        """Approved within 5 min, no prior comments -> rubber_stamp."""
        c = _make_change(
            1,
            created_at=T0,
            reviews=[
                Review(
                    reviewer="bob",
                    state="approved",
                    submitted_at=T0 + timedelta(minutes=3),
                    is_bot=False,
                ),
            ],
        )
        self.assertEqual(classify_pr_mechanism(c), "rubber_stamp")

    def test_approve_only(self):
        """Approved with no prior feedback, but after 5 min -> approve_only."""
        c = _make_change(
            1,
            created_at=T0,
            reviews=[
                Review(
                    reviewer="bob",
                    state="approved",
                    submitted_at=T0 + timedelta(minutes=30),
                    is_bot=False,
                ),
            ],
        )
        self.assertEqual(classify_pr_mechanism(c), "approve_only")

    def test_self_approved(self):
        """Only reviewer is the author -> self_approved."""
        c = _make_change(1, author="alice", reviews=[
            _review("alice", "commented", 30),
        ])
        self.assertEqual(classify_pr_mechanism(c), "self_approved")

    def test_label_based(self):
        """Bot reviews only, no human approved -> label_based."""
        c = _make_change(1, reviews=[
            _review("prow[bot]", "commented", 30, is_bot=True),
        ])
        self.assertEqual(classify_pr_mechanism(c), "label_based")

    def test_cr_beats_comment(self):
        """CR + comment + approve -> cr_approve wins (priority)."""
        c = _make_change(1, reviews=[
            _review("bob", "changes_requested", 10),
            _review("carol", "commented", 20),
            _review("bob", "approved", 120),
        ])
        self.assertEqual(classify_pr_mechanism(c), "cr_approve")

    def test_bot_reviews_excluded(self):
        """Bot reviews don't count as human reviews."""
        c = _make_change(1, reviews=[
            _review("coderabbit[bot]", "approved", 10, is_bot=True),
        ])
        self.assertEqual(classify_pr_mechanism(c), "label_based")


# ===================================================================
# Gini coefficient
# ===================================================================


class TestGini(unittest.TestCase):

    def test_perfect_equality(self):
        self.assertAlmostEqual(compute_gini([10, 10, 10, 10]), 0.0)

    def test_perfect_inequality(self):
        self.assertAlmostEqual(compute_gini([0, 0, 0, 100]), 0.75)

    def test_empty(self):
        self.assertEqual(compute_gini([]), 0.0)

    def test_all_zeros(self):
        self.assertEqual(compute_gini([0, 0, 0]), 0.0)

    def test_single_element(self):
        self.assertEqual(compute_gini([5]), 0.0)

    def test_two_elements_unequal(self):
        # [1, 3] -> sorted [1, 3], n=2, total=4
        # numerator = (2*0 - 2 + 1)*1 + (2*1 - 2 + 1)*3 = (-1)*1 + (1)*3 = 2
        # gini = 2 / (2 * 4) = 0.25
        self.assertAlmostEqual(compute_gini([1, 3]), 0.25)


# ===================================================================
# Mechanism Rates
# ===================================================================


class TestMechanismRates(unittest.TestCase):

    def test_empty_changes(self):
        rates = compute_mechanism_rates([])
        self.assertEqual(rates.sample_size, 0)
        self.assertEqual(rates.cr_approve, 0.0)

    def test_distribution(self):
        changes = [
            _make_change(1, reviews=[
                _review("bob", "changes_requested", 10),
                _review("bob", "approved", 60),
            ]),
            _make_change(2, reviews=[
                _review("bob", "commented", 10),
                _review("bob", "approved", 60),
            ]),
            _make_change(3, reviews=[]),
            _make_change(4, reviews=[
                Review("bob", "approved", T0 + timedelta(minutes=30), False),
            ]),
        ]
        rates = compute_mechanism_rates(changes)
        self.assertEqual(rates.sample_size, 4)
        self.assertAlmostEqual(rates.cr_approve, 0.25)
        self.assertAlmostEqual(rates.comment_approve, 0.25)
        self.assertAlmostEqual(rates.no_review, 0.25)
        self.assertAlmostEqual(rates.approve_only, 0.25)


# ===================================================================
# Review Depth
# ===================================================================


class TestReviewDepth(unittest.TestCase):

    def test_empty(self):
        depth = compute_review_depth([])
        self.assertEqual(depth.median_human_reviews, 0.0)

    def test_basic_depth(self):
        changes = [
            _make_change(1, reviews=[
                _review("bob", "commented", 10),
                _review("bob", "approved", 60),
            ]),
            _make_change(2, reviews=[
                _review("bob", "changes_requested", 10),
                _review("carol", "commented", 20),
                _review("bob", "approved", 120),
            ]),
        ]
        depth = compute_review_depth(changes)
        self.assertEqual(depth.median_human_reviews, 2.5)
        self.assertEqual(depth.median_unique_reviewers, 1.5)


# ===================================================================
# Participant Profile
# ===================================================================


class TestParticipantProfile(unittest.TestCase):

    def test_basic(self):
        changes = [
            _make_change(1, reviews=[
                _review("bob", "approved", 60),
                _review("carol", "approved", 70),
            ]),
            _make_change(2, reviews=[
                _review("bob", "approved", 60),
            ]),
        ]
        profile = compute_participant_profile(changes)
        self.assertEqual(profile.unique_reviewers, 2)
        self.assertEqual(len(profile.top_reviewers), 2)
        self.assertEqual(profile.top_reviewers[0].login, "bob")
        self.assertEqual(profile.top_reviewers[0].review_count, 2)

    def test_bot_detection(self):
        changes = [
            _make_change(1, reviews=[
                _review("coderabbit[bot]", "approved", 10, is_bot=True),
                _review("bob", "approved", 60),
            ]),
        ]
        profile = compute_participant_profile(changes)
        self.assertEqual(profile.unique_reviewers, 1)
        self.assertEqual(len(profile.bot_reviewers), 1)
        self.assertEqual(profile.bot_reviewers[0].login, "coderabbit[bot]")
        self.assertEqual(profile.bot_reviewers[0].role, "review_bot")


# ===================================================================
# Workflow Type Classification
# ===================================================================


class TestClassifyWorkflowType(unittest.TestCase):

    def test_github_native(self):
        rates = MechanismRates(
            cr_approve=0.52, comment_approve=0.20, approve_only=0.10,
            label_based=0.0, no_review=0.08, self_approved=0.05,
            rubber_stamp=0.05, sample_size=100,
        )
        self.assertEqual(classify_workflow_type(rates), "github-native")

    def test_comment_driven(self):
        rates = MechanismRates(
            cr_approve=0.05, comment_approve=0.55, approve_only=0.20,
            label_based=0.05, no_review=0.05, self_approved=0.05,
            rubber_stamp=0.05, sample_size=100,
        )
        self.assertEqual(classify_workflow_type(rates), "comment-driven")

    def test_minimal_review(self):
        rates = MechanismRates(
            cr_approve=0.05, comment_approve=0.05, approve_only=0.10,
            label_based=0.10, no_review=0.30, self_approved=0.10,
            rubber_stamp=0.30, sample_size=100,
        )
        self.assertEqual(classify_workflow_type(rates), "minimal-review")

    def test_mixed(self):
        rates = MechanismRates(
            cr_approve=0.30, comment_approve=0.30, approve_only=0.20,
            label_based=0.05, no_review=0.05, self_approved=0.05,
            rubber_stamp=0.05, sample_size=100,
        )
        self.assertEqual(classify_workflow_type(rates), "mixed")


# ===================================================================
# Transition Detection
# ===================================================================


class TestTransitionDetection(unittest.TestCase):

    def _make_window(self, cr: float, comment: float, **kw) -> WindowProfile:
        from delivery_gap_signals.analysis.workflow_models import (
            MechanismRates, ReviewDepth, ParticipantProfile, TimingProfile,
            ReviewerInfo,
        )
        remainder = 1.0 - cr - comment
        return WindowProfile(
            period_start="2026-01-01T00:00:00+00:00",
            period_end="2026-01-31T00:00:00+00:00",
            mechanisms=MechanismRates(
                cr_approve=cr, comment_approve=comment,
                approve_only=max(0, remainder),
                label_based=0.0, no_review=0.0,
                self_approved=0.0, rubber_stamp=0.0,
                sample_size=kw.get("sample_size", 30),
            ),
            depth=ReviewDepth(0, 0, 0, 0, 0),
            participants=ParticipantProfile(0, [], 0.0, []),
            timing=TimingProfile(None, {}),
            workflow_type="mixed",
            sample_size=kw.get("sample_size", 30),
        )

    def test_no_transition(self):
        windows = [
            self._make_window(0.50, 0.20),
            self._make_window(0.48, 0.22),
        ]
        result = detect_transitions_coarse(windows)
        self.assertEqual(len(result), 0)

    def test_detects_transition(self):
        windows = [
            self._make_window(0.50, 0.10),
            self._make_window(0.10, 0.50),
        ]
        result = detect_transitions_coarse(windows)
        # Should detect both cr_approve and comment_approve shifts
        dims = {r[1] for r in result}
        self.assertIn("cr_approve", dims)
        self.assertIn("comment_approve", dims)


# ===================================================================
# Windowing and Adaptive Windows
# ===================================================================


class TestAdaptiveWindows(unittest.TestCase):

    def test_no_transitions_single_window(self):
        changes = [
            _make_change(i, merged_at=T0 + timedelta(days=i))
            for i in range(10)
        ]
        windows = build_adaptive_windows(changes, [])
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].sample_size, 10)

    def test_empty_changes(self):
        windows = build_adaptive_windows([], [])
        self.assertEqual(len(windows), 0)


# ===================================================================
# Full Analyzer
# ===================================================================


class TestAnalyzeWorkflow(unittest.TestCase):

    def test_empty_changes(self):
        profile = analyze_workflow([], repo="test/empty")
        self.assertEqual(profile.repo, "test/empty")
        self.assertEqual(len(profile.windows), 1)
        self.assertEqual(profile.current.sample_size, 0)

    def test_basic_analysis(self):
        changes = []
        for i in range(20):
            changes.append(_make_change(
                i + 1,
                merged_at=T0 + timedelta(days=i),
                reviews=[
                    _review("bob", "changes_requested", 10),
                    _review("bob", "approved", 120),
                ],
            ))
        profile = analyze_workflow(changes, repo="test/repo", lookback_days=30)
        self.assertEqual(profile.repo, "test/repo")
        self.assertGreater(len(profile.windows), 0)
        self.assertEqual(profile.current.workflow_type, "github-native")
        self.assertEqual(len(profile.pr_tags), 20)

    def test_to_dict_serializable(self):
        """WorkflowProfile.to_dict() produces JSON-serializable output."""
        import json
        changes = [
            _make_change(1, reviews=[
                _review("bob", "approved", 30),
            ]),
        ]
        profile = analyze_workflow(changes, repo="test/repo")
        d = profile.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        self.assertIn("test/repo", serialized)

    def test_transition_detection_end_to_end(self):
        """Build changes with a clear workflow shift and verify detection."""
        changes = []
        # First 20 PRs: all cr_approve
        for i in range(20):
            changes.append(_make_change(
                i + 1,
                merged_at=T0 + timedelta(days=i),
                reviews=[
                    _review("bob", "changes_requested", 10),
                    _review("bob", "approved", 120),
                ],
            ))
        # Next 20 PRs: all no_review
        for i in range(20, 40):
            changes.append(_make_change(
                i + 1,
                merged_at=T0 + timedelta(days=i),
                reviews=[],
            ))
        profile = analyze_workflow(changes, repo="test/transition", lookback_days=60)
        # Should detect at least one transition
        self.assertGreater(len(profile.transitions), 0)
        # Should have multiple windows
        self.assertGreater(len(profile.windows), 1)


# ===================================================================
# Report Output
# ===================================================================


class TestPrintWorkflowReport(unittest.TestCase):

    def test_report_output(self):
        changes = [
            _make_change(i + 1, merged_at=T0 + timedelta(days=i), reviews=[
                _review("bob", "changes_requested", 10),
                _review("bob", "approved", 120),
            ])
            for i in range(10)
        ]
        profile = analyze_workflow(changes, repo="cli/cli", lookback_days=90)
        report = print_workflow_report(profile)
        self.assertIn("cli/cli", report)
        self.assertIn("Workflow type:", report)
        self.assertIn("Approval mechanisms:", report)
        self.assertIn("Review depth:", report)


# ===================================================================
# Recommendations
# ===================================================================


class TestRecommendations(unittest.TestCase):

    def test_comment_driven_recommendations(self):
        from delivery_gap_signals.analysis.workflow_models import (
            ReviewDepth, ParticipantProfile, TimingProfile,
        )
        window = WindowProfile(
            period_start="2026-01-01T00:00:00+00:00",
            period_end="2026-03-01T00:00:00+00:00",
            mechanisms=MechanismRates(
                cr_approve=0.05, comment_approve=0.55, approve_only=0.20,
                label_based=0.05, no_review=0.05, self_approved=0.05,
                rubber_stamp=0.05, sample_size=100,
            ),
            depth=ReviewDepth(2.0, 1.0, 1.0, 0.05, 0.60),
            participants=ParticipantProfile(5, [], 0.3, []),
            timing=TimingProfile(1.5, {}),
            workflow_type="comment-driven",
            sample_size=100,
        )
        recs = generate_recommendations(window, [])
        self.assertEqual(recs.catchrate_cycle_method, "comment_approve")
        self.assertIn("comment_then_commit", recs.catchrate_signals)
        self.assertEqual(recs.upfront_friction_baseline, "comment_approve TTM")

    def test_high_rubber_stamp_warning(self):
        from delivery_gap_signals.analysis.workflow_models import (
            ReviewDepth, ParticipantProfile, TimingProfile,
        )
        window = WindowProfile(
            period_start="2026-01-01T00:00:00+00:00",
            period_end="2026-03-01T00:00:00+00:00",
            mechanisms=MechanismRates(
                cr_approve=0.30, comment_approve=0.10, approve_only=0.10,
                label_based=0.05, no_review=0.05, self_approved=0.05,
                rubber_stamp=0.35, sample_size=100,
            ),
            depth=ReviewDepth(2.0, 1.0, 1.0, 0.35, 0.30),
            participants=ParticipantProfile(5, [], 0.3, []),
            timing=TimingProfile(1.5, {}),
            workflow_type="mixed",
            sample_size=100,
        )
        recs = generate_recommendations(window, [])
        self.assertTrue(any("rubber stamp" in w for w in recs.catchrate_warnings))


if __name__ == "__main__":
    unittest.main()
