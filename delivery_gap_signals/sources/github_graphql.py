"""GitHub GraphQL source adapter — fetches merged PRs with cursor pagination.

Uses `gh api graphql` for authentication. No gh pr list limit.
Fetches all fields needed by changeledger, CatchRate, and Upfront
in a single query per page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from ..models import CIStatus, Commit, MergedChange, Review

_REPO_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

_BOT_REVIEWERS = {
    "copilot", "github-copilot", "copilot-pull-request-reviewer",
    "copilot-swe-agent", "coderabbitai", "codium-ai",
    "sourcery-ai", "ellipsis-dev", "greptile-bot", "pantheon-ai",
    "promptfoo-scanner", "cubic-dev-ai", "devin-ai-integration",
}
_BOT_PREFIXES = ("copilot-", "coderabbit-", "sourcery-", "pantheon-", "devin-")

# GraphQL query — fetches merged PRs with reviews, commits, files, and CI status.
# Uses cursor-based pagination via $after.
_MIN_PAGE_SIZE = 5
_GATEWAY_SIGNALS = ("502", "504", "stream error", "CANCEL", "timed out")

_QUERY = """
query($owner: String!, $repo: String!, $after: String, $pageSize: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      states: MERGED,
      orderBy: {field: UPDATED_AT, direction: DESC},
      first: $pageSize,
      after: $after
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        body
        mergedAt
        createdAt
        lastEditedAt
        totalCommentsCount
        additions
        deletions
        author { login }
        mergeCommit { oid }
        files(first: 100) {
          nodes { path }
        }
        reviews(first: 50) {
          nodes {
            author { login }
            state
            submittedAt
            body
          }
        }
        commits(first: 100) {
          totalCount
          nodes {
            commit {
              message
              statusCheckRollup {
                contexts(first: 50) {
                  nodes {
                    ... on CheckRun {
                      conclusion
                    }
                    ... on StatusContext {
                      state
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_TOKEN_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}|x-access-token:[^@]+@")


def _sanitize_stderr(stderr: str, max_len: int = 200) -> str:
    text = _TOKEN_PATTERN.sub("[REDACTED]", stderr.strip())
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _validate_repo(repo: str) -> None:
    if not _REPO_PATTERN.match(repo):
        raise ValueError(f"Invalid repo format: '{repo}'. Expected 'owner/repo'.")
    if any(p in (".", "..") for p in repo.split("/")):
        raise ValueError(f"Invalid repo format: '{repo}'.")


def _is_bot_reviewer(login: str) -> bool:
    low = login.lower()
    return login.endswith("[bot]") or low in _BOT_REVIEWERS or low.startswith(_BOT_PREFIXES)


def _parse_ci_status(pr_node: dict) -> CIStatus | None:
    commits = pr_node.get("commits", {}).get("nodes", [])
    if not commits:
        return CIStatus.NO_CHECKS

    rollup = (commits[0].get("commit", {}).get("statusCheckRollup") or {})
    contexts = rollup.get("contexts", {}).get("nodes", [])
    if not contexts:
        return CIStatus.NO_CHECKS

    for ctx in contexts:
        # CheckRun has "conclusion", StatusContext has "state"
        conclusion = (ctx.get("conclusion") or "").lower()
        state = (ctx.get("state") or "").lower()
        if conclusion in ("failure", "timed_out", "cancelled") or state == "failure":
            return CIStatus.FAILED

    return CIStatus.PASSED


def _parse_reviews(pr_node: dict) -> list[Review]:
    reviews = []
    for r in pr_node.get("reviews", {}).get("nodes", []) or []:
        login = (r.get("author") or {}).get("login", "")
        state_raw = (r.get("state") or "").lower()
        state = {
            "approved": "approved",
            "changes_requested": "changes_requested",
            "commented": "commented",
            "dismissed": "commented",
        }.get(state_raw, "commented")

        submitted = r.get("submittedAt") or ""
        if not submitted:
            continue

        reviews.append(Review(
            reviewer=login,
            state=state,
            submitted_at=datetime.fromisoformat(submitted.replace("Z", "+00:00")),
            is_bot=_is_bot_reviewer(login),
            body=(r.get("body") or "")[:2000],
        ))
    return reviews


def _parse_commits(pr_node: dict) -> tuple[list[Commit], int]:
    """Parse commits from GraphQL response. Returns (commits, total_count)."""
    commits_data = pr_node.get("commits", {})
    total_count = commits_data.get("totalCount", 0)
    commits = []
    for node in commits_data.get("nodes", []) or []:
        commit = node.get("commit", {})
        message = commit.get("message", "")
        # Extract authored date from commit if available
        author_info = commit.get("author", {}) or {}
        authored_at = None
        date_str = author_info.get("date", "")
        if date_str:
            try:
                authored_at = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        commits.append(Commit(
            message=message,
            sha=node.get("oid", ""),
            authored_at=authored_at,
        ))
    return commits, total_count


def _is_gateway_error(err: str) -> bool:
    """True if the error looks like a GitHub response-size or gateway timeout."""
    return any(sig in err for sig in _GATEWAY_SIGNALS)


def _run_graphql(query: str, variables: dict, timeout: int = 60) -> dict:
    """Execute a GraphQL query via gh api graphql."""
    # gh api graphql expects -F for non-string types (Int)
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        if isinstance(v, int):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("gh api graphql timed out") from err

    if result.returncode != 0:
        raise RuntimeError(f"GraphQL query failed: {_sanitize_stderr(result.stderr)}")

    data = json.loads(result.stdout)
    if "errors" in data:
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError(f"GraphQL errors: {msgs}")

    return data


def _parse_pr_node(
    pr: dict, repo: str, lookback_days: int,
    since: datetime | None = None, until: datetime | None = None,
) -> MergedChange | None:
    """Convert a GraphQL PR node to MergedChange. Returns None if outside window."""
    merged_at = pr.get("mergedAt")
    if not merged_at:
        return None

    merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))

    # Fixed window takes precedence over lookback_days
    if since and merged_dt < since:
        return None
    if until and merged_dt > until:
        return None
    if not since:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        if merged_dt < cutoff:
            return None

    sha = (pr.get("mergeCommit") or {}).get("oid", "")
    author = (pr.get("author") or {}).get("login", "")
    created_at = None
    if pr.get("createdAt"):
        created_at = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))

    files = [
        f.get("path", "")
        for f in (pr.get("files", {}).get("nodes", []) or [])
        if f.get("path")
    ]

    # Parse commits
    commits, commit_count = _parse_commits(pr)

    # Parse edit timestamp
    last_edited_at = None
    if pr.get("lastEditedAt"):
        try:
            last_edited_at = datetime.fromisoformat(pr["lastEditedAt"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return MergedChange.build(
        id=str(pr["number"]),
        source="github_graphql",
        repo=repo,
        title=pr.get("title", ""),
        body=pr.get("body", "") or "",
        author=author,
        merged_at=merged_dt,
        created_at=created_at,
        files=files,
        additions=pr.get("additions", 0) or 0,
        deletions=pr.get("deletions", 0) or 0,
        reviews=_parse_reviews(pr),
        ci_status=_parse_ci_status(pr),
        merge_commit_sha=sha or None,
        pr_number=pr["number"],
        commits=commits,
        commit_count=commit_count,
        last_edited_at=last_edited_at,
        total_comments_count=pr.get("totalCommentsCount", 0) or 0,
    )


def _save_incremental(path: str, changes: list[MergedChange]) -> None:
    """Save fetched PRs to disk after each page. Merges with existing data."""
    import json as _json
    from pathlib import Path as _Path

    p = _Path(path)
    existing = {}
    if p.exists():
        try:
            for pr in _json.loads(p.read_text()):
                existing[pr.get("pr_number")] = pr
        except Exception:
            pass

    for c in changes:
        d = c.to_dict()
        existing[d.get("pr_number")] = d

    p.write_text(_json.dumps(list(existing.values()), indent=2, default=str))


def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 0,
    page_size: int = 15,
    incremental_path: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[MergedChange]:
    """Fetch all merged PRs in a time window via GraphQL.

    Time window options (in priority order):
    - since/until: fixed date window (for historical fetches)
    - lookback_days: N days from now (default)

    Adaptive page sizing: starts at min page size, scales up on
    consecutive successes. Set limit > 0 to cap total results.
    """
    _validate_repo(repo)

    owner, name = repo.split("/", 1)
    all_changes: list[MergedChange] = []
    cursor: str | None = None
    # Start small, scale up on success. Avoids wasting API calls on
    # 504s at larger sizes. Most repos work fine at 5; some can handle 25+.
    current_page_size = _MIN_PAGE_SIZE
    consecutive_successes = 0

    while True:
        variables = {
            "owner": owner,
            "repo": name,
            "after": cursor,
            "pageSize": current_page_size,
        }

        print(f"    [{len(all_changes):4d} PRs] page size={current_page_size}...", end="", flush=True)

        try:
            data = _run_graphql(_QUERY, variables)
        except RuntimeError as exc:
            print(f" FAILED", flush=True)
            if _is_gateway_error(str(exc)) and current_page_size > _MIN_PAGE_SIZE:
                current_page_size = max(_MIN_PAGE_SIZE, current_page_size // 2)
                consecutive_successes = 0
                print(f"    backing down to size={current_page_size}", flush=True)
                continue
            raise

        prs_data = data.get("data", {}).get("repository", {}).get("pullRequests", {})
        nodes = prs_data.get("nodes", [])
        page_info = prs_data.get("pageInfo", {})
        print(f" +{len(nodes)} = {len(all_changes) + len(nodes)} total", flush=True)

        for pr in nodes:
            change = _parse_pr_node(pr, repo, lookback_days, since=since, until=until)
            if change is not None:
                all_changes.append(change)
                if 0 < limit <= len(all_changes):
                    if incremental_path:
                        _save_incremental(incremental_path, all_changes[:limit])
                    return all_changes[:limit]

        # Save after every page so no data is lost on interrupt
        if incremental_path and all_changes:
            _save_incremental(incremental_path, all_changes)

        if not page_info.get("hasNextPage"):
            break

        cursor = page_info.get("endCursor")
        if not cursor:
            break

        # Scale up on consecutive successes
        consecutive_successes += 1
        if consecutive_successes >= 3 and current_page_size < page_size:
            current_page_size = min(page_size, current_page_size * 2)
            consecutive_successes = 0

    return all_changes
