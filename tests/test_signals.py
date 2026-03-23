"""Tests for delivery-gap-signals shared functions."""

import unittest

from delivery_gap_signals import (
    compute_file_overlap,
    extract_fixes_sha,
    extract_pr_number_from_subject,
    extract_revert_pr_numbers,
    extract_ticket_ids,
    is_fix_message,
    is_revert_message,
    is_source_file,
)


class TestExtractTicketIds(unittest.TestCase):
    def test_jira_style(self):
        self.assertEqual(extract_ticket_ids("Fix PROJ-123 bug"), {"PROJ-123"})

    def test_github_issue(self):
        self.assertEqual(extract_ticket_ids("fixes #456"), {"#456"})

    def test_multiple_tickets(self):
        ids = extract_ticket_ids("Fixes PROJ-123, closes #456")
        self.assertEqual(ids, {"PROJ-123", "#456"})

    def test_no_tickets(self):
        self.assertEqual(extract_ticket_ids("refactor: clean up utils"), set())

    def test_deduplication(self):
        ids = extract_ticket_ids("fixes #123 and #123 again")
        self.assertEqual(ids, {"#123"})

    def test_linear_alphanumeric(self):
        ids = extract_ticket_ids("ENG-abc123: add feature")
        self.assertIn("ENG-ABC123", ids)


class TestIsFixMessage(unittest.TestCase):
    def test_fix_prefix(self):
        self.assertTrue(is_fix_message("fix: null pointer"))

    def test_hotfix_prefix(self):
        self.assertTrue(is_fix_message("hotfix: urgent patch"))

    def test_bugfix_prefix(self):
        self.assertTrue(is_fix_message("bugfix: race condition"))

    def test_patch_prefix(self):
        self.assertTrue(is_fix_message("patch: security update"))

    def test_not_fix(self):
        self.assertFalse(is_fix_message("feat: add checkout"))

    def test_revert_not_fix(self):
        """revert is detected by is_revert_message, not is_fix_message."""
        self.assertFalse(is_fix_message("Revert \"add feature\""))


class TestIsRevertMessage(unittest.TestCase):
    def test_revert_with_quotes(self):
        self.assertTrue(is_revert_message('Revert "add feature"'))

    def test_this_reverts_commit(self):
        self.assertTrue(is_revert_message("This reverts commit abc1234"))

    def test_not_revert(self):
        self.assertFalse(is_revert_message("fix: revert-related bug"))


class TestExtractFixesSha(unittest.TestCase):
    def test_fixes_trailer(self):
        self.assertEqual(extract_fixes_sha("Fixes: abc1234567"), "abc1234567")

    def test_no_trailer(self):
        self.assertIsNone(extract_fixes_sha("fix: something"))


class TestExtractRevertPrNumbers(unittest.TestCase):
    def test_revert_pr(self):
        self.assertEqual(extract_revert_pr_numbers("Revert #42"), {42})

    def test_reverts_pr(self):
        self.assertEqual(extract_revert_pr_numbers("Reverts #99"), {99})

    def test_no_pr(self):
        self.assertEqual(extract_revert_pr_numbers("Revert bad change"), set())


class TestExtractPrNumberFromSubject(unittest.TestCase):
    def test_merge_commit(self):
        self.assertEqual(
            extract_pr_number_from_subject("Merge pull request #42 from owner/branch"),
            42,
        )

    def test_squash_merge(self):
        self.assertEqual(
            extract_pr_number_from_subject("feat: add checkout (#123)"),
            123,
        )

    def test_no_pr(self):
        self.assertIsNone(extract_pr_number_from_subject("fix: null pointer"))


class TestIsSourceFile(unittest.TestCase):
    def test_source_file(self):
        self.assertTrue(is_source_file("src/app.py"))

    def test_lockfile(self):
        self.assertFalse(is_source_file("package-lock.json"))

    def test_docs(self):
        self.assertFalse(is_source_file("docs/guide.md"))

    def test_github_dir(self):
        self.assertFalse(is_source_file(".github/workflows/ci.yml"))


class TestComputeFileOverlap(unittest.TestCase):
    def test_full_overlap(self):
        self.assertAlmostEqual(
            compute_file_overlap({"a.py", "b.py"}, {"a.py", "b.py"}),
            1.0,
        )

    def test_partial_overlap(self):
        self.assertAlmostEqual(
            compute_file_overlap({"a.py", "b.py", "c.py"}, {"a.py", "d.py"}),
            0.5,
        )

    def test_no_overlap(self):
        self.assertAlmostEqual(
            compute_file_overlap({"a.py"}, {"b.py"}),
            0.0,
        )

    def test_empty_candidate(self):
        self.assertAlmostEqual(compute_file_overlap({"a.py"}, set()), 0.0)

    def test_surgical_fix(self):
        """1-file fix to 20-file original: 100% of candidate overlaps."""
        original = {f"file{i}.py" for i in range(20)}
        fix = {"file3.py"}
        self.assertAlmostEqual(compute_file_overlap(original, fix), 1.0)


if __name__ == "__main__":
    unittest.main()
