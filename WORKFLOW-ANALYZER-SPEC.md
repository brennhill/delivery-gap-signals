# Workflow Analyzer — Product & Technical Spec

## Problem

CATCHRATE and UPFRONT produce metrics that are only meaningful in context. A repo showing 0% machine catch rate might be a failure (no automation) or a misclassification (Prow-based approval that CATCHRATE can't see). A repo showing 30% rework on spec'd PRs might mean specs are bad or that specs give reviewers something to reject against.

Without understanding HOW a repo reviews code, the numbers are uninterpretable.

We discovered this running the 30-repo study:
- **cli/cli**: Uses `CHANGES_REQUESTED` → 0% MCR, 96% HSR. Correct.
- **kubernetes**: Uses Prow comments + `/lgtm` labels → showed 73% MCR (wrong). After comment-based detection fix → 31% MCR, 68% HSR. Correct.
- **cockroachdb**: Uses `COMMENTED` → `APPROVED`, almost never `CHANGES_REQUESTED` → showed 0 review cycles (wrong). Real review friction is 6.9h vs 2.4h.

Three repos, three different workflows, three misclassifications. There will be more.

## What It Does

The Workflow Analyzer examines `list[MergedChange]` and produces a `WorkflowProfile` that:

1. **Identifies the review workflow** — how PRs get approved in this repo
2. **Detects time-windowed changes** — workflow shifts mid-period
3. **Provides per-PR classification context** — which workflow was active when each PR merged
4. **Recommends tool configuration** — how CATCHRATE/UPFRONT should interpret this repo
5. **Flags reliability concerns** — "rubber stamp rate is 40%, HSR may overcount"

## Behavior Spec

### B1: Approval Mechanism Detection

**Given** a set of merged PRs with review data, **When** the analyzer runs, **Then** it classifies each PR into one of these approval mechanisms:

| Mechanism | Detection Rule |
|-----------|---------------|
| `cr_approve` | PR has `CHANGES_REQUESTED` from a human reviewer, followed by `APPROVED` |
| `comment_approve` | PR has `COMMENTED` from a human reviewer, followed by commits, followed by `APPROVED` (same or different reviewer) |
| `approve_only` | PR has `APPROVED` from a human reviewer with no prior `COMMENTED` or `CHANGES_REQUESTED` from any human |
| `label_based` | PR has no `APPROVED` or `CHANGES_REQUESTED` but was merged (implies label/bot-based approval). Detected by: merged PR with 0 `APPROVED` reviews from humans |
| `no_review` | PR has zero reviews from any source (human or bot) |
| `self_approved` | Only reviewer is the PR author |
| `rubber_stamp` | `APPROVED` within 5 minutes of PR creation with 0 prior comments. Quick approval with no evidence of review |

Each PR gets exactly one classification. Priority order (first match wins): `cr_approve` > `comment_approve` > `rubber_stamp` > `approve_only` > `self_approved` > `label_based` > `no_review`.

The profile reports the percentage of PRs in each category.

### B2: Review Depth

**Given** classified PRs, **When** depth is computed, **Then** report:

- `median_human_reviews_per_pr` — count of human review submissions (any state)
- `median_unique_reviewers_per_pr` — distinct human reviewers per PR
- `median_review_rounds` — back-and-forth cycles (using CATCHRATE's cycle detection)
- `rubber_stamp_rate` — % of PRs approved in <5 min with 0 prior comments
- `substantive_review_rate` — % of PRs with 2+ review rounds OR 3+ human review submissions

### B3: Participant Analysis

**Given** review data, **When** participants are analyzed, **Then** report:

- `unique_reviewers` — total distinct human reviewers in the period
- `top_reviewers` — top 5 reviewers by review count, with count
- `reviewer_concentration` — Gini coefficient (0.0 = evenly distributed, 1.0 = one person reviews everything)
- `bot_reviewers` — list of detected bot reviewers with review counts and their role (`ci`, `review`, `label`)

### B4: Timing Patterns

**Given** PR creation, review, and merge timestamps, **When** timing is analyzed, **Then** report:

- `median_time_to_first_review_hours` — time from PR creation to first human review
- `median_ttm_by_mechanism` — time-to-merge broken down by approval mechanism
- `review_hours_distribution` — what hours of day reviews happen (detect timezone patterns)

### B5: Bot Ecosystem Detection

**Given** review and CI data, **When** bot detection runs, **Then** classify bots into:

- `ci_bots` — bots that only appear in CI check runs (not in reviews)
- `review_bots` — bots that submit `COMMENTED` or `APPROVED` reviews (CodeRabbit, Copilot reviewer)
- `label_bots` — bots that manage labels/approvals (Prow, Bors, Mergify)

Detection: any reviewer where `is_bot=True` or username ends in `[bot]`. Classification by behavior: if the bot submits `APPROVED`, it's a review bot. If only `COMMENTED`, it's a label/review bot. If only in CI checks, it's a CI bot.

### B6: Adaptive Time-Windowed Profiles

**Given** PRs spanning a lookback period, **When** windowed analysis runs, **Then** use a three-pass algorithm:

**Pass 1 — Coarse detection:** Split PRs into fixed windows (default: monthly). Compute the full profile (B1-B5) for each window. Identify candidate transitions where any mechanism's rate changes by >20 percentage points between adjacent windows.

**Pass 2 — Binary search refinement:** For each candidate transition, binary search within the month to find the inflection point. Split the window in half, compute mechanism rates for each half, determine which half contains the shift, recurse until the window is ≤10 PRs or the mechanism rate delta within the sub-window is <10pp. The transition point is identified to the individual PR: the last PR exhibiting the old workflow and the first PR exhibiting the new workflow. The transition timestamp is the midpoint between those two PRs' merge timestamps (full ISO 8601, not just date).

**Pass 3 — Adaptive recomputation:** Replace fixed calendar windows with data-driven windows using the detected transition timestamps as boundaries. Recompute the full profile (B1-B5) for each adaptive window.

```
Fixed windows:     |--- Jan ---|--- Feb ---|--- Mar ---|
Pass 1 detects:                    ^ shift somewhere in Feb
Pass 2 narrows:                    ^ between PR #4521 (14:32Z) and #4523 (15:01Z)
Adaptive windows:  |--- Jan 1 to Feb 12 14:46Z ---|--- Feb 12 14:47Z to Mar 24 ---|
```

If no transitions are detected, the entire lookback period is one window.

**Given** a stable repo with no workflow changes, **When** windowed analysis runs, **Then** produce a single window covering the full lookback period (no unnecessary splitting).

**Given** a repo with multiple transitions, **When** windowed analysis runs, **Then** produce N+1 windows for N transitions. Each window has its own full profile and workflow type classification.

### B7: Workflow Type Classification

**Given** the mechanism percentages, **When** classification runs, **Then** assign one of:

| Type | Condition |
|------|-----------|
| `github-native` | `cr_approve` > 40% |
| `comment-driven` | `comment_approve` > 40% |
| `approve-direct` | `approve_only` > 60% |
| `label-based` | `label_based` > 30% |
| `bot-reviewed` | review bots submit >20% of all reviews |
| `mixed` | no single mechanism > 40% |
| `minimal-review` | `no_review` + `rubber_stamp` + `self_approved` > 50% |

### B8: Tool Configuration Recommendations

**Given** the workflow profile, **When** recommendations are generated, **Then** produce:

```
recommendations:
  catchrate:
    human_save_signals: ["changes_requested", "comment_then_commit", "comment_then_approve"]
    review_cycle_method: "comment_approve"  # or "cr_only" or "combined"
    warnings: ["40% rubber stamp rate — MCR may include unreviewed PRs"]
  upfront:
    friction_baseline: "comment_approve TTM"  # which TTM to use as friction signal
    warnings: ["86% approve-only — friction metrics may undercount review effort"]
```

### B9: Per-PR Workflow Tag

**Given** time-windowed profiles, **When** a PR is classified, **Then** tag it with:

- `workflow_window` — which time window it belongs to
- `approval_mechanism` — which mechanism was used for this specific PR
- `active_workflow_type` — the workflow type that was active when this PR merged

This allows CATCHRATE to apply the correct classification logic per-PR when the workflow shifts mid-period.

---

## Technical Architecture

### Module Location

```
delivery_gap_signals/
  analysis/
    __init__.py
    workflow.py          # Core analyzer
    workflow_models.py   # Data models (WorkflowProfile, etc.)
    workflow_detect.py   # Individual detector functions
    workflow_recommend.py # Tool config recommendations
```

### Data Models

```python
# workflow_models.py

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

@dataclass(frozen=True)
class ReviewDepth:
    median_human_reviews: float
    median_unique_reviewers: float
    median_review_rounds: float
    rubber_stamp_rate: float
    substantive_review_rate: float

@dataclass(frozen=True)
class ReviewerInfo:
    login: str
    review_count: int
    role: str  # "human", "ci_bot", "review_bot", "label_bot"

@dataclass(frozen=True)
class ParticipantProfile:
    unique_reviewers: int
    top_reviewers: list[ReviewerInfo]
    reviewer_concentration: float  # gini
    bot_reviewers: list[ReviewerInfo]

@dataclass(frozen=True)
class TimingProfile:
    median_time_to_first_review_hours: float | None
    median_ttm_by_mechanism: dict[str, float]

@dataclass(frozen=True)
class WindowProfile:
    """Full profile for a single time window. Boundaries are data-driven, not calendar-based."""
    period_start: str  # ISO 8601 timestamp (not just date)
    period_end: str    # ISO 8601 timestamp
    mechanisms: MechanismRates
    depth: ReviewDepth
    participants: ParticipantProfile
    timing: TimingProfile
    workflow_type: str  # classified type
    sample_size: int

@dataclass(frozen=True)
class Transition:
    """Detected workflow shift between windows."""
    timestamp: str              # ISO 8601 — midpoint between last old-workflow PR and first new-workflow PR
    last_old_pr: int            # PR number of last PR in old workflow
    first_new_pr: int           # PR number of first PR in new workflow
    description: str            # human-readable: "cr_approve dropped from 45% to 5%"
    dimension: str              # which metric changed (e.g. "cr_approve")
    before_value: float         # rate in window before transition
    after_value: float          # rate in window after transition

@dataclass(frozen=True)
class ToolRecommendations:
    catchrate_signals: list[str]
    catchrate_cycle_method: str
    catchrate_warnings: list[str]
    upfront_friction_baseline: str
    upfront_warnings: list[str]

@dataclass(frozen=True)
class PRWorkflowTag:
    """Per-PR workflow context."""
    pr_number: int
    approval_mechanism: str
    workflow_window: str  # period label
    active_workflow_type: str

@dataclass
class WorkflowProfile:
    """Complete workflow analysis for a repo."""
    repo: str
    lookback_days: int
    windows: list[WindowProfile]
    current: WindowProfile         # most recent window
    transitions: list[Transition]
    recommendations: ToolRecommendations
    pr_tags: list[PRWorkflowTag]   # per-PR mechanism classification
```

### Detector Functions

```python
# workflow_detect.py — pure functions, no I/O

def classify_pr_mechanism(change: MergedChange) -> str:
    """Classify a single PR's approval mechanism.
    Returns one of: cr_approve, comment_approve, approve_only,
    label_based, no_review, self_approved, rubber_stamp.
    """

def compute_mechanism_rates(changes: list[MergedChange]) -> MechanismRates:
    """Compute mechanism distribution across a set of changes."""

def compute_review_depth(changes: list[MergedChange]) -> ReviewDepth:
    """Compute review depth metrics."""

def compute_participant_profile(changes: list[MergedChange]) -> ParticipantProfile:
    """Analyze reviewer distribution and bot ecosystem."""

def compute_timing_profile(changes: list[MergedChange]) -> TimingProfile:
    """Analyze review timing patterns."""

def classify_workflow_type(mechanisms: MechanismRates) -> str:
    """Classify the overall workflow type from mechanism rates."""

def detect_transitions_coarse(windows: list[WindowProfile]) -> list[tuple[int, str, float, float]]:
    """Pass 1: detect candidate transitions between fixed windows.
    Returns (window_index, dimension, before_rate, after_rate) for shifts >20pp."""

def refine_transition(
    changes: list[MergedChange],
    window_start: datetime,
    window_end: datetime,
    dimension: str,
) -> Transition:
    """Pass 2: binary search within a window to find the exact transition point.
    Recurses until sub-window is <=10 PRs or delta <10pp.
    Returns Transition with timestamp precision to the PR level."""

def build_adaptive_windows(
    changes: list[MergedChange],
    transitions: list[Transition],
) -> list[WindowProfile]:
    """Pass 3: recompute profiles using transition timestamps as boundaries."""

def compute_gini(counts: list[int]) -> float:
    """Gini coefficient for reviewer concentration."""
```

### Main Analyzer

```python
# workflow.py

def analyze_workflow(
    changes: list[MergedChange],
    *,
    window_size_days: int = 30,
    lookback_days: int = 90,
) -> WorkflowProfile:
    """Full workflow analysis with adaptive time windows.

    Three-pass algorithm:

    Pass 1 — Coarse detection:
      Split changes into fixed windows (window_size_days).
      Compute mechanism rates for each.
      Identify candidate transitions (>20pp shift on any dimension).

    Pass 2 — Binary search refinement:
      For each candidate transition, binary search within the window
      to find the inflection point to PR-level precision.
      Transition timestamp = midpoint between last old-workflow PR
      and first new-workflow PR merge times.

    Pass 3 — Adaptive recomputation:
      Use transition timestamps as window boundaries.
      Recompute full profiles (mechanisms, depth, participants, timing)
      for each adaptive window.

    If no transitions detected, the entire period is one window.

    Finally:
      - Classify workflow type per window
      - Generate recommendations from the current (most recent) window
      - Tag each PR with its mechanism and active workflow type
    """

def print_workflow_report(profile: WorkflowProfile) -> str:
    """Human-readable workflow report for terminal output."""
```

### Recommendation Engine

```python
# workflow_recommend.py

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
```

### Integration Points

**Runner (study):**
```python
profile = analyze_workflow(changes)
print(print_workflow_report(profile))
# Then run upfront/catchrate with profile context
```

**CATCHRATE:**
```python
# Before classification, check the profile
profile = analyze_workflow(changes)
# Use profile.recommendations.catchrate_signals to configure detection
# Use profile.pr_tags to apply per-PR workflow context
```

**UPFRONT:**
```python
# Use profile.recommendations.upfront_friction_baseline
# to interpret TTM/review cycle comparisons
```

### Serialization

`WorkflowProfile.to_dict()` for JSON output. Saved alongside other data files:
```
data/
  prs-cli-cli.json
  upfront-cli-cli.json
  catchrate-cli-cli.json
  workflow-cli-cli.json     # NEW
```

### What We Expect to Discover

Running across 30 repos will likely reveal:

1. **Monorepo multi-workflow** — different directories reviewed differently
2. **Seasonal patterns** — review depth drops during release freezes
3. **AI reviewer adoption curves** — repos adopting CodeRabbit/Copilot mid-period
4. **Review fatigue signals** — rubber stamp rate climbing over time
5. **Cultural patterns** — some orgs have "approve then comment" culture vs "comment then approve"
6. **Stale review patterns** — approved weeks ago, merged today (approval expiry question)
7. **Review delegation** — senior requests review, junior approves (detectable by reviewer seniority patterns)

Each discovery becomes a new detector function in `workflow_detect.py`. The architecture is designed to accumulate detectors without restructuring.
