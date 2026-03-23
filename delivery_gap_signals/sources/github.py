"""GitHub source adapter — fetches merged PRs via gh CLI."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

from ..models import CIStatus, MergedChange, Review

# Known bot/LLM reviewer accounts
_BOT_REVIEWERS = {
    "copilot", "github-copilot", "coderabbitai", "codium-ai",
    "sourcery-ai", "ellipsis-dev", "greptile-bot",
}

_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


def _validate_repo(repo: str) -> None:
    if not _REPO_PATTERN.match(repo):
        raise ValueError(f"Invalid repo format: '{repo}'. Expected 'owner/repo'.")
    if any(p in (".", "..") for p in repo.split("/")):
        raise ValueError(f"Invalid repo format: '{repo}'.")


def _is_bot_reviewer(login: str) -> bool:
    return login.endswith("[bot]") or login.lower() in _BOT_REVIEWERS


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


def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 500,
) -> list[MergedChange]:
    """Fetch merged PRs from GitHub via gh CLI.

    Returns the superset of fields needed by all three tools.
    """
    _validate_repo(repo)

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo,
                "--state", "merged", "--limit", str(limit),
                "--search", f"merged:>={since[:10]}",
                "--json", "number,title,mergedAt,files,mergeCommit,body,"
                          "additions,deletions,reviews,statusCheckRollup,"
                          "labels,author,createdAt",
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("gh pr list timed out after 60 seconds") from err

    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {_sanitize_stderr(result.stderr)}")

    prs = json.loads(result.stdout)
    if len(prs) >= limit:
        raise RuntimeError(
            f"gh pr list reached the {limit} PR limit for this lookback window. "
            "Narrow the lookback or add pagination."
        )

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
