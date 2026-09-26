import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yuc.__main__ import main
from yuc.report import annotations, summary_markdown


def run_report(state="healthy", consecutive=0):
    failure = {"url": "https://yuc.wiki/new/", "pageId": "new",
               "kind": "ValueError", "message": "incomplete upcoming article"}
    return {
        "state": state, "published": True, "completedAt": "2026-09-26T00:00:00+00:00",
        "full": False, "consecutiveFailures": consecutive, "pages": 1,
        "changedFiles": ["status.json"], "sourceChanges": [],
        "discovery": {"state": "current"},
        "pageStates": [{"url": failure["url"], "state": "current" if state == "healthy" else "stale"}],
        "failures": [] if state == "healthy" else [failure],
    }


class CliTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}))

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["yuc", *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main()
        return code, output.getvalue(), errors.getvalue()

    def test_sync_writes_reports_and_passes_custom_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("yuc.__main__.sync", return_value=run_report()) as synchronize:
                code, _, _ = self.invoke("sync", "--full", "--data-dir", str(root / "data"),
                                         "--report-dir", str(root / "reports"))
            self.assertEqual(code, 0)
            synchronize.assert_called_once_with(root / "data", full=True, report_dir=root / "reports")
            self.assertEqual(json.loads((root / "reports/sync.json").read_text())["state"], "healthy")
            self.assertIn("current 1", (root / "reports/summary.md").read_text())

    def test_degraded_is_publishable_exit_two_with_warning_then_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            for consecutive, level in ((1, "warning"), (3, "error")):
                with self.subTest(consecutive=consecutive), patch("yuc.__main__.sync", return_value=run_report("degraded", consecutive)):
                    code, output, _ = self.invoke("sync", "--report-dir", tmp)
                    self.assertEqual(code, 2)
                    self.assertIn(f"::{level}::https://yuc.wiki/new/", output)
                    report = json.loads((Path(tmp) / "sync.json").read_text())
                    self.assertTrue(report["published"])
                    self.assertEqual(report["state"], "degraded")
                    self.assertIn("stale 1", (Path(tmp) / "summary.md").read_text())

    def test_fatal_replaces_previous_success_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sync.json").write_text('{"state":"healthy"}')
            (root / "summary.md").write_text("old successful run")

            def fail(*args, **kwargs):
                self.assertFalse((root / "sync.json").exists())
                self.assertFalse((root / "summary.md").exists())
                raise ValueError("existing raw snapshot corrupt")

            with patch("yuc.__main__.sync", side_effect=fail):
                code, _, _ = self.invoke("sync", "--report-dir", tmp)
            self.assertEqual(code, 1)
            result = json.loads((root / "sync.json").read_text())
            self.assertEqual(result["state"], "fatal")
            self.assertFalse(result["published"])
            self.assertEqual(result["failures"][0]["kind"], "ValueError")
            self.assertNotIn("old successful run", (root / "summary.md").read_text())

    def test_unwritable_report_destination_fails_without_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report-file"
            path.write_text("occupied")
            with patch("yuc.__main__.sync") as synchronize:
                code, output, error = self.invoke("sync", "--report-dir", str(path))
            self.assertEqual(code, 1)
            synchronize.assert_not_called()
            self.assertEqual(json.loads(output)["state"], "fatal")
            self.assertIn("ERROR writing run report", error)

    def test_health_requires_both_healthy_and_fresh(self):
        for state, fresh, code in (("healthy", True, 0), ("degraded", True, 1), ("healthy", False, 1)):
            with self.subTest(state=state, fresh=fresh), patch("yuc.__main__.check_status", return_value={
                    "state": state, "fresh": fresh, "failures": []}) as check:
                actual, _, _ = self.invoke("health", "--data-dir", "fixture-data")
                self.assertEqual(actual, code)
                check.assert_called_once_with(Path("fixture-data"))

    def test_health_missing_or_invalid_status_is_failure(self):
        with patch("yuc.__main__.check_status", side_effect=ValueError("status missing; run sync")):
            code, _, error = self.invoke("health")
        self.assertEqual(code, 1)
        self.assertIn("run sync", error)

    def test_summary_and_annotations_escape_source_text(self):
        report = run_report("degraded", 1)
        report["failures"][0]["message"] = "100%\r\n::error::injected | [x](https://example.com) <script>alert(1)</script>"
        emitted = annotations(report)
        self.assertEqual(len(emitted), 1)
        self.assertIn("100%25%0D%0A::error::injected", emitted[0])
        self.assertNotIn("\n", emitted[0])
        summary = summary_markdown(report)
        self.assertNotIn("<script>", summary)
        self.assertNotIn("[x](https://example.com)", summary)
        self.assertIn("&#124;", summary)
        self.assertIn("&lt;script&gt;", summary)


if __name__ == "__main__":
    unittest.main()
