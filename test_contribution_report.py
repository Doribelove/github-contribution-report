import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import contribution_report as report


def pr(number=1, owner="upstream", state="OPEN", draft=False, title="Fix behavior"):
    return {
        "url": f"https://github.com/{owner}/project/pull/{number}",
        "title": title, "number": number, "state": state, "isDraft": draft,
        "mergedAt": "2026-09-15T00:00:00Z" if state == "MERGED" else None,
        "updatedAt": "2026-09-15T00:00:00Z", "reviewDecision": None,
        "repository": {"nameWithOwner": f"{owner}/project", "isFork": False, "owner": {"login": owner}},
    }


def response(nodes, count=None, more=False, cursor=None):
    payload = {"data": {"search": {
        "nodes": nodes, "issueCount": len(nodes) if count is None else count,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }}}
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


class ReportTests(unittest.TestCase):
    @patch("contribution_report.subprocess.run")
    def test_pagination_uses_cursor_and_fixed_public_host(self, run):
        run.side_effect = [response([pr(1)], 2, True, "cursor-1"), response([pr(2)], 2)]
        result = report.collect_report("Doribelove", gh="/opt/custom gh")
        self.assertEqual(result["counts"]["total"], 2)
        self.assertEqual(len(run.call_args_list), 2)
        first, second = run.call_args_list
        self.assertEqual(first.args[0], ["/opt/custom gh", "api", "graphql", "--hostname", "github.com", "--input", "-"])
        self.assertEqual(json.loads(first.kwargs["input"])["variables"], {
            "search": "is:pr is:public author:Doribelove sort:updated-desc", "after": None,
        })
        self.assertEqual(json.loads(second.kwargs["input"])["variables"]["after"], "cursor-1")
        self.assertNotIn("shell", first.kwargs)

    @patch("contribution_report.subprocess.run")
    def test_counts_distinguish_closed_merged_drafts_and_own_repos(self, run):
        nodes = [pr(1, state="MERGED"), pr(2, state="CLOSED"), pr(3, draft=True),
                 pr(4, owner="doribelove", state="MERGED")]
        run.return_value = response(nodes)
        result = report.collect_report("Doribelove")
        self.assertEqual(result["counts"], {"total": 3, "merged": 1, "open": 1, "closed_unmerged": 1, "draft": 1})
        self.assertEqual(result["public_authored_total"], 4)
        self.assertEqual(result["own_repository_total"], 1)
        self.assertTrue(all(item["relationship"] == "external" for item in result["pull_requests"]))
        included = report.collect_report("Doribelove", include_own=True)
        self.assertEqual(included["counts"]["merged"], 2)
        self.assertEqual(included["pull_requests"][-1]["relationship"], "own")

    @patch("contribution_report.subprocess.run")
    def test_other_owners_fork_is_external_and_retains_fork_flag(self, run):
        node = pr()
        node["repository"]["isFork"] = True
        run.return_value = response([node])
        result = report.collect_report("Doribelove")
        self.assertTrue(result["pull_requests"][0]["repository"]["isFork"])
        self.assertEqual(result["pull_requests"][0]["relationship"], "external")

    @patch("contribution_report.subprocess.run", return_value=response([]))
    def test_empty_result_is_successful_report(self, run):
        result = report.collect_report("no-matches")
        self.assertEqual(result["counts"]["total"], 0)
        self.assertIn("No matching public pull requests.", report.to_markdown(result))

    @patch("contribution_report.subprocess.run", return_value=response([], 1001))
    def test_search_cap_fails_before_reporting_partial_data(self, run):
        with self.assertRaisesRegex(report.ReportError, "1000-result search cap"):
            report.collect_report("Doribelove")

    @patch("contribution_report.subprocess.run")
    def test_exact_search_cap_with_complete_pages_succeeds(self, run):
        run.side_effect = [
            response([pr(n) for n in range(start, start + 100)], 1000,
                     more=start < 901, cursor=f"cursor-{start}")
            for start in range(1, 1001, 100)
        ]
        result = report.collect_report("Doribelove")
        self.assertEqual(result["counts"]["total"], 1000)
        self.assertEqual(run.call_count, 10)

    @patch("contribution_report.subprocess.run")
    def test_incomplete_and_unstable_pages_fail(self, run):
        scenarios = {
            "missing PR": [response([pr(1)], 2)],
            "count changed": [response([pr(1)], 2, True, "c1"), response([pr(2)], 3)],
            "duplicate PR": [response([pr(1)], 2, True, "c1"), response([pr(1)], 2)],
            "missing cursor": [response([pr(1)], 2, True)],
            "repeated cursor": [response([pr(1)], 3, True, "c1"), response([pr(2)], 3, True, "c1")],
            "empty next page": [response([], 1, True, "c1")],
        }
        for name, pages in scenarios.items():
            with self.subTest(name=name):
                run.side_effect = pages
                with self.assertRaises(report.ReportError):
                    report.collect_report("Doribelove")

    @patch("contribution_report.subprocess.run")
    def test_transport_failures_and_invalid_responses(self, run):
        failures = [
            subprocess.CompletedProcess([], 1, "PRIVATE DEBUG VALUE", "PRIVATE DEBUG VALUE"),
            subprocess.CompletedProcess([], 0, '{"errors":[{"message":"bad query"}]}', ""),
            subprocess.CompletedProcess([], 0, '{"data":null}', ""),
            subprocess.CompletedProcess([], 0, 'not JSON', ""),
            subprocess.CompletedProcess([], 0, '[]', ""),
            response([None]),
            response([{}]),
        ]
        for item in failures:
            with self.subTest(response=item.stdout):
                run.return_value = item
                with self.assertRaises(report.ReportError) as error:
                    report.collect_report("Doribelove")
                self.assertNotIn("PRIVATE DEBUG VALUE", str(error.exception))

    @patch("contribution_report.subprocess.run")
    def test_missing_cli_and_timeout_have_actionable_errors(self, run):
        for error, message in [(FileNotFoundError(), "could not execute"),
                               (subprocess.TimeoutExpired("gh", 60), "timed out")]:
            with self.subTest(error=error):
                run.side_effect = error
                with self.assertRaisesRegex(report.ReportError, message):
                    report.collect_report("Doribelove")

    @patch("contribution_report.subprocess.run")
    def test_unsafe_username_never_reaches_subprocess(self, run):
        for username in ["", "https://github.com/user", "user is:private", "-user", "user--name", "x" * 40, "$(id)"]:
            with self.subTest(username=username):
                with self.assertRaises(argparse.ArgumentTypeError):
                    report.collect_report(username)
        run.assert_not_called()

    @patch("contribution_report.subprocess.run")
    def test_markdown_title_is_escaped_without_injecting_table_or_html(self, run):
        title = "[click](https://evil.test) | <script>\n**text** `code` " + chr(92)
        run.return_value = response([pr(title=title)])
        output = report.to_markdown(report.collect_report("Doribelove"))
        self.assertIn(r"\[click\]\(https://evil.test\) \| &lt;script&gt;", output)
        self.assertNotIn("<script>", output)
        self.assertIn(r"\*\*text\*\* \`code\`", output)
        self.assertEqual(len([line for line in output.splitlines() if line.startswith("| ")]), 3)

    @patch("contribution_report.subprocess.run")
    def test_noncanonical_or_unsafe_links_are_rejected(self, run):
        for url in ["javascript:alert(1)", "https://evil.test/upstream/project/pull/1", "https://github.com/upstream/project/pull/1?x=y"]:
            node = pr()
            node["url"] = url
            run.return_value = response([node])
            with self.subTest(url=url), self.assertRaises(report.ReportError):
                report.collect_report("Doribelove")

    @patch("contribution_report.subprocess.run", return_value=response([pr()]))
    def test_cli_json_is_machine_readable(self, run):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(report.main(["Doribelove", "--format", "json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["counts"]["open"], 1)

    @patch("contribution_report.subprocess.run", return_value=response([], 1001))
    def test_cli_failure_leaves_stdout_empty(self, run):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(report.main(["Doribelove"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("1000-result search cap", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
