# Orchestrator Spec: `delivery-gap scan`

## Context

### Problem / Why Now

Running changeledger, Upfront, and CatchRate on 30 repos means 90 GitHub API calls (3 per repo), 90 commands to run, and no progress visibility. Each tool independently fetches the same PR data with slightly different `--json` fields.

### Expected Outcomes

- **One command** runs all three tools across N repos
- **One API call per repo** instead of three
- **Progress indicator** shows which repo is being processed and how long each takes
- **Partial failure handling** — one repo failing doesn't stop the other 29
- **Unified output** — results organized per-repo in a single directory

### Priorities

1. **Fetch once, share data (P0)** — `--from-prs` flag on each tool to read cached PR data
2. **Multi-repo orchestration (P0)** — `delivery-gap scan --repos repos.txt`
3. **Progress and timing (P1)** — `[3/30] owner/repo — 4.2s`
4. **Failure isolation (P1)** — skip failed repos, report at end
5. **Resume support (P2)** — skip repos that already have results

---

## Architecture

### Data flow

```
repos.txt                 delivery-gap scan
  owner/repo-1    ──→    ┌─────────────────────────────┐
  owner/repo-2           │  For each repo:              │
  owner/repo-3           │    1. Fetch PRs (gh pr list) │
  ...                    │    2. Run upfront             │
                         │    3. Run catchrate           │
                         │    4. Run changeledger        │
                         └─────────┬───────────────────┘
                                   │
                                   ▼
                         results/
                           owner-repo-1/
                             prs.json          ← raw PR data (shared)
                             upfront.json      ← upfront output
                             catchrate.json    ← catchrate output
                             changeledger.json ← cost output (enriched)
                             report.html       ← unified HTML report
                           owner-repo-2/
                             ...
                           summary.json        ← cross-repo summary
                           errors.json         ← failed repos + reasons
```

### Shared PR fetch

One `gh pr list` call per repo, requesting the superset of fields:

```bash
gh pr list --repo owner/repo \
  --state merged --limit 500 \
  --search "merged:>=2025-12-24" \
  --json number,title,mergedAt,files,mergeCommit,body,additions,deletions,reviews,statusCheckRollup,labels,author,createdAt
```

This covers:
- **changeledger**: number, title, mergedAt, files, mergeCommit, body, additions, deletions
- **CatchRate**: all of the above + reviews, statusCheckRollup, createdAt, author
- **Upfront**: number, title, mergedAt, body, labels, files

The raw response is written to `prs.json` and each tool reads from it via `--from-prs`.

### `--from-prs` flag on each tool

Each tool adds a `--from-prs FILE` flag that reads pre-fetched PR data instead of calling `gh`:

```bash
upfront report --from-prs prs.json --json upfront.json
catchrate check --from-prs prs.json --json catchrate.json
changeledger full --from-prs prs.json --from-upfront upfront.json --from-catchrate catchrate.json --json changeledger.json
```

When `--from-prs` is provided:
- The tool skips its own `gh pr list` call
- It reads the JSON array from the file
- It extracts the fields it needs, ignoring extras
- `--repo` is not required (the repo name comes from the PR data or is inferred)
- `--lookback` and `--window` still apply (filter by `mergedAt` date)

### Orchestrator CLI

```bash
# Scan multiple repos
delivery-gap scan --repos repos.txt --output results/ [--lookback 90] [--window 14]

# Scan a single repo
delivery-gap scan --repo owner/repo --output results/

# Resume a partial run (skip repos with existing results)
delivery-gap scan --repos repos.txt --output results/ --resume

# Fetch-only mode (just download PR data, don't run tools)
delivery-gap fetch --repos repos.txt --output results/
```

`repos.txt` format:
```
owner/repo-1
owner/repo-2
# comment lines ignored
owner/repo-3
```

---

## Failure Handling

### Failure modes

