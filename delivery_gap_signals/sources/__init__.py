"""Source adapters for fetching merged changes from various platforms.

Each adapter exports a fetch_changes() function returning list[MergedChange].
"""

from pathlib import Path


def auto_fetch(
    repo: str,
    lookback_days: int = 90,
    *,
    limit: int = 500,
    source: str | None = None,
):
    """Detect the right adapter based on repo format or explicit source.

    source options: "github" (default for owner/repo), "graphql", "git", "file"
    """
    # Explicit source override
    if source == "graphql":
        from . import github_graphql
        return github_graphql.fetch_changes(repo, lookback_days, limit=limit)
    elif source == "rest" or source == "github":
        from . import github
        return github.fetch_changes(repo, lookback_days, limit=limit)
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
        from . import github
        return github.fetch_changes(repo, lookback_days, limit=limit)
    else:
        raise ValueError(f"Cannot determine source for: {repo!r}")
