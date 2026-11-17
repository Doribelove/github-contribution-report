"""Both CLIs report stdout failures before successful process exit."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import compare_reports as comparison
import contribution_report as report
from test_compare_reports import pr, snapshot


class StdoutTests(unittest.TestCase):
    def test_write_and_flush_failures_raise_report_errors(self):
        for binary in (False, True):
            for failing_operation in ("write", "flush"):
                with self.subTest(binary=binary, operation=failing_operation):
                    stream = Mock(spec=io.BytesIO if binary else io.StringIO)
                    stream.fileno.side_effect = io.UnsupportedOperation("no descriptor")
                    getattr(stream, failing_operation).side_effect = OSError("disk full")
                    stdout = SimpleNamespace(buffer=stream) if binary else stream
                    with redirect_stdout(stdout), self.assertRaisesRegex(
                        report.ReportError, "could not write output to stdout"
                    ):
                        report.write_output("report contents\n")


class StdoutCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.before = directory / "before.json"
        self.after = directory / "after.json"
        self.before.write_text(json.dumps(snapshot()), encoding="utf-8")

    def commands(self, large):
        nodes = [pr(number=number, title="修复导航 🚀")
                 for number in range(1, 51 if large else 2)]
        self.after.write_text(json.dumps(snapshot(nodes)), encoding="utf-8")
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "import contribution_report as report\n"
            "data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
            "with patch('contribution_report.collect_report', return_value=data):\n"
            "    raise SystemExit(report.main(['Doribelove', '--format', 'json']))\n"
        )
        return (
            [sys.executable, "-c", script, str(self.after)],
            [sys.executable, str(Path(comparison.__file__)), str(self.before),
             str(self.after), "--format", "json"],
        )

    def run_cli(self, command, stdout):
        return subprocess.run(command, stdout=stdout, stderr=subprocess.PIPE, timeout=10,
                              env=dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0"))

    def assert_stdout_failure(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stderr.startswith(b"error: could not write output to stdout"),
                        result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertNotIn(b"Exception ignored", result.stderr)

    def test_closed_pipe_has_a_clean_error_for_small_and_large_reports(self):
        for large in (False, True):
            for command in self.commands(large):
                with self.subTest(large=large, command=command[:2]):
                    read_fd, write_fd = os.pipe()
                    os.close(read_fd)
                    try:
                        self.assert_stdout_failure(self.run_cli(command, write_fd))
                    finally:
                        os.close(write_fd)

    @unittest.skipUnless(Path("/dev/full").exists(), "/dev/full is unavailable")
    def test_full_destination_has_a_clean_error_for_small_and_large_reports(self):
        for large in (False, True):
            for command in self.commands(large):
                with self.subTest(large=large, command=command[:2]), open("/dev/full", "wb") as full:
                    self.assert_stdout_failure(self.run_cli(command, full))

    def test_normal_pipe_receives_complete_utf8_reports(self):
        for large in (False, True):
            for command in self.commands(large):
                with self.subTest(large=large, command=command[:2]):
                    result = self.run_cli(command, subprocess.PIPE)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    data = json.loads(result.stdout.decode("utf-8"))
                    rows = data.get("pull_requests", data.get("added"))
                    self.assertEqual(len(rows), 50 if large else 1)
                    self.assertTrue(all(row["title"] == "修复导航 🚀" for row in rows))


if __name__ == "__main__":
    unittest.main()
