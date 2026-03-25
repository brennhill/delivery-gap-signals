"""GitHub source adapter — fetches merged PRs via gh CLI."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from ..models import CIStatus, MergedChange, Review

# Known bot/LLM reviewer accounts
_BOT_REVIEWERS = {
    "copilot", "github-copilot", "copilot-pull-request-reviewer",
    "copilot-swe-agent", "coderabbitai", "codium-ai",
    "sourcery-ai", "ellipsis-dev", "greptile-bot", "pantheon-ai",
    "promptfoo-scanner", "cubic-dev-ai", "devin-ai-integration",
}
_BOT_PREFIXES = ("copilot-", "coderabbit-", "sourcery-", "pantheon-", "devin-")

_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def _validate_repo(repo: str) -> None:
    if not _REPO_PATTERN.match(repo):
        raise ValueError(f"Invalid repo format: '{repo}'. Expected 'owner/repo'.")
    if any(p in (".", "..") for p in repo.split("/")):
        raise ValueError(f"Invalid repo format: '{repo}'.")


def _is_bot_reviewer(login: str) -> bool:
    low = login.lower()
    return login.endswith("[bot]") or low in _BOT_REVIEWERS or low.startswith(_BOT_PREFIXES)


def _parse_ci_status(pr: dict) -> CIStatus | None:
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return CIStatus.NO_CHECKS
    for check in checks:
        conclusion = (check.get("conclusion") or "").lower()
        if conclusion in ("failure", "timed_out", "cancelled"):
            return CIStatus.FAILED
    return CIStatus.PASSED


def _parse_reviews(pr: dict) -> list[Review]:
    reviews = []
    for r in pr.get("reviews") or []:
        login = r.get("author", {}).get("login", "") if isinstance(r.get("author"), dict) else ""
        state_raw = (r.get("state") or "").lower()
        state = {
            "approved": "approved",
            "changes_requested": "changes_requested",
            "commented": "commented",
            "dismissed": "commented",
        }.get(state_raw, "commented")

        submitted = r.get("submittedAt") or r.get("submitted_at") or ""
        if submitted:
            submitted_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        else:
            continue  # skip reviews without timestamp

        reviews.append(Review(
            reviewer=login,
            state=state,
            submitted_at=submitted_dt,
            is_bot=_is_bot_reviewer(login),
        ))
    return reviews


def _sanitize_stderr(stderr: str, max_len: int = 200) -> str:
    import re as _re
    text = _re.sub(r"gh[pousr]_[A-Za-z0-9]{10,}|x-access-token:[^@]+@", "[REDACTED]", stderr.strip())
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


_PAGE_SIZE = 15
_PAGE_SIZE_FALLBACKS = [10, 5]
_HARD_LIMIT = 5000  # absolute safety cap regardless of time window
_FIELDS = (
    "number,title,mergedAt,files,mergeCommit,body,"
    "additions,deletions,reviews,statusCheckRollup,"
    "labels,author,createdAt"
)


def _is_gateway_error(error_msg: str) -> bool:
    """Check if a RuntimeError message indicates a GitHub GraphQL overload."""
    return any(sig in error_msg for sig in ("502", "504", "stream error", "CANCEL", "timed out"))


def _run_gh_pr_list(repo: str, page_size: int, search: str) -> subprocess.CompletedProcess:
    """Run a single gh pr list call. Returns the CompletedProcess."""
    try:
        return subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo,
                "--state", "merged", "--limit", str(page_size),
                "--search", search,
                "--json", _FIELDS,
            ],
            capture_output=True, text=True, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("gh pr list timed out") from err


def _fetch_pr_batches(repo: str, since_date: str, limit: int) -> list[dict]:
    """Paginate gh pr list in batches to avoid GraphQL payload limits.

    Narrows the search window each batch using the oldest mergedAt date,
    same pattern as UPFRONT's github_source.py.

    Auto-adjusts page size on 502/504 errors:
      50 -> 25 -> 10 -> raise (caller handles REST fallback).
    """
    items: list[dict] = []
    seen: set[int] = set()
    upper_date = ""  # no upper bound initially
    page_size = _PAGE_SIZE
    window_covered = False  # True once we've paged past since_date

    while not window_covered and len(items) < min(limit, _HARD_LIMIT):

        # Build date range: merged:SINCE..UPPER or merged:>=SINCE
        if upper_date:
            search = f"merged:{since_date}..{upper_date}"
        else:
            search = f"merged:>={since_date}"

        result = _run_gh_pr_list(repo, page_size, search)

        if result.returncode != 0:
            err_msg = _sanitize_stderr(result.stderr)

            if _is_gateway_error(err_msg):
                # Try progressively smaller page sizes
                recovered = False
                for fallback_size in _PAGE_SIZE_FALLBACKS:
                    if fallback_size >= page_size:
                        continue
                    print(
                        f"  {err_msg[:30]}... at page_size={page_size}, "
                        f"retrying at {fallback_size}...",
                        file=sys.stderr,
                    )
                    page_size = fallback_size
                    result = _run_gh_pr_list(repo, page_size, search)
                    if result.returncode == 0:
                        recovered = True
                        break
                    err_msg = _sanitize_stderr(result.stderr)
                    if not _is_gateway_error(err_msg):
                        # Non-gateway error at smaller size — surface it
                        raise RuntimeError(f"gh pr list failed: {err_msg}")

                if not recovered:
                    # All page sizes exhausted — raise so caller can
                    # fall back to REST adapter
                    raise RuntimeError(
                        f"gh pr list failed (all page sizes exhausted): {err_msg}"
                    )
            else:
                raise RuntimeError(f"gh pr list failed: {err_msg}")

        batch = json.loads(result.stdout)
        new_items = [it for it in batch if it.get("number") not in seen]
        seen.update(it.get("number") for it in batch)
        items.extend(new_items)

        if len(batch) < page_size:
            window_covered = True  # exhausted all PRs in window
            break

        if not new_items:
            window_covered = True
            break

        # Set upper bound to day BEFORE the oldest in this batch
        oldest = min((it.get("mergedAt", "") for it in batch), default="")
        if not oldest:
            window_covered = True
            break
        from datetime import date as _date
        oldest_date = _date.fromisoformat(oldest[:10])
        prev_day = oldest_date - timedelta(days=1)
        upper_date = prev_day.isoformat()

        if upper_date < since_date:
            window_covered = True  # reached the start of the lookback window

    return items[:limit]


def _fetch_pr_batches_no_search(repo: str, since_date: str, limit: int) -> list[dict]:
    """Fallback: fetch PRs without --search and filter by date client-side.

    gh pr list without --search returns most-recently-merged first. Uses
    small page sizes to avoid GraphQL gateway errors on large repos.
    """
    items: list[dict] = []
    seen: set[int] = set()

    # Use smaller fields to avoid GraphQL payload limits — drop commits
    # and statusCheckRollup which are the heaviest nested fields.
    light_fields = (
        "number,title,mergedAt,files,mergeCommit,body,"
        "additions,deletions,reviews,labels,author,createdAt"
    )

    for page_size in [100, 50, 25]:
        fetch_limit = min(limit, 500)
        try:
            result = subprocess.run(
                [
                    "gh", "pr", "list", "--repo", repo,
                    "--state", "merged", "--limit", str(fetch_limit),
                    "--json", light_fields,
                ],
                capture_output=True, text=True, check=False, timeout=120,
            )
        except subprocess.TimeoutExpired:
            continue  # try smaller page size

        if result.returncode != 0:
            err_msg = _sanitize_stderr(result.stderr)
            if _is_gateway_error(err_msg):
                print(
                    f"  no-search fallback: {err_msg[:40]}... retrying smaller...",
                    file=sys.stderr,
                )
                continue  # try smaller page size
            raise RuntimeError(f"gh pr list (no-search fallback) failed: {err_msg}")

        batch = json.loads(result.stdout)
        for pr in batch:
            merged_at = pr.get("mergedAt", "")
            if not merged_at:
                continue
            if merged_at[:10] < since_date:
                continue
            num = pr.get("number")
            if num not in seen:
                seen.add(num)
                items.append(pr)
        break  # success — don't retry

    return items[:limit]


def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 5000,
) -> list[MergedChange]:
    """Fetch merged PRs from GitHub via gh CLI.

    Pages until the full lookback window is covered. The limit parameter
    is a safety cap, not a target — the time window is the primary constraint.
    Auto-adjusts page size on GraphQL errors (50 -> 25 -> 10).

    If --search "merged:>=DATE" returns 0 results (known silent failure on
    some repos), falls back to fetching without --search and filtering
    client-side by merge date.
    """
    _validate_repo(repo)

    since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    prs = _fetch_pr_batches(repo, since_date, limit)

    # Fallback: --search "merged:>=DATE" silently returns [] on some repos.
    # Fetch without --search and filter client-side.
    if not prs:
        print(
            f"  --search returned 0 PRs for {repo}, retrying without search filter...",
            file=sys.stderr,
        )
        prs = _fetch_pr_batches_no_search(repo, since_date, limit)

    changes: list[MergedChange] = []
    for pr in prs:
        merged_at = pr.get("mergedAt", "")
        if not merged_at:
            continue

        sha = (pr.get("mergeCommit") or {}).get("oid", "")
        author = ""
        if isinstance(pr.get("author"), dict):
            author = pr["author"].get("login", "")

        created_at = None
        if pr.get("createdAt"):
            created_at = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))

        files = [f.get("path", "") for f in pr.get("files", []) if f.get("path")]

        changes.append(MergedChange.build(
            id=str(pr["number"]),
            source="github",
            repo=repo,
            title=pr.get("title", ""),
            body=pr.get("body", "") or "",
            author=author,
            merged_at=datetime.fromisoformat(merged_at.replace("Z", "+00:00")),
            created_at=created_at,
            files=files,
            additions=pr.get("additions", 0) or 0,
            deletions=pr.get("deletions", 0) or 0,
            reviews=_parse_reviews(pr),
            ci_status=_parse_ci_status(pr),
            merge_commit_sha=sha or None,
            pr_number=pr["number"],
        ))

    return changes
