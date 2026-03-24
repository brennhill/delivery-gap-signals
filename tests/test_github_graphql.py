"""Tests for the GitHub GraphQL source adapter."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from delivery_gap_signals.models import CIStatus
from delivery_gap_signals.sources import github_graphql


def _make_pr_node(number=1, title="Test PR", body="", merged_days_ago=10,
                  files=None, additions=50, deletions=10,
                  reviews=None, ci_conclusion="SUCCESS",
                  merge_sha="a" * 40):
    """Build a GraphQL PR node."""
    merged_at = (datetime.now(timezone.utc) - timedelta(days=merged_days_ago)).isoformat()
    created_at = (datetime.now(timezone.utc) - timedelta(days=merged_days_ago + 1)).isoformat()

    return {
        "number": number,
        "title": title,
        "body": body,
        "mergedAt": merged_at,
        "createdAt": created_at,
        "additions": additions,
        "deletions": deletions,
        "author": {"login": "dev"},
        "mergeCommit": {"oid": merge_sha},
        "files": {"nodes": [{"path": f} for f in (files or ["src/app.py"])]},
        "reviews": {"nodes": reviews or []},
        "commits": {"nodes": [{
            "commit": {
                "statusCheckRollup": {
                    "contexts": {"nodes": [{"conclusion": ci_conclusion}]}
                }
            }
        }]},
    }


def _graphql_response(nodes, has_next=False, end_cursor=None):
    """Build a mock GraphQL response."""
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                }
            }
        }
    }


class TestParseCI(unittest.TestCase):

    def test_passed(self):
        pr = _make_pr_node(ci_conclusion="SUCCESS")
        self.assertEqual(github_graphql._parse_ci_status(pr), CIStatus.PASSED)

    def test_failed(self):
        pr = _make_pr_node(ci_conclusion="FAILURE")
        self.assertEqual(github_graphql._parse_ci_status(pr), CIStatus.FAILED)

    def test_no_checks(self):
        pr = {"commits": {"nodes": []}}
        self.assertEqual(github_graphql._parse_ci_status(pr), CIStatus.NO_CHECKS)

    def test_timed_out(self):
        pr = _make_pr_node(ci_conclusion="TIMED_OUT")
        self.assertEqual(github_graphql._parse_ci_status(pr), CIStatus.FAILED)


class TestParseReviews(unittest.TestCase):

    def test_approved(self):
        pr = {"reviews": {"nodes": [
            {"author": {"login": "alice"}, "state": "APPROVED",
             "submittedAt": "2026-03-01T00:00:00Z"}
        ]}}
        reviews = github_graphql._parse_reviews(pr)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].state, "approved")
        self.assertFalse(reviews[0].is_bot)

    def test_bot_reviewer(self):
        pr = {"reviews": {"nodes": [
            {"author": {"login": "coderabbitai[bot]"}, "state": "COMMENTED",
             "submittedAt": "2026-03-01T00:00:00Z"}
        ]}}
        reviews = github_graphql._parse_reviews(pr)
        self.assertTrue(reviews[0].is_bot)

    def test_empty_reviews(self):
        pr = {"reviews": {"nodes": []}}
        self.assertEqual(github_graphql._parse_reviews(pr), [])


class TestFetchChanges(unittest.TestCase):

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_single_page(self, mock_gql):
        pr = _make_pr_node(number=42, title="Fix bug PROJ-123")
        mock_gql.return_value = _graphql_response([pr])

        changes = github_graphql.fetch_changes("owner/repo", lookback_days=90)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].pr_number, 42)
        self.assertEqual(changes[0].source, "github_graphql")
        self.assertIn("PROJ-123", changes[0].ticket_ids)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_pagination(self, mock_gql):
        """Should follow cursor across multiple pages."""
        page1 = _graphql_response(
            [_make_pr_node(number=1)],
            has_next=True, end_cursor="cursor1",
        )
        page2 = _graphql_response(
            [_make_pr_node(number=2)],
            has_next=False,
        )
        mock_gql.side_effect = [page1, page2]

        changes = github_graphql.fetch_changes("owner/repo", lookback_days=90)

        self.assertEqual(len(changes), 2)
        self.assertEqual(mock_gql.call_count, 2)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_limit_stops_pagination(self, mock_gql):
        """limit=1 should stop after first matching PR."""
        prs = [_make_pr_node(number=i) for i in range(5)]
        mock_gql.return_value = _graphql_response(prs)

        changes = github_graphql.fetch_changes("owner/repo", lookback_days=90, limit=2)

        self.assertEqual(len(changes), 2)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_skips_prs_outside_lookback(self, mock_gql):
        """PRs merged before the lookback cutoff should be excluded."""
        old_pr = _make_pr_node(number=1, merged_days_ago=200)
        new_pr = _make_pr_node(number=2, merged_days_ago=10)
        mock_gql.return_value = _graphql_response([old_pr, new_pr])

        changes = github_graphql.fetch_changes("owner/repo", lookback_days=90)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].pr_number, 2)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_files_parsed(self, mock_gql):
        pr = _make_pr_node(files=["src/app.py", "src/utils.py"])
        mock_gql.return_value = _graphql_response([pr])

        changes = github_graphql.fetch_changes("owner/repo")

        self.assertEqual(changes[0].files, ["src/app.py", "src/utils.py"])

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_empty_repo(self, mock_gql):
        mock_gql.return_value = _graphql_response([])

        changes = github_graphql.fetch_changes("owner/repo")

        self.assertEqual(changes, [])

    def test_invalid_repo(self):
        with self.assertRaises(ValueError):
            github_graphql.fetch_changes("not-valid", lookback_days=90)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_additions_deletions(self, mock_gql):
        pr = _make_pr_node(additions=100, deletions=25)
        mock_gql.return_value = _graphql_response([pr])

        changes = github_graphql.fetch_changes("owner/repo")

        self.assertEqual(changes[0].additions, 100)
        self.assertEqual(changes[0].deletions, 25)


class TestAdaptivePageSize(unittest.TestCase):

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_halves_page_size_on_gateway_error(self, mock_gql):
        """502 should halve page size and retry same cursor."""
        pr = _make_pr_node(number=1)
        mock_gql.side_effect = [
            RuntimeError("GraphQL query failed: 502 Bad Gateway"),
            _graphql_response([pr]),
        ]

        changes = github_graphql.fetch_changes("owner/repo", page_size=100)

        self.assertEqual(len(changes), 1)
        self.assertEqual(mock_gql.call_count, 2)
        # Second call should have smaller pageSize
        second_call_vars = mock_gql.call_args_list[1][0][1]
        self.assertEqual(second_call_vars["pageSize"], 50)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_halves_repeatedly_until_success(self, mock_gql):
        """Multiple 502s should keep halving: 100 → 50 → 25 → success."""
        pr = _make_pr_node(number=1)
        mock_gql.side_effect = [
            RuntimeError("502"),
            RuntimeError("504"),
            _graphql_response([pr]),
        ]

        changes = github_graphql.fetch_changes("owner/repo", page_size=100)

        self.assertEqual(len(changes), 1)
        self.assertEqual(mock_gql.call_count, 3)
        third_call_vars = mock_gql.call_args_list[2][0][1]
        self.assertEqual(third_call_vars["pageSize"], 25)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_gives_up_at_min_page_size(self, mock_gql):
        """Should raise if still failing at MIN_PAGE_SIZE."""
        mock_gql.side_effect = RuntimeError("502")

        with self.assertRaises(RuntimeError):
            github_graphql.fetch_changes("owner/repo", page_size=5)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_grows_back_after_success(self, mock_gql):
        """After a successful small page, should try growing back."""
        page1_pr = _make_pr_node(number=1)
        page2_pr = _make_pr_node(number=2)
        mock_gql.side_effect = [
            RuntimeError("502"),                                    # 100 fails
            _graphql_response([page1_pr], has_next=True, end_cursor="c1"),  # 50 works
            _graphql_response([page2_pr]),                          # grows back to 100
        ]

        changes = github_graphql.fetch_changes("owner/repo", page_size=100)

        self.assertEqual(len(changes), 2)
        # First retry at 50, then grows back to 100
        third_call_vars = mock_gql.call_args_list[2][0][1]
        self.assertEqual(third_call_vars["pageSize"], 100)

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_non_gateway_error_raises_immediately(self, mock_gql):
        """Auth errors should not trigger backoff."""
        mock_gql.side_effect = RuntimeError("401 Unauthorized")

        with self.assertRaises(RuntimeError) as ctx:
            github_graphql.fetch_changes("owner/repo")

        self.assertIn("401", str(ctx.exception))
        self.assertEqual(mock_gql.call_count, 1)  # no retry

    @mock.patch.object(github_graphql, "_run_graphql")
    def test_pages_until_lookback_covered(self, mock_gql):
        """Should keep paging until all PRs in the window are fetched."""
        pages = []
        for i in range(5):
            pr = _make_pr_node(number=i + 1, merged_days_ago=10 + i * 15)
            has_next = i < 4
            pages.append(_graphql_response([pr], has_next=has_next,
                                           end_cursor=f"c{i}" if has_next else None))
        mock_gql.side_effect = pages

        changes = github_graphql.fetch_changes("owner/repo", lookback_days=90)

        # All 5 pages fetched, but only PRs within 90 days kept
        self.assertEqual(mock_gql.call_count, 5)
        for c in changes:
            age = (datetime.now(timezone.utc) - c.merged_at).days
            self.assertLessEqual(age, 90)


if __name__ == "__main__":
    unittest.main()
