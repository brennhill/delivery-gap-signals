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

from ..models import CIStatus, MergedChange, Review

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
          }
        }
        commits(last: 1) {
          nodes {
            commit {
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
        ))
    return reviews


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


def _parse_pr_node(pr: dict, repo: str, lookback_days: int) -> MergedChange | None:
    """Convert a GraphQL PR node to MergedChange. Returns None if outside window."""
    merged_at = pr.get("mergedAt")
    if not merged_at:
        return None

    merged_dt = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
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
    )


def fetch_changes(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 0,
    page_size: int = 15,
) -> list[MergedChange]:
    """Fetch all merged PRs in the lookback window via GraphQL.

    Adaptive page sizing: starts at *page_size*, halves on gateway errors
    (502/504/timeout), retries the same cursor. Keeps paging until the
    full lookback window is covered or there are no more pages.

    Set limit > 0 to cap the total number of results (0 = fetch all).
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

        import sys
        print(f"  [{len(all_changes)} PRs] fetching page (size={current_page_size})...",
              end="", flush=True, file=sys.stderr)

        try:
            data = _run_graphql(_QUERY, variables)
        except RuntimeError as exc:
            print(" FAILED", file=sys.stderr)
            if _is_gateway_error(str(exc)) and current_page_size > _MIN_PAGE_SIZE:
                current_page_size = max(_MIN_PAGE_SIZE, current_page_size // 2)
                consecutive_successes = 0
                import sys
                print(
                    f"  page_size {current_page_size * 2} failed, backing down to {current_page_size}...",
                    file=sys.stderr,
                )
                continue  # retry same cursor with smaller page
            raise

        prs_data = data.get("data", {}).get("repository", {}).get("pullRequests", {})
        nodes = prs_data.get("nodes", [])
        page_info = prs_data.get("pageInfo", {})
        print(f" +{len(nodes)}", file=sys.stderr, flush=True)

        for pr in nodes:
            change = _parse_pr_node(pr, repo, lookback_days)
            if change is not None:
                all_changes.append(change)
                if 0 < limit <= len(all_changes):
                    return all_changes[:limit]

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
