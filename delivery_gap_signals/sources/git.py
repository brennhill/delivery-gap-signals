"""Local git source adapter — fetches merge commits via git log."""

from __future__ import annotations

import contextlib
import subprocess
from datetime import datetime, timedelta, timezone

from ..models import MergedChange
from ..signals import extract_pr_number_from_subject


def _sanitize_stderr(stderr: str, max_len: int = 200) -> str:
    text = stderr.strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def fetch_changes(
    repo_path: str = ".",
    lookback_days: int = 90,
    *,
    limit: int = 500,
) -> list[MergedChange]:
    """Fetch merge commits from local git repo.

    Reviews and CI status are None (not available from git log).
    PR numbers are extracted from merge commit subjects when possible.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            [
                "git", "log", "--first-parent", f"--since={since}",
                f"--max-count={limit}",
                "--format=%x1e%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1f", "--numstat", "-z",
            ],
            cwd=repo_path,
            capture_output=True, text=True, check=False, timeout=60,
        )
    except subprocess.TimeoutExpired as err:
        raise RuntimeError("git log timed out after 60 seconds") from err

    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {_sanitize_stderr(result.stderr)}")

    changes: list[MergedChange] = []

    for chunk in result.stdout.split("\x1e"):
        chunk = chunk.strip("\x00\n")
        if not chunk:
            continue

        parts = chunk.split("\x1f", 5)
        if len(parts) < 6:
            continue

        sha, date_str, author, subject, body, stats_blob = parts

        files: list[str] = []
        total_additions = 0
        total_deletions = 0
        for entry in stats_blob.lstrip("\x00\n").split("\x00"):
            entry = entry.strip()
            if not entry:
                continue
            stat_parts = entry.split("\t", 2)
            if len(stat_parts) == 3:
                adds_str, dels_str, path = stat_parts
                if path:
                    files.append(path)
                with contextlib.suppress(ValueError):
                    total_additions += int(adds_str)
                with contextlib.suppress(ValueError):
                    total_deletions += int(dels_str)
            elif entry:
                files.append(entry)

        pr_number = extract_pr_number_from_subject(subject)

        changes.append(MergedChange.build(
            id=sha,
            source="git",
            repo=repo_path,
            title=subject,
            body=body.strip(),
            author=author,
            merged_at=datetime.fromisoformat(date_str),
            files=files,
            additions=total_additions,
            deletions=total_deletions,
            merge_commit_sha=sha,
            pr_number=pr_number,
        ))

    return changes
