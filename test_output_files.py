"""File exports preserve previous reports when generation or saving fails."""

from contextlib import redirect_stderr, redirect_stdout
import csv
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
from test_contribution_report import pr, response
from test_compare_reports import snapshot


class OutputFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "saved report.json"
        self.previous = b'{"previous": "valid snapshot"}\n'
        self.output.write_bytes(self.previous)

    def assert_preserved(self):
        self.assertEqual(self.output.read_bytes(), self.previous)
        self.assertEqual(list(self.directory.glob(".contribution-report-*.tmp")), [])

    def test_new_and_existing_files_receive_exact_utf8_bytes(self):
        content = '修复导航 🚀\n"quoted",value\r\n'
        for path in (self.output, self.directory / "new report.csv"):
            with self.subTest(path=path):
                output = io.StringIO()
                with redirect_stdout(output):
                    report.write_output(content, path)
                self.assertEqual(path.read_bytes(), content.encode("utf-8"))
                self.assertEqual(output.getvalue(), "")
        self.assertEqual(list(self.directory.glob(".contribution-report-*.tmp")), [])

    def test_encoding_failure_keeps_existing_report(self):
        with self.assertRaises(report.ReportError):
            report.write_output("invalid surrogate: \ud800", self.output)
        self.assert_preserved()

    def test_replace_failure_keeps_existing_report_and_removes_temporary_file(self):
        with patch("contribution_report.os.replace", side_effect=PermissionError("locked file")):
            with self.assertRaises(report.ReportError):
                report.write_output("new contents", self.output)
        self.assert_preserved()

    def test_partial_write_failure_keeps_existing_report_and_cleans_up(self):
        create_temporary = tempfile.NamedTemporaryFile

        def fail_after_partial_write(**kwargs):
            stream = create_temporary(**kwargs)
            write = stream.write

            def partial(data):
                write(data[:3])
                raise OSError("disk full")

            stream.write = partial
            return stream

        with patch("contribution_report.tempfile.NamedTemporaryFile", side_effect=fail_after_partial_write):
            with self.assertRaises(report.ReportError):
                report.write_output("new contents", self.output)
        self.assert_preserved()

    def test_unusable_destinations_produce_report_errors(self):
        for path in (self.directory / "missing" / "report.json", self.directory):
            with self.subTest(path=path), self.assertRaises(report.ReportError):
                report.write_output("new contents", path)
            self.assert_preserved()

    def test_symlink_output_preserves_link_and_target(self):
        link = self.directory / "linked.json"
        try:
            link.symlink_to(self.output)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(report.ReportError, "symbolic link"):
            report.write_output("new contents", link)
        self.assertTrue(link.is_symlink())
        self.assert_preserved()

    def test_report_cli_writes_all_formats_without_stdout(self):
        title = "修复导航 🚀"
        for format in ("json", "markdown", "csv"):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(format=format), redirect_stdout(output), redirect_stderr(errors):
                with patch("contribution_report.subprocess.run", return_value=response([pr(title=title)])):
                    code = report.main(["Doribelove", "--format", format, "--output", str(self.output)])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
            text = self.output.read_text(encoding="utf-8")
            self.assertIn(title, text)
            if format == "json":
                self.assertEqual(json.loads(text)["counts"]["total"], 1)
            elif format == "csv":
                self.assertEqual(list(csv.DictReader(io.StringIO(text)))[0]["title"], title)

    def test_report_api_failure_preserves_existing_output(self):
        with patch("contribution_report.collect_report", side_effect=report.ReportError("API failure")):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                code = report.main(["Doribelove", "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("API failure", errors.getvalue())
        self.assert_preserved()

    def test_report_save_failure_is_friendly_and_preserves_file(self):
        with patch("contribution_report.subprocess.run", return_value=response([])):
            with patch("contribution_report.os.replace", side_effect=PermissionError("locked")):
                with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                    code = report.main(["Doribelove", "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("could not save output", errors.getvalue())
        self.assert_preserved()

    def test_real_report_cli_saves_unicode_under_legacy_stdout_encoding(self):
        page = json.loads(response([pr(title="修复导航 🚀")]).stdout)["data"]["search"]
        script = (
            "from unittest.mock import patch\n"
            "import contribution_report as r\n"
            f"with patch('contribution_report.fetch_page', return_value={page!r}):\n"
            "    raise SystemExit(r.main())\n"
        )
        process = subprocess.run([sys.executable, "-c", script, "Doribelove", "--format", "json",
                                  "--output", str(self.output)], capture_output=True, timeout=10,
                                 env=dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0"))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, b"")
        self.assertEqual(json.loads(self.output.read_bytes())["pull_requests"][0]["title"], "修复导航 🚀")


class ComparisonOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.before, self.after = self.directory / "before.json", self.directory / "after.json"
        self.before.write_text(json.dumps(snapshot()), encoding="utf-8")
        self.after.write_text(json.dumps(snapshot([pr(title="修复导航 🚀")])), encoding="utf-8")
        self.output = self.directory / "changes.json"
        self.output.write_bytes(b"previous comparison\n")

    def run_cli(self, path=None, format="json"):
        return subprocess.run([sys.executable, str(Path(comparison.__file__)),
                               str(self.before), str(self.after), "--format", format,
                               "--output", str(path or self.output)], capture_output=True, timeout=10,
                              env=dict(os.environ, PATH="", PYTHONIOENCODING="ascii", PYTHONUTF8="0"))

    def test_real_comparison_cli_writes_utf8_json_and_markdown_without_stdout(self):
        for format in ("json", "markdown"):
            result = self.run_cli(format=format)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertIn("修复导航 🚀", self.output.read_text(encoding="utf-8"))

    def test_invalid_snapshot_preserves_previous_comparison(self):
        self.after.write_bytes(b"invalid JSON")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith(b"error: after snapshot:"))
        self.assertEqual(self.output.read_bytes(), b"previous comparison\n")

    def test_output_cannot_overwrite_either_input(self):
        for source in (self.before, self.after):
            previous = source.read_bytes()
            result = self.run_cli(source)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"different from both input snapshots", result.stderr)
            self.assertEqual(source.read_bytes(), previous)

    def test_hard_link_to_input_is_rejected(self):
        linked = self.directory / "hardlink.json"
        try:
            os.link(self.after, linked)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        previous = self.after.read_bytes()
        result = self.run_cli(linked)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"different from both input snapshots", result.stderr)
        self.assertEqual(self.after.read_bytes(), previous)
        self.assertEqual(linked.read_bytes(), previous)

    def test_unwritable_comparison_destination_has_no_traceback(self):
        result = self.run_cli(self.directory)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"could not save output", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(list(self.directory.glob(".contribution-report-*.tmp")), [])

    def test_symlink_loop_destination_has_no_traceback(self):
        loop = self.directory / "loop"
        try:
            loop.symlink_to(loop)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        result = self.run_cli(loop)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertTrue(loop.is_symlink())


if __name__ == "__main__":
    unittest.main()
