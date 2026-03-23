# Ecosystem Architecture: Ports & Adapters for Data Sources

## Problem

Three tools each call `gh pr list` directly. Adding GitLab, Bitbucket, Azure DevOps, or a CSV import means changing all three tools. The API-specific parsing is duplicated — each tool extracts `mergedAt`, `files`, `body` from the same JSON with slightly different code.

## Design: Canonical Change Model + Source Adapters

```
                         ┌──────────────────────────┐
                         │   delivery-gap-signals    │
                         │                          │
  ┌─────────────┐        │  ┌────────────────────┐  │
  │   GitHub    │───────▶│  │  GitHubAdapter     │  │
  │   API (gh)  │        │  └────────┬───────────┘  │
  └─────────────┘        │           │              │
                         │           ▼              │
  ┌─────────────┐        │  ┌────────────────────┐  │        ┌──────────────┐
  │   GitLab    │───────▶│  │  GitLabAdapter     │──┼──▶     │              │
  │   API       │        │  └────────┬───────────┘  │  MergedChange[]  │  changeledger │
  └─────────────┘        │           │              │        │  catchrate   │
                         │           ▼              │        │  upfront     │
  ┌─────────────┐        │  ┌────────────────────┐  │        └──────────────┘
  │  Local Git  │───────▶│  │  GitAdapter        │  │
  │  (git log)  │        │  └────────┬───────────┘  │
  └─────────────┘        │           │              │
                         │           ▼              │
  ┌─────────────┐        │  ┌────────────────────┐  │
  │  JSON file  │───────▶│  │  FileAdapter       │  │
  │  (cached)   │        │  │  (--from-prs)      │  │
  └─────────────┘        │  └────────────────────┘  │
                         │                          │
                         │  signals.py (existing)    │
                         └──────────────────────────┘
```

Tools never call `gh`, `glab`, or `git` directly. They receive a `list[MergedChange]` and operate on it.

---

## Canonical Data Model

One model that represents a merged change regardless of platform:

```python
@dataclass(frozen=True)
class MergedChange:
    """A single merged change — the universal unit across all tools."""

    # Identity
    id: str                      # Platform-specific: PR number, MR iid, commit SHA
    source: str                  # "github", "gitlab", "git", "file"
    repo: str                    # "owner/repo" or local path

    # Content
    title: str                   # PR title or commit subject
    body: str                    # PR body or commit body
    author: str                  # Username or email

    # Timestamps
    created_at: datetime | None  # When opened (None for git commits)
    merged_at: datetime          # When merged

    # Files
    files: list[str]             # File paths touched
    additions: int               # Lines added
    deletions: int               # Lines deleted

    # Ticket linkage
    ticket_ids: frozenset[str]   # Extracted by delivery_gap_signals

    # Review data (None when unavailable)
    reviews: list[Review] | None
    ci_status: CIStatus | None

    # Commit data
    merge_commit_sha: str | None # The merge commit SHA
    pr_number: int | None        # PR/MR number (None for bare git)


@dataclass(frozen=True)
class Review:
    """A single review on a change."""
    reviewer: str
    state: str          # "approved", "changes_requested", "commented"
    submitted_at: datetime
    is_bot: bool


class CIStatus(str, Enum):
    """Aggregate CI result."""
    PASSED = "passed"
    FAILED = "failed"
    NO_CHECKS = "no_checks"
```

### What each tool needs from MergedChange

| Field | changeledger | CatchRate | Upfront |
|-------|-------------|-----------|---------|
| id | join key | join key | join key |
| title | subject display | classification | coverage |
| body | ticket extraction | escape detection | spec detection |
| merged_at | window/lookback | window/lookback | window/lookback |
| files | overlap detection | overlap detection | overlap detection |
| additions/deletions | LOC normalization | size bucketing | — |
| ticket_ids | rework signals | escape attribution | coverage linkage |
| reviews | — | human_save detection | — |
| ci_status | — | machine_catch detection | — |
| merge_commit_sha | SHA matching | escape matching | revert matching |
| pr_number | join key | join key | join key |
| author | — | reviewer filtering | bot filtering |
| created_at | — | time-to-merge | — |