| Failure | Cause | Handling |
|---------|-------|----------|
| `gh` not authenticated | Missing `gh auth login` | Fail fast before any repo. Check once at startup. |
| API rate limit | Too many calls | Pause, print warning, retry after reset window. `gh` handles this internally. |
| Repo not found | Typo in repos.txt | Log error, skip to next repo. |
| `gh pr list` returns 500 | Too many PRs in lookback | Log warning with the changeledger error message. Write empty result. |
| `gh pr list` timeout | Large repo, slow network | Log error, skip to next repo. Respect the 60s timeout. |
| Tool crashes | Bug in upfront/catchrate/changeledger | Catch exception, log traceback to `errors.json`, skip to next repo. |
| Partial tool failure | e.g., upfront succeeds, catchrate fails | Run remaining tools. changeledger runs without `--from-catchrate`. Note in output. |
| Disk full | Can't write results | Fail fast. No recovery. |
| Invalid JSON in `--from-prs` | Corrupted fetch | Re-fetch for that repo. |

### Error reporting

After all repos complete, print a summary:

```
Completed: 27/30 repos
Skipped:   2 (rate limit, will retry)
Failed:    1 (owner/repo-17: gh pr list returned 500 — narrow lookback)

See results/errors.json for details.
```

`errors.json`:
```json
[
  {
    "repo": "owner/repo-17",
    "step": "fetch",
    "error": "gh pr list reached the 500 PR limit",
    "suggestion": "Narrow --lookback or add pagination"
  },
  {
    "repo": "owner/repo-22",
    "step": "catchrate",
    "error": "KeyError: 'statusCheckRollup'",
    "traceback": "..."
  }
]
```

### Partial results

If upfront succeeds but catchrate fails for a repo:
- `upfront.json` exists, `catchrate.json` does not
- changeledger runs with `--from-upfront` only (no `--from-catchrate`)
- `changeledger.json` contains `by_spec_quality` but not `by_pipeline_classification`
- The repo is counted as "partial" in the summary, not "failed"

### Resume support

`--resume` checks for existing result files before processing each repo:
- If `prs.json` exists → skip fetch
- If `upfront.json` exists → skip upfront
- If `catchrate.json` exists → skip catchrate
- If `changeledger.json` exists → skip changeledger

This allows re-running after a failure without re-fetching or re-processing successful repos.

A repo is considered fully complete when all four files exist. `--resume --force repo-name` re-processes a specific repo.

---

## Progress Output

```
delivery-gap scan: 30 repos, lookback=90d, window=14d

[  1/30] owner/repo-1
         fetch: 95 PRs (3.2s)
         upfront: 72% coverage, quality 78 (1.1s)
         catchrate: 82% catch, 6% escape (2.4s)
         changeledger: $1,247/change, 14% rework (0.8s)

[  2/30] owner/repo-2
         fetch: 312 PRs (8.1s)
         upfront: 45% coverage, quality 61 (2.3s)
         catchrate: 71% catch, 12% escape (5.2s)
         changeledger: $2,891/change, 28% rework (1.1s)

...

[  17/30] owner/repo-17
          fetch: ERROR — 500 PR limit reached (skipping)

...

[  30/30] owner/repo-30
          fetch: 44 PRs (1.8s)
          upfront: 90% coverage, quality 85 (0.6s)
          catchrate: 91% catch, 2% escape (1.2s)
          changeledger: $489/change, 5% rework (0.4s)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Summary: 29/30 repos complete (1 failed)
Total time: 4m 12s
Results: results/summary.json
Errors: results/errors.json
```

### Quiet mode

`--quiet` suppresses per-repo detail, shows only the progress line:

```
[  1/30] owner/repo-1 ✓ (7.5s)
[  2/30] owner/repo-2 ✓ (16.7s)
[ 17/30] owner/repo-17 ✗ (500 PR limit)
[ 30/30] owner/repo-30 ✓ (4.0s)
```

---

## Cross-Repo Summary

`results/summary.json`:

```json
{
  "scan_date": "2026-03-23",
  "lookback_days": 90,
  "window_days": 14,
  "repos_scanned": 29,
  "repos_failed": 1,
  "total_prs": 2847,
  "aggregate": {
    "avg_spec_coverage_pct": 68,
    "avg_spec_quality": 72,
    "avg_catch_rate": 0.81,
    "avg_escape_rate": 0.07,
    "avg_cost_per_change": 1420,
    "avg_rework_rate_pct": 16.2
  },
  "repos": [
    {
      "repo": "owner/repo-1",
      "prs": 95,
      "spec_coverage_pct": 72,
      "spec_quality": 78,
      "catch_rate": 0.82,
      "escape_rate": 0.06,
      "cost_per_change": 1247,
      "rework_rate_pct": 14.0
    }
  ],
  "worst": {
    "highest_rework": "owner/repo-5 (34%)",
    "highest_escape": "owner/repo-12 (18%)",
    "lowest_spec_coverage": "owner/repo-8 (23%)",
    "highest_cost": "owner/repo-5 ($3,891/change)"
  }
}
```

