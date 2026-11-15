from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import compare_reports as comparison
import contribution_report as report


def pr(number=1, owner="upstream", repository="project", **fields):
    node = {
        "url": f"https://github.com/{owner}/{repository}/pull/{number}",
        "number": number, "title": "Improve behavior", "state": "OPEN", "isDraft": False,
        "updatedAt": "2026-11-14T00:00:00Z", "mergedAt": None, "reviewDecision": None,
        "repository": {"nameWithOwner": f"{owner}/{repository}", "isFork": False,
                       "owner": {"login": owner}},
    }
    node.update(fields)
    return node


def snapshot(nodes=(), username="Doribelove", **options):
    page = {"nodes": list(nodes), "issueCount": len(nodes),
            "pageInfo": {"hasNextPage": False, "endCursor": None}}
    with patch("contribution_report.fetch_page", return_value=page):
        result = report.collect_report(username, **options)
    result["generated_at"] = "2026-11-15T00:00:00+00:00"
    return result


class ComparisonTests(unittest.TestCase):
    def test_empty_snapshots(self):
        result = comparison.compare_snapshots(snapshot(), snapshot())
        self.assertEqual(result["counts"], {
            "before": 0, "after": 0, "added": 0, "removed": 0, "changed": 0, "unchanged": 0,
        })
        self.assertIn("No pull request changes", comparison.to_markdown(result))

    def test_identical_snapshots_ignore_generation_time_and_input_order(self):
        before = snapshot([pr(2), pr(1)])
        after = snapshot([pr(1), pr(2)])
        after["generated_at"] = "2026-11-16T10:00:00+08:00"
        result = comparison.compare_snapshots(before, after)
        self.assertEqual(result["counts"]["unchanged"], 2)
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["before_generated_at"], before["generated_at"])
        self.assertEqual(result["after_generated_at"], after["generated_at"])

    def test_added_removed_and_unchanged_are_disjoint_and_sorted(self):
        result = comparison.compare_snapshots(snapshot([pr(4), pr(1), pr(2)]),
                                              snapshot([pr(10), pr(3), pr(2)]))
        self.assertEqual([item["number"] for item in result["added"]], [3, 10])
        self.assertEqual([item["number"] for item in result["removed"]], [1, 4])
        self.assertEqual(result["counts"], {
            "before": 3, "after": 3, "added": 2, "removed": 2, "changed": 0, "unchanged": 1,
        })
        self.assertIn("not deleted from GitHub", result["membership_note"])
        self.assertIn("last observed values", comparison.to_markdown(result))

    def test_all_observed_changes_include_before_and_after_values(self):
        original = pr(isDraft=True, reviewDecision="REVIEW_REQUIRED")
        updated = pr(state="MERGED", reviewDecision="APPROVED", title="Ready to merge",
                     updatedAt="2026-11-15T01:00:00Z", mergedAt="2026-11-15T00:00:00Z")
        updated["repository"]["isFork"] = True
        before, after = snapshot([original]), snapshot([updated])
        original_inputs = deepcopy((before, after))
        result = comparison.compare_snapshots(before, after)
        self.assertEqual(result["counts"]["changed"], 1)
        self.assertEqual(result["counts"]["unchanged"], 0)
        self.assertEqual(set(result["changed"][0]["changes"]), set(comparison.CHANGE_FIELDS))
        changes = result["changed"][0]["changes"]
        self.assertEqual(changes["state"], {"before": "OPEN", "after": "MERGED"})
        self.assertEqual(changes["reviewDecision"], {"before": "REVIEW_REQUIRED", "after": "APPROVED"})
        self.assertEqual(changes["isDraft"], {"before": True, "after": False})
        self.assertEqual(changes["mergedAt"]["before"], None)
        self.assertEqual(result["changed"][0]["after"], after["pull_requests"][0])
        self.assertEqual((before, after), original_inputs)
        markdown = comparison.to_markdown(result)
        self.assertIn("| state | OPEN | MERGED |", markdown)
        self.assertIn("| isDraft | true | false |", markdown)

    def test_review_changes_and_update_only_changes_are_visible(self):
        for fields in ({"reviewDecision": "CHANGES_REQUESTED"},
                       {"reviewDecision": "APPROVED"},
                       {"isDraft": True},
                       {"updatedAt": "2026-11-15T04:00:00Z"},
                       {"state": "CLOSED"}):
            with self.subTest(fields=fields):
                result = comparison.compare_snapshots(snapshot([pr()]), snapshot([pr(**fields)]))
                self.assertEqual(set(result["changed"][0]["changes"]), set(fields))

    def test_open_scope_removal_does_not_infer_merged_or_deleted(self):
        before = snapshot([pr()], state_filter="open")
        after = snapshot([pr(state="MERGED", mergedAt="2026-11-15T00:00:00Z")], state_filter="open")
        result = comparison.compare_snapshots(before, after)
        self.assertEqual(result["removed"][0]["state"], "OPEN")
        self.assertEqual(result["changed"], [])
        self.assertIn("filters", result["membership_note"])

    def test_author_and_repository_case_do_not_change_scope_or_identity(self):
        before = snapshot([pr(owner="Doribelove", repository="Project")],
                          include_own=True, repository="Doribelove/Project")
        after = snapshot([pr(owner="doribelove", repository="project")],
                         username="doribelove", include_own=True, repository="doribelove/project")
        after["search_query"] = "  " + after["search_query"].replace(" ", "  ")
        result = comparison.compare_snapshots(before, after)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["counts"]["added"], 0)
        self.assertEqual(result["counts"]["removed"], 0)

    def test_different_authors_are_rejected(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "different authors"):
            comparison.compare_snapshots(snapshot(), snapshot(username="someone-else"))

    def test_scope_and_search_changes_are_both_rejected(self):
        before = snapshot()
        alternatives = [snapshot(include_own=True), snapshot(state_filter="open"),
                        snapshot(repository="upstream/project"),
                        snapshot(updated_since="2026-11-01")]
        scope_only, search_only = deepcopy(before), deepcopy(before)
        scope_only["scope"] += "; merged since: 2026-11-01 (inclusive, UTC)"
        search_only["search_query"] += " merged:>=2026-11-01T00:00:00+00:00"
        alternatives.extend([scope_only, search_only])
        for after in alternatives:
            with self.subTest(scope=after["scope"], search=after["search_query"]):
                with self.assertRaisesRegex(comparison.ComparisonError, "different scopes or search filters"):
                    comparison.compare_snapshots(before, after)

    def test_additional_identical_scope_and_search_qualifiers_are_supported(self):
        before = snapshot()
        before["scope"] += "; merged since: 2026-11-01 (inclusive, UTC)"
        before["search_query"] += " merged:>=2026-11-01T00:00:00+00:00"
        result = comparison.compare_snapshots(before, deepcopy(before))
        self.assertEqual(result["counts"]["changed"], 0)

    def test_duplicate_pr_including_case_only_url_difference_is_rejected(self):
        for duplicate in (pr(), pr(owner="UPSTREAM", repository="PROJECT")):
            invalid = snapshot([pr()])
            duplicate["relationship"] = "external"
            invalid["pull_requests"].append(duplicate)
            with self.subTest(url=duplicate["url"]):
                with self.assertRaisesRegex(comparison.ComparisonError, "duplicate pull request"):
                    comparison.compare_snapshots(snapshot(), invalid)

    def test_unsafe_and_inconsistent_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "https://evil.test/upstream/project/pull/1",
                    "https://github.com@evil.test/upstream/project/pull/1",
                    "https://github.com/upstream/project/pull/1?query=x",
                    "https://github.com/upstream/project/pull/1#fragment",
                    "https://github.com/upstream/project/pull/2",
                    "\nhttps://github.com/upstream/project/pull/1",
                    "https://github.com/upstream/project/pull/1\t"):
            invalid = snapshot([pr()])
            invalid["pull_requests"][0]["url"] = url
            with self.subTest(url=url), self.assertRaises(comparison.ComparisonError):
                comparison.compare_snapshots(snapshot(), invalid)

    def test_markdown_escapes_titles_for_added_removed_and_changed_records(self):
        title = '[link](https://evil.test) | <script>\n**bold** `code` ~~gone~~ \\ 测试 🚀'
        before = snapshot([pr(1, title=title), pr(2)])
        after = snapshot([pr(2, title=title), pr(3, title=title)])
        result = comparison.compare_snapshots(before, after)
        markdown = comparison.to_markdown(result)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[link](https://evil.test)", markdown)
        self.assertIn(r"\[link\]\(https://evil.test\) \| &lt;script&gt;", markdown)
        self.assertIn(r"\~\~gone\~\~", markdown)
        self.assertEqual(len([line for line in markdown.splitlines() if line.startswith("| ")]), 9)
        self.assertEqual(result["added"][0]["title"], title)
        self.assertEqual(result["removed"][0]["title"], title)
        self.assertEqual(result["changed"][0]["changes"]["title"]["after"], title)

    def test_invalid_snapshot_structure_and_counts_are_rejected(self):
        cases = [None, [], {}, {"username": "Doribelove"}]
        for field, value in (("username", None), ("username", "someone is:private"),
                             ("generated_at", "yesterday"), ("scope", ""),
                             ("search_query", []), ("pull_requests", {}),
                             ("public_authored_total", True), ("public_authored_total", -1),
                             ("own_repository_total", 2), ("counts", [])):
            invalid = snapshot([pr()])
            invalid[field] = value
            cases.append(invalid)
        for field in ("total", "merged", "open", "closed_unmerged", "draft"):
            invalid = snapshot([pr()])
            invalid["counts"][field] += 1
            cases.append(invalid)
        for field, value in (("number", True), ("state", []), ("isDraft", "false"),
                             ("reviewDecision", "UNKNOWN"), ("relationship", "own"),
                             ("updatedAt", "2026-02-30T00:00:00Z"),
                             ("updatedAt", "2026-11-15T00:00:00"),
                             ("mergedAt", "invalid"), ("title", "\ud800")):
            invalid = snapshot([pr()])
            invalid["pull_requests"][0][field] = value
            cases.append(invalid)
        for index, invalid in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(comparison.ComparisonError):
                comparison.compare_snapshots(snapshot(), invalid)

    def test_query_author_must_agree_with_snapshot_author(self):
        for query in ("is:pr is:public author:someone-else", "is:pr is:public",
                      "is:pr is:public author:Doribelove author:Doribelove",
                      "is:pr author:Doribelove", "is:pr is:public author:Doribelove repo:../repo"):
            invalid = snapshot()
            invalid["search_query"] = query
            with self.subTest(query=query), self.assertRaises(comparison.ComparisonError):
                comparison.compare_snapshots(snapshot(), invalid)


class ComparisonCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.before = Path(self.temporary.name) / "before report.json"
        self.after = Path(self.temporary.name) / "after report.json"
        self.before.write_text(json.dumps(snapshot()), encoding="utf-8")
        self.after.write_text(json.dumps(snapshot([pr(title="修复测试 🚀")])), encoding="utf-8")

    def run_cli(self, *arguments):
        # gh is deliberately unavailable: comparisons only read the two files.
        env = dict(os.environ, PATH="", PYTHONIOENCODING="ascii", PYTHONUTF8="0")
        return subprocess.run([sys.executable, str(Path(comparison.__file__)),
                               str(self.before), str(self.after), *arguments],
                              capture_output=True, env=env, timeout=10, check=False)

    def test_cli_json_is_utf8_machine_readable_without_gh(self):
        result = self.run_cli("--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(payload["added"][0]["title"], "修复测试 🚀")
        self.assertEqual(payload["counts"]["added"], 1)

    def test_cli_markdown_default_and_utf8_bom_input(self):
        self.after.write_text(json.dumps(snapshot([pr(title="修复测试 🚀")])), encoding="utf-8-sig")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Added to snapshot", result.stdout.decode("utf-8"))
        self.assertIn("修复测试 🚀", result.stdout.decode("utf-8"))

    def test_cli_invalid_files_have_friendly_errors_and_empty_stdout(self):
        for content in (b"not JSON", b"\xff", b"[]", b'{"username":"a","username":"b"}',
                        b'{"invalid":NaN}', b'{"integer":' + b"9" * 5000 + b"}"):
            self.after.write_bytes(content)
            with self.subTest(content=content[:30]):
                result = self.run_cli("--format", "json")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr.startswith(b"error: after snapshot:"))
                self.assertNotIn(b"Traceback", result.stderr)

    def test_cli_scope_mismatch_and_missing_file_leave_stdout_empty(self):
        self.after.write_text(json.dumps(snapshot(include_own=True)), encoding="utf-8")
        mismatch = self.run_cli()
        self.assertEqual(mismatch.returncode, 1)
        self.assertEqual(mismatch.stdout, b"")
        self.assertIn(b"different scopes or search filters", mismatch.stderr)
        self.before.unlink()
        missing = self.run_cli()
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, b"")
        self.assertIn(b"before snapshot: could not read", missing.stderr)

    def test_main_supports_redirected_text_streams(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = comparison.main([str(self.before), str(self.after), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["counts"]["added"], 1)


if __name__ == "__main__":
    unittest.main()
