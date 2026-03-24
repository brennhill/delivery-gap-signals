# delivery-gap-signals

Shared signal detection and data sourcing for the delivery-gap tool ecosystem.

```
pip install delivery-gap-signals
```

## Used by

- [changeledger](https://github.com/brennhill/change-ledger) — cost per accepted change
- [CatchRate](https://github.com/brennhill/catchrate) — pipeline trustworthiness
- [Upfront](https://github.com/brennhill/upfront) — spec quality measurement

## Signal Detection

Pure stateless functions for classifying commit messages and file paths. No I/O, no state.

```python
from delivery_gap_signals import (
    extract_ticket_ids,      # "Fixes PROJ-123, #456" → {"PROJ-123", "#456"}
    is_fix_message,          # "fix: null pointer" → True
    is_revert_message,       # 'Revert "add feature"' → True
    extract_fixes_sha,       # "Fixes: abc1234567" → "abc1234567"
    extract_revert_pr_numbers,  # "Reverts #42" → {42}
    extract_pr_number_from_subject,  # "Merge pull request #42 from ..." → 42
    is_source_file,          # "src/app.py" → True, "package-lock.json" → False
    compute_file_overlap,    # overlap ratio (% of candidate)
)
```

## Source Adapters

Fetch merged changes from any platform into a canonical `MergedChange` model. Tools receive `list[MergedChange]` and never call APIs directly.

```python
from delivery_gap_signals.sources import auto_fetch

# GitHub via gh CLI (REST)
changes = auto_fetch("owner/repo")

# GitHub via GraphQL — no 500-PR limit, cursor pagination
changes = auto_fetch("owner/repo", source="graphql")

# Local git repo
changes = auto_fetch(".", source="git")

# Cached JSON file (--from-prs)
changes = auto_fetch("prs.json", source="file")
```

### Available adapters

| Adapter | Source | Auth | Pagination | Reviews/CI |
|---------|--------|------|-----------|------------|
| `github` | `gh pr list` (REST) | gh CLI | 500 limit | Yes |
| `github_graphql` | `gh api graphql` | gh CLI | Cursor-based, unlimited | Yes |
| `git` | `git log` | None | N/A | No |
| `file` | JSON on disk | None | N/A | If present |

### MergedChange model

All adapters return `list[MergedChange]` — the universal unit across the ecosystem:

```python
from delivery_gap_signals import MergedChange

# Fields available on every change:
change.id              # "42" (PR number) or SHA
change.source          # "github", "github_graphql", "git", "file"
change.title           # PR title or commit subject
change.body            # PR body or commit body
change.merged_at       # datetime
change.files           # ["src/app.py", "src/utils.py"]
change.additions       # 50
change.deletions       # 10
change.ticket_ids      # frozenset({"PROJ-123", "#456"})
change.pr_number       # 42 or None (local git)
change.merge_commit_sha # "abc123..." or None

# Platform-dependent (None when unavailable):
change.reviews         # list[Review] or None
change.ci_status       # CIStatus.PASSED / FAILED / NO_CHECKS or None
```

## Why a separate package?

Three tools detect rework independently using similar patterns. Without a shared source of truth, the patterns drift and the tools disagree on which changes are reverts, fixes, or rework. This package ensures consistent detection and data fetching across the ecosystem.

Adding a new platform (GitLab, Bitbucket) means writing one adapter file. Zero changes to changeledger, CatchRate, or Upfront.
