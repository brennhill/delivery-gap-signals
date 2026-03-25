"""Analysis modules for delivery pipeline intelligence.

These run AFTER data is fetched and provide diagnostic context
for interpreting metrics from CATCHRATE and UPFRONT.
"""

from .workflow import analyze_workflow, print_workflow_report
from .workflow_detect import (
    build_adaptive_windows,
    classify_pr_mechanism,
    classify_workflow_type,
    compute_gini,
    compute_mechanism_rates,
    compute_participant_profile,
    compute_review_depth,
    compute_timing_profile,
    detect_transitions_coarse,
    refine_transition,
)
from .workflow_models import (
    MechanismRates,
    PRWorkflowTag,
    ParticipantProfile,
    ReviewDepth,
    ReviewerInfo,
    TimingProfile,
    ToolRecommendations,
    Transition,
    WindowProfile,
    WorkflowProfile,
)
from .workflow_recommend import generate_recommendations

__all__ = [
    "analyze_workflow",
    "build_adaptive_windows",
    "classify_pr_mechanism",
    "classify_workflow_type",
    "compute_gini",
    "compute_mechanism_rates",
    "compute_participant_profile",
    "compute_review_depth",
    "compute_timing_profile",
    "detect_transitions_coarse",
    "generate_recommendations",
    "MechanismRates",
    "ParticipantProfile",
    "PRWorkflowTag",
    "print_workflow_report",
    "refine_transition",
    "ReviewDepth",
    "ReviewerInfo",
    "TimingProfile",
    "ToolRecommendations",
    "Transition",
    "WindowProfile",
    "WorkflowProfile",
]