The model is a superset. Each tool reads what it needs and ignores the rest.

---

## Source Adapters

Each adapter implements one function:

```python
def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 500,
) -> list[MergedChange]:
    ...
```

### GitHubAdapter

```python
# delivery_gap_signals/sources/github.py

def fetch_changes(repo, lookback_days=90, *, limit=500) -> list[MergedChange]:
    """Fetch merged PRs from GitHub via gh CLI."""
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo,
         "--state", "merged", "--limit", str(limit),
         "--search", f"merged:>={since}",
         "--json", "number,title,mergedAt,files,mergeCommit,body,"
                   "additions,deletions,reviews,statusCheckRollup,"
                   "labels,author,createdAt"],
        capture_output=True, text=True, timeout=60,
    )
    prs = json.loads(result.stdout)
    return [_pr_to_change(pr, repo) for pr in prs]
```

### GitAdapter

```python
# delivery_gap_signals/sources/git.py

def fetch_changes(repo_path, lookback_days=90, *, limit=500) -> list[MergedChange]:
    """Fetch merge commits from local git repo."""
    result = subprocess.run(
        ["git", "log", "--first-parent", f"--since={since}",
         "--format=...", "--numstat", "-z"],
        cwd=repo_path,
        capture_output=True, text=True, timeout=60,
    )
    return [_commit_to_change(chunk, repo_path) for chunk in parse(result.stdout)]
```

Reviews and CI status are `None` for git-only sources. CatchRate degrades gracefully (all non-escaped → machine_catch). Upfront works fully (spec detection uses title/body only).

### FileAdapter

```python
# delivery_gap_signals/sources/file.py

def fetch_changes(path, **kwargs) -> list[MergedChange]:
    """Read cached MergedChange data from JSON file (--from-prs)."""
    data = json.loads(Path(path).read_text())
    return [MergedChange(**item) for item in data]
```

This is the adapter the orchestrator uses: fetch once via GitHubAdapter, write to disk, tools read via FileAdapter.

### Future adapters

```
sources/
    github.py      # gh CLI
    gitlab.py      # glab CLI or API
    bitbucket.py   # Bitbucket API
    git.py         # local git log
    file.py        # cached JSON (--from-prs)
    csv.py         # CSV import (manual data)
```

Adding GitLab means writing one file (~80 lines) that maps GitLab MR fields to `MergedChange`. Zero changes to changeledger, CatchRate, or Upfront.

---

## How Tools Consume Changes

Today, each tool has its own fetch function:

```python
# changeledger/rework.py (today)
def get_merges_github(repo, lookback_days) -> list[Commit]:
    result = subprocess.run(["gh", "pr", "list", ...])
    ...

# catchrate/github_source.py (today)
def fetch_prs(repo, lookback_days) -> list[ClassifiedPR]:
    result = subprocess.run(["gh", "pr", "list", ...])
    ...
```

After the change:

```python
# changeledger/rework.py (after)
def get_merges(changes: list[MergedChange]) -> list[Commit]:
    """Convert platform-agnostic changes to Commit dataclass."""
    return [Commit.from_change(c) for c in changes]

# catchrate/classifier.py (after)
def classify(changes: list[MergedChange]) -> list[ClassifiedPR]:
    """Classify changes using review and CI data."""
    ...
```

Each tool's CLI handles the source selection:

```python
# Any tool's CLI
from delivery_gap_signals.sources import auto_fetch

def cmd_rework(args):
    if args.from_prs:
        changes = file.fetch_changes(args.from_prs)
    elif args.repo:
        changes = auto_fetch(args.repo, args.lookback)
    else:
        changes = git.fetch_changes(".", args.lookback)

    # Tool-specific logic operates on changes
    results = detect_rework(changes, args.window)
```

### `auto_fetch` — source auto-detection