---

## Acceptance Criteria

### Fetch once

1. **Given** a repos.txt with 3 repos, **When** `delivery-gap scan --repos repos.txt`, **Then** `gh pr list` is called exactly 3 times (once per repo), not 9.
2. **Given** `--from-prs prs.json` on any tool, **When** the tool runs, **Then** it does not call `gh pr list`.
3. **Given** a `prs.json` missing the `reviews` field, **When** CatchRate reads it, **Then** CatchRate warns "Missing reviews field — human_save classification unavailable" and classifies all non-escaped PRs as machine_catch.

### Failure isolation

4. **Given** repo-2 of 30 returns a `gh` error, **When** the orchestrator catches it, **Then** it logs the error, skips repo-2, and continues with repo-3.
5. **Given** upfront fails on repo-5 but catchrate succeeds, **When** changeledger runs on repo-5, **Then** it runs without `--from-upfront` and notes the gap.
6. **Given** `gh auth status` fails, **When** the orchestrator starts, **Then** it exits immediately with "GitHub CLI not authenticated. Run: gh auth login".

### Resume

7. **Given** a previous run completed 20/30 repos, **When** `--resume` is passed, **Then** only the remaining 10 repos are processed.
8. **Given** `--resume` and a repo has `prs.json` but not `changeledger.json`, **When** processed, **Then** fetch is skipped but all three tools run.

### Progress

9. **Given** a 30-repo scan, **When** running, **Then** each repo prints `[N/30] owner/repo` before starting and per-tool timing after completing.
10. **Given** `--quiet`, **When** running, **Then** only one line per repo (pass/fail + time).

### Cross-repo summary

11. **Given** 29/30 repos complete, **When** scan finishes, **Then** `summary.json` contains aggregate metrics across all successful repos and a `worst` section identifying outliers.

---

## Implementation

### Where it lives

`delivery-gap-signals` already exists as the shared package. The orchestrator is a natural extension — it's the "glue" that coordinates the tools. Add a `delivery_gap_signals/cli.py` with the `delivery-gap` entry point.

Alternatively, it could be a standalone `delivery-gap` package. But since `delivery-gap-signals` is already a dependency of all three tools, putting the orchestrator there avoids adding a fifth package.

### Entry point

```toml
# delivery-gap-signals/pyproject.toml
[project.scripts]
delivery-gap = "delivery_gap_signals.cli:main"
```

### Dependencies

The orchestrator calls the tools via subprocess (same as `gh`), not via Python import. This keeps the tools independently installable. The orchestrator only needs:
- `gh` CLI (for fetching)
- `upfront`, `catchrate`, `changeledger` CLIs (for processing)

It checks for each at startup and reports which are missing.

### `--from-prs` implementation per tool

Each tool needs a new code path that reads from a file instead of calling `gh`:

**changeledger** (`rework.py`):
- `get_merges_github` already parses `gh pr list` JSON output
- Add: if `--from-prs` is set, read file instead of calling subprocess
- The PR JSON schema is identical — same fields, same format

**CatchRate** (`github_source.py` or equivalent):
- Same pattern — read from file instead of API call
- Needs `reviews` and `statusCheckRollup` in the data

**Upfront** (`github_source.py`):
- Same pattern — read from file instead of API call
- Needs `body`, `labels`, `files`

### Estimated effort

| Component | Effort | Depends on |
|-----------|--------|-----------|
| `--from-prs` in changeledger | Small | Nothing |
| `--from-prs` in CatchRate | Small | Nothing |
| `--from-prs` in Upfront | Small | Nothing |
| Orchestrator fetch logic | Medium | `--from-prs` in all three |
| Progress output | Small | Orchestrator |
| Failure handling + resume | Medium | Orchestrator |
| Cross-repo summary | Medium | All tools complete |
