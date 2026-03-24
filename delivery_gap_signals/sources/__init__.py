"""Source adapters for fetching merged changes from various platforms.

Each adapter exports a fetch_changes() function returning list[MergedChange].
"""

import sys
from pathlib import Path


def _github_with_rest_fallback(repo: str, lookback_days: int, limit: int):
    """Try GraphQL-backed gh pr list first; fall back to REST on gateway errors.

    NOTE: The REST fallback paginates by most-recently-updated (not merge
    date), so the result set may differ slightly for repos with many open PRs.
    This is acceptable — the alternative is no data at all.
    """
    from . import github

    try:
        return github.fetch_changes(repo, lookback_days, limit=limit)
    except RuntimeError as exc:
        err = str(exc)
        if any(sig in err for sig in ("502", "504", "stream error", "CANCEL", "timed out", "all page sizes exhausted")):
            print(
                f"  GraphQL failed for {repo}, falling back to REST adapter...",
                file=sys.stderr,
            )
            from . import github_rest
            return github_rest.fetch_changes(repo, lookback_days, limit=limit)
        raise


def auto_fetch(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 500,
    source: str | None = None,
):
    """Detect the right adapter based on repo format or explicit source.

    source options: "github" (default for owner/repo), "graphql", "git", "file",
                    "rest_only"

    When source is "github" (or auto-detected as owner/repo), tries GraphQL
    first and automatically falls back to REST on 502/504 gateway errors.
    """
    # Explicit source override
    if source == "graphql":
        from . import github_graphql
        return github_graphql.fetch_changes(repo, lookback_days, limit=limit)
    elif source == "rest_only":
        from . import github_rest
        return github_rest.fetch_changes(repo, lookback_days, limit=limit)
    elif source == "rest" or source == "github":
        return _github_with_rest_fallback(repo, lookback_days, limit)
    elif source == "git":
        from . import git
        return git.fetch_changes(repo, lookback_days, limit=limit)
    elif source == "file":
        from . import file
        return file.fetch_changes(repo)

    # Auto-detect
    if Path(repo).is_file():
        from . import file
        return file.fetch_changes(repo)
    elif Path(repo).is_dir():
        from . import git
        return git.fetch_changes(repo, lookback_days, limit=limit)
    elif "/" in repo:
        return _github_with_rest_fallback(repo, lookback_days, limit)
    else:
        raise ValueError(f"Cannot determine source for: {repo!r}")