```python
def auto_fetch(repo: str, lookback_days: int = 90) -> list[MergedChange]:
    """Detect the right adapter based on repo format."""
    if "/" in repo and not Path(repo).exists():
        # Looks like owner/repo — try GitHub, fall back to GitLab
        return github.fetch_changes(repo, lookback_days)
    elif Path(repo).is_dir():
        return git.fetch_changes(repo, lookback_days)
    elif Path(repo).is_file():
        return file.fetch_changes(repo)
    else:
        raise ValueError(f"Cannot determine source for: {repo}")
```

---

## Migration Path

### Phase 1: Add MergedChange to delivery-gap-signals (no tool changes)

```
delivery_gap_signals/
    __init__.py
    signals.py          # existing — pure pattern matching
    models.py           # NEW — MergedChange, Review, CIStatus
    sources/
        __init__.py     # auto_fetch
        github.py       # GitHubAdapter
        git.py          # GitAdapter
        file.py         # FileAdapter
```

### Phase 2: Add `--from-prs` to each tool

Each tool adds one flag. Internally, it calls `file.fetch_changes()` and converts to its domain model. The existing fetch functions remain as fallback.

### Phase 3: Replace tool-specific fetch with adapter calls

Remove `get_merges_github` from changeledger, `fetch_prs` from CatchRate, `fetch_github_prs` from Upfront. Replace with:

```python
from delivery_gap_signals.sources import github, git, file
```

Each tool's CLI selects the source. The tool's domain logic only sees `list[MergedChange]`.

### Phase 4: Orchestrator uses adapters

The orchestrator calls `github.fetch_changes()` once per repo, serializes to JSON, and tools read via `file.fetch_changes()`.

### Phase 5: Add new sources

GitLab, Bitbucket, Azure DevOps — one adapter file each. Tools work immediately because they only see `MergedChange`.

---

## Design Decisions

### Why frozen dataclass, not dict?

`MergedChange` is a frozen dataclass because:
- Fields are documented and discoverable (IDE autocomplete)
- Missing fields cause `TypeError` at construction, not `KeyError` at use
- Immutable — no accidental mutation during processing
- mypy catches field name typos

### Why subprocess for gh/glab, not REST API?

- `gh` and `glab` CLIs handle authentication, rate limiting, pagination, and token refresh
- No HTTP library dependency (stays stdlib-only)
- Users already have `gh` configured — no separate auth setup

### Why adapters in delivery-gap-signals, not in each tool?

- One place to fix a GitHub API change
- One place to add GitLab support
- Tools stay focused on their domain logic
- The shared package already exists and is already a dependency

### Why not an abstract base class for adapters?

A protocol or ABC (`class SourceAdapter(Protocol): def fetch_changes(...) -> list[MergedChange]`) would enforce the interface. But with only 3-4 adapters, duck typing is fine. Each adapter is a module with a `fetch_changes` function — no class hierarchy needed. Add a Protocol if the adapter count exceeds 5.

### What about fields a platform doesn't have?

`reviews` is `None` for git-only sources. `ci_status` is `None` for platforms without CI integration. `pr_number` is `None` for bare git commits.

Tools check for `None` and degrade:
- CatchRate without reviews → all non-escaped changes are `machine_catch` (no `human_save` possible)
- CatchRate without ci_status → all changes are `ungated`
- changeledger without pr_number → ticket ID join only

This is explicit in the model — `reviews: list[Review] | None` — so mypy enforces the None check.

---

## What Doesn't Change

- **signals.py** — pure pattern matching, no I/O. Unchanged.
- **Each tool's domain logic** — detect_rework, classify, score_quality. Unchanged.
- **Each tool's CLI** — still works standalone. `--repo` and `--from-prs` are both supported.
- **The orchestrator spec** — still valid. It just uses `github.fetch_changes()` instead of raw `gh` calls.

The architecture adds a layer (MergedChange + adapters) between "fetch data" and "analyze data." Everything above the adapter layer (tools) and below it (APIs) stays the same.
