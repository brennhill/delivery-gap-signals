"""Tests for MergedChange canonical model and source adapters."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from delivery_gap_signals import MergedChange, Review, CIStatus
from delivery_gap_signals.sources.file import fetch_changes as file_fetch


class TestMergedChange(unittest.TestCase):

    def test_build_extracts_ticket_ids(self):
        c = MergedChange.build(
            id="42", source="github", repo="owner/repo",
            title="Fix PROJ-123 checkout bug",
            body="Closes #456",
            author="dev",
            merged_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        self.assertIn("PROJ-123", c.ticket_ids)
        self.assertIn("#456", c.ticket_ids)

    def test_build_defaults(self):
        c = MergedChange.build(
            id="1", source="git", repo=".",
            title="test", body="", author="dev",
            merged_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(c.files, [])
        self.assertEqual(c.additions, 0)
        self.assertIsNone(c.reviews)
        self.assertIsNone(c.ci_status)
        self.assertIsNone(c.pr_number)

    def test_to_dict_roundtrip(self):
        c = MergedChange.build(
            id="42", source="github", repo="owner/repo",
            title="Add feature", body="Body text",
            author="dev",
            merged_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc),
            files=["src/app.py"],
            additions=50, deletions=10,
            merge_commit_sha="abc123",
            pr_number=42,
            reviews=[Review("alice", "approved", datetime(2026, 3, 1, tzinfo=timezone.utc))],
            ci_status=CIStatus.PASSED,
        )
        d = c.to_dict()
        self.assertEqual(d["id"], "42")
        self.assertEqual(d["pr_number"], 42)
        self.assertEqual(d["ci_status"], "passed")
        self.assertEqual(len(d["reviews"]), 1)

    def test_frozen(self):
        c = MergedChange.build(
            id="1", source="git", repo=".",
            title="test", body="", author="dev",
            merged_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with self.assertRaises(AttributeError):
            c.title = "changed"


class TestFileAdapter(unittest.TestCase):

    def test_read_cached_changes(self):
        data = [
            {
                "id": "42",
                "source": "github",
                "repo": "owner/repo",
                "title": "Fix bug PROJ-123",
                "body": "",
                "author": "dev",
                "merged_at": "2026-03-01T00:00:00+00:00",
                "files": ["src/app.py"],
                "additions": 50,
                "deletions": 10,
                "pr_number": 42,
                "merge_commit_sha": "abc123",
                "ticket_ids": ["PROJ-123"],
                "reviews": [],
                "ci_status": "passed",
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            path = f.name

        try:
            changes = file_fetch(path)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].pr_number, 42)
            self.assertEqual(changes[0].ci_status, CIStatus.PASSED)
            self.assertIn("PROJ-123", changes[0].ticket_ids)
        finally:
            os.unlink(path)

    def test_read_github_raw_format(self):
        """File adapter should handle GitHub's raw JSON format too."""
        data = [
            {
                "number": 99,
                "title": "Add feature",
                "body": "JIRA-789",
                "mergedAt": "2026-03-01T00:00:00Z",
                "files": ["app.py"],
                "additions": 20,
                "deletions": 5,
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
            path = f.name

        try:
            changes = file_fetch(path)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].id, "99")
            self.assertIn("JIRA-789", changes[0].ticket_ids)
        finally:
            os.unlink(path)

    def test_invalid_json_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write("not json")
            path = f.name
        try:
            with self.assertRaises(json.JSONDecodeError):
                file_fetch(path)
        finally:
            os.unlink(path)

    def test_non_array_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"not": "array"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                file_fetch(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
