"""GitHub REST source adapter — fetches merged PRs via gh api (REST only).

Uses /repos/{owner}/{repo}/pulls endpoint which does NOT go through GraphQL.
Resilient to GitHub GraphQL outages. Requires separate API calls for reviews
and check runs per PR, so it uses more API quota but never hits GraphQL.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

from ..models import CIStatus, MergedChange, Review

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


def _sanitize_stderr(stderr: str, max_len: int = 200) -> str:
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{10,}|x-access-token:[^@]+@", "[REDACTED]", stderr.strip())
    return text[:max_len] + "..." if len(text) > max_len else text


def _gh_rest(endpoint: str, *, timeout: int = 30) -> list | dict:
    """Call gh api with REST endpoint. No pagination — caller handles it."""
    cmd = ["gh", "api", endpoint, "--cache", "1h"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise RuntimeError(f"gh api timed out: {endpoint}") from err

    if result.returncode != 0:
        err_text = result.stderr.strip().lower()
        if any(sig in err_text for sig in ("rate limit", "403", "429", "secondary rate", "abuse")):
            import time as _time
            wait = 900
            print(f"\n  *** GitHub rate limit hit (REST). Waiting {wait//60} minutes... ***",
                  flush=True)
            _time.sleep(wait)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
            except subprocess.TimeoutExpired as err:
                raise RuntimeError(f"gh api timed out after rate limit wait: {endpoint}") from err
            if result.returncode == 0:
                stdout = result.stdout.strip()
                if stdout:
                    return json.loads(stdout)
        raise RuntimeError(f"gh api failed: {_sanitize_stderr(result.stderr)}")

    stdout = result.stdout.strip()
    if not stdout:
        return []

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Truncated response — try to salvage by finding the last complete
        # JSON object/array boundary
        for end in range(len(stdout) - 1, 0, -1):
            if stdout[end] in (']', '}'):
                try:
                    return json.loads(stdout[:end + 1])
                except json.JSONDecodeError:
                    continue
        raise RuntimeError(f"gh api returned unparseable JSON for {endpoint}")


def _fetch_pr_list(repo: str, since: str, limit: int) -> list[dict]:
    """Fetch merged PRs via REST pagination.

    Uses state=closed (includes merged) and filters to merged_at >= since.
    Cannot early-exit on date because sort=updated mixes unmerged and merged.
    Pages until we have enough merged PRs or exhaust the window.
    """
    prs: list[dict] = []
    page = 1
    per_page = 100
    consecutive_empty = 0

    while len(prs) < limit:
        endpoint = f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={per_page}&page={page}"
        batch = _gh_rest(endpoint, timeout=30)

        if not batch:
            break

        found_in_batch = 0
        for pr in batch:
            # Only merged PRs
            if not pr.get("merged_at"):
                continue
            # Only within lookback
            if pr["merged_at"] < since:
                continue
            prs.append(pr)
            found_in_batch += 1

        if found_in_batch == 0:
            consecutive_empty += 1
            # If 3 consecutive pages have no merged PRs in window, stop
            if consecutive_empty >= 3:
                break
        else:
            consecutive_empty = 0

        if len(batch) < per_page:
            break

        page += 1
        if page > 50:  # safety valve
            break

    return prs[:limit]


def _fetch_reviews(repo: str, pr_number: int) -> list[Review]:
    """Fetch reviews for a single PR via REST."""
    try:
        data = _gh_rest(f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100")
    except RuntimeError:
        return []

    if not isinstance(data, list):
        return []

    reviews = []
    for r in data:
        login = (r.get("user") or {}).get("login", "")
        state_raw = (r.get("state") or "").lower()
        state = {
            "approved": "approved",
            "changes_requested": "changes_requested",
            "commented": "commented",
            "dismissed": "commented",
        }.get(state_raw, "commented")

        submitted = r.get("submitted_at", "")
        if not submitted:
            continue

        reviews.append(Review(
            reviewer=login,
            state=state,
            submitted_at=datetime.fromisoformat(submitted.replace("Z", "+00:00")),
            is_bot=_is_bot_reviewer(login),
        ))
    return reviews


def _fetch_ci_status(repo: str, sha: str) -> CIStatus | None:
    """Fetch CI status for a commit via REST."""
    if not sha:
        return CIStatus.NO_CHECKS

    try:
        data = _gh_rest(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    except RuntimeError:
        return None

    runs = data.get("check_runs", []) if isinstance(data, dict) else []
    if not runs:
        return CIStatus.NO_CHECKS

    for run in runs:
        conclusion = (run.get("conclusion") or "").lower()
        if conclusion in ("failure", "timed_out", "cancelled"):
            return CIStatus.FAILED

    return CIStatus.PASSED


def _fetch_files(repo: str, pr_number: int) -> tuple[list[str], int, int]:
    """Fetch changed files for a PR via REST. Returns (paths, additions, deletions)."""
    try:
        data = _gh_rest(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100")
    except RuntimeError:
        return [], 0, 0

    if not isinstance(data, list):
        return [], 0, 0

    paths = [f.get("filename", "") for f in data if f.get("filename")]
    additions = sum(f.get("additions", 0) for f in data)
    deletions = sum(f.get("deletions", 0) for f in data)
    return paths, additions, deletions


def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 200,
) -> list[MergedChange]:
    """Fetch merged PRs from GitHub via pure REST API.

    Uses /repos/{owner}/{repo}/pulls (REST) instead of gh pr list (GraphQL).
    Makes additional REST calls per PR for reviews, CI status, and files.
    More API calls but zero GraphQL dependency.
    """
    _validate_repo(repo)

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    print(f"    Fetching PR list (REST, limit={limit})...")
    raw_prs = _fetch_pr_list(repo, since, limit)
    print(f"    {len(raw_prs)} merged PRs found. Fetching details...")

    changes: list[MergedChange] = []
    for i, pr in enumerate(raw_prs, 1):
        pr_number = pr["number"]
        if i % 20 == 0 or i == len(raw_prs):
            print(f"    [{i}/{len(raw_prs)}] enriching PR #{pr_number}...")

        merged_at = pr.get("merged_at", "")
        if not merged_at:
            continue

        author = (pr.get("user") or {}).get("login", "")
        sha = pr.get("merge_commit_sha", "")

        created_at = None
        if pr.get("created_at"):
            created_at = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))

        # Fetch per-PR details via REST
        reviews = _fetch_reviews(repo, pr_number)
        ci_status = _fetch_ci_status(repo, sha)
        files, additions, deletions = _fetch_files(repo, pr_number)

        changes.append(MergedChange.build(
            id=str(pr_number),
            source="github_rest",
            repo=repo,
            title=pr.get("title", ""),
            body=pr.get("body", "") or "",
            author=author,
            merged_at=datetime.fromisoformat(merged_at.replace("Z", "+00:00")),
            created_at=created_at,
            files=files,
            additions=additions,
            deletions=deletions,
            reviews=reviews,
            ci_status=ci_status,
            merge_commit_sha=sha or None,
            pr_number=pr_number,
        ))

    return changes
