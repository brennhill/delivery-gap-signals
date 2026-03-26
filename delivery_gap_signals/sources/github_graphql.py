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
_RATE_LIMIT_SIGNALS = ("rate limit", "API rate limit", "403", "429", "secondary rate", "abuse detection")

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
        err_text = result.stderr.strip()
        # Rate limit: wait and retry
        if any(sig in err_text.lower() for sig in _RATE_LIMIT_SIGNALS):
            import time as _time
            wait = 900  # 15 minutes
            print(f"\n  *** GitHub rate limit hit. Waiting {wait//60} minutes... ***",
                  flush=True)
            _time.sleep(wait)
            # Retry once after waiting
            try:
                result = subprocess.run(
                    args, capture_output=True, text=True, check=False, timeout=timeout,
                )
            except subprocess.TimeoutExpired as err:
                raise RuntimeError("gh api graphql timed out (after rate limit wait)") from err
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if "errors" not in data:
                    return data
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

    # Skip optimization disabled — GitHub orders by UPDATED_AT not
    # MERGED_AT, so skip_to_window jumps around non-monotonically.
    # For historical fetches we page through everything and filter
    # client-side. The "added_this_page == 0 and any_before_since"
    # check below stops us once we're past the window.
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

        added_this_page = 0
        any_before_since = False
        for pr in nodes:
            change = _parse_pr_node(pr, repo, lookback_days, since=since, until=until)
            if change is not None:
                all_changes.append(change)
                added_this_page += 1
                if 0 < limit <= len(all_changes):
                    if incremental_path:
                        _save_incremental(incremental_path, all_changes[:limit])
                    return all_changes[:limit]
            else:
                # Track if we've gone past the window (older than since)
                mat = pr.get("mergedAt")
                if mat and since:
                    dt = datetime.fromisoformat(mat.replace("Z", "+00:00"))
                    if dt < since:
                        any_before_since = True

        # Save after every page so no data is lost on interrupt
        if incremental_path and all_changes:
            _save_incremental(incremental_path, all_changes)

        # If we found PRs older than our window, we've passed it — stop
        if any_before_since:
            break

        # If we got nodes but none matched and none were too old,
        # we're still approaching the window — keep paging
        # (unless there's no since filter, then old behavior applies)
        if nodes and added_this_page == 0 and not since:
            break

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


def _skip_to_window(
    owner: str, name: str, until: datetime,
) -> str | None:
    """Skip ahead through pages to find the cursor near `until`.

    Uses large page sizes to jump fast, checks the oldest PR on each
    page. Once we find a page containing PRs near or before `until`,
    return that cursor as the starting point for detailed collection.
    """
    cursor: str | None = None
    prev_cursor: str | None = None
    skip_size = 25  # Start moderate, GitHub often 504s at 100

    print(f"    Skipping ahead to find window (before {until.strftime('%Y-%m-%d')})...", flush=True)
    pages_skipped = 0

    while True:
        variables = {
            "owner": owner,
            "repo": name,
            "after": cursor,
            "pageSize": skip_size,
        }

        try:
            data = _run_graphql(_QUERY, variables)
        except RuntimeError as exc:
            if _is_gateway_error(str(exc)) and skip_size > _MIN_PAGE_SIZE:
                skip_size = max(_MIN_PAGE_SIZE, skip_size // 2)
                print(f"    skip: got {exc}, backing down to size={skip_size}", flush=True)
                continue
            # If we can't even skip at min size, fall back to no skip
            print(f"    skip: failed ({exc}), starting from beginning", flush=True)
            return None

        prs_data = data.get("data", {}).get("repository", {}).get("pullRequests", {})
        nodes = prs_data.get("nodes", [])
        page_info = prs_data.get("pageInfo", {})

        if not nodes:
            print(f"    skip: exhausted all PRs after {pages_skipped} pages", flush=True)
            return cursor

        # Check newest and oldest PR on this page
        newest_on_page = None
        oldest_on_page = None
        for pr in nodes:
            mat = pr.get("mergedAt") or pr.get("updatedAt")
            if mat:
                dt = datetime.fromisoformat(mat.replace("Z", "+00:00"))
                if newest_on_page is None or dt > newest_on_page:
                    newest_on_page = dt
                if oldest_on_page is None or dt < oldest_on_page:
                    oldest_on_page = dt

        pages_skipped += 1
        total_skipped = pages_skipped * skip_size
        newest_str = newest_on_page.strftime('%Y-%m-%d') if newest_on_page else '?'
        oldest_str = oldest_on_page.strftime('%Y-%m-%d') if oldest_on_page else '?'
        target_str = until.strftime('%Y-%m-%d')

        if oldest_on_page and oldest_on_page <= until:
            print(f"    skip: page {pages_skipped} spans {newest_str}..{oldest_str}, "
                  f"reached target {target_str} — starting collection "
                  f"({total_skipped} PRs skipped)", flush=True)
            return prev_cursor
        else:
            print(f"    skip: page {pages_skipped} spans {newest_str}..{oldest_str}, "
                  f"still above target {target_str} — jumping ahead "
                  f"({total_skipped} PRs skipped so far)", flush=True)

        prev_cursor = cursor
        if not page_info.get("hasNextPage"):
            print(f"    skip: reached end of repo after {pages_skipped} pages, "
                  f"no PRs in window", flush=True)
            return cursor

        cursor = page_info.get("endCursor")
        if not cursor:
            return prev_cursor
