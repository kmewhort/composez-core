import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from composez_core.vale_linter import (
    DEFAULT_VALE_INI,
    ValeLinter,
    ValeLintResult,
    _styles_dir,
    _sync_timestamp,
    init_vale_config,
    make_markdown_linter,
    vale_available,
    vale_sync,
)


class TestValeLintResult(unittest.TestCase):
    """Test the ValeLintResult dataclass."""

    def test_defaults(self):
        r = ValeLintResult()
        self.assertEqual(r.text, "")
        self.assertEqual(r.lines, [])
        self.assertEqual(r.warnings, [])

    def test_with_values(self):
        r = ValeLintResult(
            text="some output",
            lines=[1, 5],
            warnings=[{"line": 1, "message": "test"}],
        )
        self.assertEqual(r.text, "some output")
        self.assertEqual(r.lines, [1, 5])
        self.assertEqual(len(r.warnings), 1)


class TestValeAvailable(unittest.TestCase):
    """Test vale_available function."""

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_available(self, _):
        self.assertTrue(vale_available())

    @patch("composez_core.vale_linter._vale_bin", return_value=None)
    def test_not_available(self, _):
        self.assertFalse(vale_available())


class TestInitValeConfig(unittest.TestCase):
    """Test init_vale_config function."""

    def test_creates_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = init_vale_config(tmpdir)
            config_path = os.path.join(tmpdir, ".vale.ini")
            self.assertEqual(result, config_path)
            self.assertTrue(os.path.isfile(config_path))
            content = Path(config_path).read_text(encoding="utf-8")
            self.assertIn("StylesPath", content)
            self.assertIn("write-good", content)
            self.assertIn("proselint", content)

    def test_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, ".vale.ini")
            Path(config_path).write_text("custom config", encoding="utf-8")
            result = init_vale_config(tmpdir)
            self.assertIsNone(result)
            content = Path(config_path).read_text(encoding="utf-8")
            self.assertEqual(content, "custom config")

    def test_default_ini_content(self):
        self.assertIn("MinAlertLevel", DEFAULT_VALE_INI)
        self.assertIn("Packages", DEFAULT_VALE_INI)
        self.assertIn("[*.{md,txt}]", DEFAULT_VALE_INI)
        self.assertIn("BasedOnStyles", DEFAULT_VALE_INI)
        self.assertIn("ai-tells", DEFAULT_VALE_INI)
        self.assertIn(".vale-styles", DEFAULT_VALE_INI)


class TestValeSync(unittest.TestCase):
    """Test vale_sync function."""

    @patch("composez_core.vale_linter._vale_bin", return_value=None)
    def test_skips_when_not_available(self, _):
        self.assertFalse(vale_sync("/tmp"))

    @patch("composez_core.vale_linter.subprocess.run")
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_runs_vale_sync(self, _, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .vale.ini so _styles_dir reads it
            Path(os.path.join(tmpdir, ".vale.ini")).write_text(
                DEFAULT_VALE_INI, encoding="utf-8"
            )
            result = vale_sync(tmpdir)
            self.assertTrue(result)
            mock_run.assert_called_once()
            args = mock_run.call_args
            self.assertEqual(args[0][0], ["/path/to/vale_bin", "sync"])
            # Verify sync-timestamp was written
            ts_path = _sync_timestamp(tmpdir)
            self.assertTrue(os.path.isfile(ts_path))

    @patch("composez_core.vale_linter.subprocess.run", side_effect=OSError("fail"))
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_handles_error(self, _, __):
        self.assertFalse(vale_sync("/tmp"))


class TestValeLinterParseJson(unittest.TestCase):
    """Test the JSON parsing logic."""

    def setUp(self):
        self.linter = ValeLinter(root="/tmp")

    def test_empty_input(self):
        self.assertEqual(self.linter._parse_json(""), [])
        self.assertEqual(self.linter._parse_json(None), [])
        self.assertEqual(self.linter._parse_json("   "), [])

    def test_invalid_json(self):
        self.assertEqual(self.linter._parse_json("not json"), [])

    def test_valid_output(self):
        data = {
            "scene.txt": [
                {
                    "Check": "write-good.Passive",
                    "Line": 5,
                    "Message": "\"was killed\" may be passive voice.",
                    "Severity": "suggestion",
                    "Match": "was killed",
                },
                {
                    "Check": "proselint.Clichés",
                    "Line": 12,
                    "Message": "\"tip of the iceberg\" is a cliché.",
                    "Severity": "warning",
                    "Match": "tip of the iceberg",
                },
            ]
        }
        warnings = self.linter._parse_json(json.dumps(data))
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0]["line"], 5)
        self.assertEqual(warnings[0]["check"], "write-good.Passive")
        self.assertEqual(warnings[0]["severity"], "suggestion")
        self.assertEqual(warnings[1]["line"], 12)
        self.assertEqual(warnings[1]["severity"], "warning")

    def test_multiple_files(self):
        data = {
            "a.txt": [
                {"Check": "c1", "Line": 1, "Message": "m1", "Severity": "error", "Match": "x"},
            ],
            "b.txt": [
                {"Check": "c2", "Line": 3, "Message": "m2", "Severity": "warning", "Match": "y"},
            ],
        }
        warnings = self.linter._parse_json(json.dumps(data))
        self.assertEqual(len(warnings), 2)


class TestValeLinterFormatWarnings(unittest.TestCase):
    """Test warning formatting."""

    def setUp(self):
        self.linter = ValeLinter(root="/tmp")

    def test_format_by_severity(self):
        warnings = [
            {"line": 1, "check": "c1", "message": "err msg", "severity": "error", "match": "x"},
            {"line": 5, "check": "c2", "message": "warn msg", "severity": "warning", "match": "y"},
            {"line": 9, "check": "c3", "message": "sug msg", "severity": "suggestion", "match": "z"},
        ]
        text = self.linter._format_warnings("test.txt", warnings)
        self.assertIn("## Vale lint: test.txt", text)
        self.assertIn("### error (1)", text)
        self.assertIn("### warning (1)", text)
        self.assertIn("### suggestion (1)", text)
        self.assertIn("[c1] err msg", text)
        self.assertIn("Line 1:", text)
        self.assertIn("Line 5:", text)

    def test_empty_warnings(self):
        text = self.linter._format_warnings("test.txt", [])
        self.assertIn("## Vale lint:", text)


class TestValeLinterLint(unittest.TestCase):
    """Test the main lint method."""

    @patch("composez_core.vale_linter._vale_bin", return_value=None)
    def test_not_installed(self, _):
        linter = ValeLinter(root="/tmp")
        result = linter.lint("test.txt")
        self.assertIsNotNone(result)
        self.assertIn("not installed", result.text)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale", return_value=None)
    def test_vale_returns_none(self, _, __):
        linter = ValeLinter(root="/tmp")
        result = linter.lint("test.txt")
        self.assertIsNone(result)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_clean_file(self, mock_run, _):
        mock_run.return_value = json.dumps({})
        linter = ValeLinter(root="/tmp")
        result = linter.lint("test.txt")
        self.assertIsNone(result)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_file_with_issues(self, mock_run, _):
        data = {
            "test.txt": [
                {
                    "Check": "write-good.Passive",
                    "Line": 3,
                    "Message": "passive voice",
                    "Severity": "suggestion",
                    "Match": "was seen",
                },
                {
                    "Check": "proselint.Clichés",
                    "Line": 7,
                    "Message": "cliché",
                    "Severity": "warning",
                    "Match": "crystal clear",
                },
            ]
        }
        mock_run.return_value = json.dumps(data)
        linter = ValeLinter(root="/tmp")
        result = linter.lint("test.txt")
        self.assertIsNotNone(result)
        self.assertEqual(result.lines, [3, 7])
        self.assertEqual(len(result.warnings), 2)
        self.assertIn("Vale lint:", result.text)


class TestValeLinterRunVale(unittest.TestCase):
    """Test the _run_vale subprocess call."""

    @patch("composez_core.vale_linter.subprocess.run")
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_calls_subprocess(self, _, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        linter = ValeLinter(root="/tmp")
        result = linter._run_vale("/tmp/test.txt")
        self.assertEqual(result, "{}")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertEqual(cmd[0], "/path/to/vale_bin")
        self.assertIn("--output=JSON", cmd)

    @patch("composez_core.vale_linter.subprocess.run")
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_uses_config(self, _, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, ".vale.ini")).write_text("test", encoding="utf-8")
            mock_run.return_value = MagicMock(returncode=0, stdout="{}")
            linter = ValeLinter(root=tmpdir)
            linter._run_vale(os.path.join(tmpdir, "test.txt"))
            cmd = mock_run.call_args[0][0]
            config_args = [a for a in cmd if a.startswith("--config=")]
            self.assertEqual(len(config_args), 1)

    @patch("composez_core.vale_linter.subprocess.run")
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_fatal_error_returns_none(self, _, mock_run):
        mock_run.return_value = MagicMock(returncode=2, stdout="fatal error")
        linter = ValeLinter(root="/tmp")
        result = linter._run_vale("/tmp/test.txt")
        self.assertIsNone(result)

    @patch("composez_core.vale_linter.subprocess.run", side_effect=OSError("fail"))
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_oserror_returns_none(self, _, __):
        linter = ValeLinter(root="/tmp")
        result = linter._run_vale("/tmp/test.txt")
        self.assertIsNone(result)

    @patch(
        "composez_core.vale_linter.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="vale", timeout=60),
    )
    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_timeout_returns_none(self, _, __):
        linter = ValeLinter(root="/tmp")
        result = linter._run_vale("/tmp/test.txt")
        self.assertIsNone(result)

    @patch("composez_core.vale_linter._vale_bin", return_value=None)
    def test_no_binary_returns_none(self, _):
        linter = ValeLinter(root="/tmp")
        result = linter._run_vale("/tmp/test.txt")
        self.assertIsNone(result)


class TestValeLinterRel(unittest.TestCase):
    """Test the _rel helper."""

    def test_with_root(self):
        linter = ValeLinter(root="/home/user/project")
        self.assertEqual(linter._rel("/home/user/project/foo.txt"), "foo.txt")

    def test_without_root(self):
        linter = ValeLinter()
        self.assertEqual(linter._rel("/some/path.txt"), "/some/path.txt")


class TestStylesDirHelpers(unittest.TestCase):
    """Test _styles_dir and _sync_timestamp helpers."""

    def test_reads_stylespath_from_ini(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, ".vale.ini")).write_text(
                "StylesPath = .vale-styles\n", encoding="utf-8"
            )
            self.assertEqual(_styles_dir(tmpdir), os.path.join(tmpdir, ".vale-styles"))

    def test_fallback_without_ini(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(_styles_dir(tmpdir), os.path.join(tmpdir, ".vale-styles"))

    def test_sync_timestamp_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, ".vale.ini")).write_text(
                "StylesPath = .vale-styles\n", encoding="utf-8"
            )
            expected = os.path.join(tmpdir, ".vale-styles", "sync-timestamp")
            self.assertEqual(_sync_timestamp(tmpdir), expected)


class TestEnsureSynced(unittest.TestCase):
    """Test the _ensure_synced mtime-based sync logic."""

    @patch("composez_core.vale_linter.vale_sync")
    def test_syncs_when_no_timestamp(self, mock_sync):
        mock_sync.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, ".vale.ini")).write_text(
                DEFAULT_VALE_INI, encoding="utf-8"
            )
            linter = ValeLinter(root=tmpdir)
            linter._ensure_synced()
            mock_sync.assert_called_once_with(tmpdir)

    @patch("composez_core.vale_linter.vale_sync")
    def test_skips_when_timestamp_newer(self, mock_sync):
        """Don't re-sync when timestamp is newer than config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, ".vale.ini")
            Path(config_path).write_text(DEFAULT_VALE_INI, encoding="utf-8")

            # Create the timestamp file *after* the config
            styles_dir = os.path.join(tmpdir, ".vale-styles")
            os.makedirs(styles_dir, exist_ok=True)
            ts_path = os.path.join(styles_dir, "sync-timestamp")
            Path(ts_path).write_text("", encoding="utf-8")
            # Ensure timestamp is newer
            import time

            time.sleep(0.05)
            Path(ts_path).write_text("", encoding="utf-8")

            linter = ValeLinter(root=tmpdir)
            linter._ensure_synced()
            mock_sync.assert_not_called()

    @patch("composez_core.vale_linter.vale_sync")
    def test_resyncs_when_config_newer(self, mock_sync):
        """Re-sync when .vale.ini is newer than the timestamp."""
        mock_sync.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create timestamp first
            styles_dir = os.path.join(tmpdir, ".vale-styles")
            os.makedirs(styles_dir, exist_ok=True)
            ts_path = os.path.join(styles_dir, "sync-timestamp")
            Path(ts_path).write_text("", encoding="utf-8")

            import time

            time.sleep(0.05)

            # Then create config (newer)
            config_path = os.path.join(tmpdir, ".vale.ini")
            Path(config_path).write_text(DEFAULT_VALE_INI, encoding="utf-8")

            linter = ValeLinter(root=tmpdir)
            linter._ensure_synced()
            mock_sync.assert_called_once_with(tmpdir)

    @patch("composez_core.vale_linter.vale_sync")
    def test_only_syncs_once_per_instance(self, mock_sync):
        """After first sync check, _synced flag prevents re-checks."""
        mock_sync.return_value = True
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(os.path.join(tmpdir, ".vale.ini")).write_text(
                DEFAULT_VALE_INI, encoding="utf-8"
            )
            linter = ValeLinter(root=tmpdir)
            linter._ensure_synced()
            linter._ensure_synced()  # second call
            mock_sync.assert_called_once()

    def test_skips_when_no_root(self):
        linter = ValeLinter(root=None)
        linter._ensure_synced()  # Should not raise
        self.assertTrue(linter._synced is False)


class TestNovelCommandsLint(unittest.TestCase):
    """Test the /lint override in NovelCommands."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.repo = None

    def test_lint_registered(self):
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, None, root=self.tmpdir)
        commands = cmds.get_commands()
        self.assertIn("lint", commands)

    def test_lint_no_files(self):
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)
        cmds.cmd_lint("")
        self.io.tool_warning.assert_called()

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_lint_clean_file(self, mock_run, _):
        from composez_core.novel_commands import NovelCommands

        mock_run.return_value = json.dumps({})
        content_path = os.path.join(self.tmpdir, "test.txt")
        Path(content_path).write_text("Clean prose here.", encoding="utf-8")
        self.coder.abs_fnames = {os.path.abspath(content_path)}
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)
        cmds.cmd_lint("")
        # Should report clean
        output_calls = [str(c) for c in self.io.tool_output.call_args_list]
        found_clean = any("no prose issues" in s or "clean" in s.lower() for s in output_calls)
        self.assertTrue(found_clean)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_lint_with_issues(self, mock_run, _):
        from composez_core.novel_commands import NovelCommands

        data = {
            "test.txt": [
                {
                    "Check": "write-good.TooWordy",
                    "Line": 1,
                    "Message": "too wordy",
                    "Severity": "warning",
                    "Match": "utilize",
                }
            ]
        }
        mock_run.return_value = json.dumps(data)
        content_path = os.path.join(self.tmpdir, "test.txt")
        Path(content_path).write_text("We utilize the cat.", encoding="utf-8")
        self.coder.abs_fnames = {os.path.abspath(content_path)}
        self.io.confirm_ask.return_value = False  # Don't fix
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)
        cmds.cmd_lint("")
        # Should output the issue (default level is warning)
        output_calls = " ".join(str(c) for c in self.io.tool_output.call_args_list)
        self.assertIn("Vale lint", output_calls)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_lint_filters_by_level(self, mock_run, _):
        """Suggestions should be hidden at the default 'warning' level."""
        from composez_core.novel_commands import NovelCommands

        data = {
            "test.txt": [
                {
                    "Check": "write-good.Passive",
                    "Line": 1,
                    "Message": "passive voice",
                    "Severity": "suggestion",
                    "Match": "was seen",
                }
            ]
        }
        mock_run.return_value = json.dumps(data)
        content_path = os.path.join(self.tmpdir, "test.txt")
        Path(content_path).write_text("The cat was seen.", encoding="utf-8")
        self.coder.abs_fnames = {os.path.abspath(content_path)}
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)
        cmds.cmd_lint("")
        # Default level is "warning" so suggestion-only file shows as clean
        output_calls = " ".join(str(c) for c in self.io.tool_output.call_args_list)
        self.assertIn("no prose issues", output_calls)

    def test_lint_delegates_non_prose(self):
        """Non-prose files should delegate to parent /lint"""
        from composez_core.novel_commands import NovelCommands

        parent = MagicMock()
        py_path = os.path.join(self.tmpdir, "test.py")
        Path(py_path).write_text("x = 1", encoding="utf-8")
        self.coder.abs_fnames = {os.path.abspath(py_path)}
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir, parent_commands=parent)
        cmds.cmd_lint("")
        parent.cmd_lint.assert_called_once()


class TestFixLintErrorsBatch(unittest.TestCase):
    """Test the _fix_lint_errors_batch method."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.repo = None

    @patch("aider.coders.base_coder.Coder.create")
    def test_batch_fix_creates_single_coder(self, mock_create):
        """_fix_lint_errors_batch should create one coder for all files."""
        from composez_core.novel_commands import NovelCommands

        lint_coder = MagicMock()
        mock_create.return_value = lint_coder

        f1 = os.path.join(self.tmpdir, "a.md")
        f2 = os.path.join(self.tmpdir, "b.md")
        Path(f1).write_text("text a", encoding="utf-8")
        Path(f2).write_text("text b", encoding="utf-8")

        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)
        cmds._fix_lint_errors_batch([f1, f2], "errors here")

        # Should create exactly one coder
        mock_create.assert_called_once()
        # Should run once with all errors
        lint_coder.run.assert_called_once()
        call_arg = lint_coder.run.call_args[0][0]
        self.assertIn("errors here", call_arg)
        # After run, abs_fnames is cleared
        self.assertEqual(lint_coder.abs_fnames, set())

    def test_batch_fix_no_coder_noop(self):
        """_fix_lint_errors_batch should be a no-op when coder is None."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, None, root=self.tmpdir)
        # Should not raise
        cmds._fix_lint_errors_batch(["/tmp/a.md"], "errors")

    @patch("aider.coders.base_coder.Coder.create")
    def test_batch_fix_commits_dirty_before(self, mock_create):
        """Should commit dirty files before fixing."""
        from composez_core.novel_commands import NovelCommands

        lint_coder = MagicMock()
        mock_create.return_value = lint_coder
        parent = MagicMock()
        self.coder.repo = MagicMock()
        self.coder.repo.is_dirty.return_value = True
        self.coder.dirty_commits = True

        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir, parent_commands=parent)
        cmds._fix_lint_errors_batch(["/tmp/a.md"], "errors")

        parent.cmd_commit.assert_called()


class TestLintLevel(unittest.TestCase):
    """Test /lint-level command and severity filtering."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.repo = None

    def _make_cmds(self):
        from composez_core.novel_commands import NovelCommands

        return NovelCommands(self.io, self.coder, root=self.tmpdir)

    def _make_result(self, severities):
        """Build a ValeLintResult with one warning per given severity."""
        warnings = []
        for i, sev in enumerate(severities, 1):
            warnings.append({
                "line": i,
                "check": f"test.Check{i}",
                "message": f"{sev} msg",
                "severity": sev,
                "match": "x",
            })
        text = "## Vale lint: test.md\n"
        return ValeLintResult(text=text, lines=[w["line"] for w in warnings], warnings=warnings)

    # -- /lint-level command -----------------------------------------------

    def test_default_level_is_warning(self):
        cmds = self._make_cmds()
        self.assertEqual(cmds._lint_level, "warning")

    def test_show_current_level(self):
        cmds = self._make_cmds()
        cmds.cmd_lint_level("")
        self.io.tool_output.assert_called_with("Lint level: warning")

    def test_set_error_level(self):
        cmds = self._make_cmds()
        cmds.cmd_lint_level("error")
        self.assertEqual(cmds._lint_level, "error")
        output = self.io.tool_output.call_args[0][0]
        self.assertIn("error", output)

    def test_set_suggestion_level(self):
        cmds = self._make_cmds()
        cmds.cmd_lint_level("suggestion")
        self.assertEqual(cmds._lint_level, "suggestion")
        output = self.io.tool_output.call_args[0][0]
        self.assertIn("suggestion", output)

    def test_set_warning_level(self):
        cmds = self._make_cmds()
        cmds._lint_level = "error"  # change from default
        cmds.cmd_lint_level("warning")
        self.assertEqual(cmds._lint_level, "warning")

    def test_invalid_level_shows_error(self):
        cmds = self._make_cmds()
        cmds.cmd_lint_level("critical")
        self.io.tool_error.assert_called_once()
        self.assertEqual(cmds._lint_level, "warning")  # unchanged

    def test_completions(self):
        cmds = self._make_cmds()
        completions = cmds.completions_lint_level()
        self.assertIn("error", completions)
        self.assertIn("warning", completions)
        self.assertIn("suggestion", completions)

    # -- _lint_severities --------------------------------------------------

    def test_severities_error(self):
        cmds = self._make_cmds()
        cmds._lint_level = "error"
        self.assertEqual(cmds._lint_severities(), {"error"})

    def test_severities_warning(self):
        cmds = self._make_cmds()
        cmds._lint_level = "warning"
        self.assertEqual(cmds._lint_severities(), {"error", "warning"})

    def test_severities_suggestion(self):
        cmds = self._make_cmds()
        cmds._lint_level = "suggestion"
        self.assertEqual(cmds._lint_severities(), {"error", "warning", "suggestion"})

    # -- _filter_warnings --------------------------------------------------

    def test_filter_errors_only(self):
        cmds = self._make_cmds()
        result = self._make_result(["error", "warning", "suggestion"])
        filtered = cmds._filter_warnings(result, {"error"})
        self.assertIn("error msg", filtered)
        self.assertNotIn("warning msg", filtered)
        self.assertNotIn("suggestion msg", filtered)

    def test_filter_warnings_and_errors(self):
        cmds = self._make_cmds()
        result = self._make_result(["error", "warning", "suggestion"])
        filtered = cmds._filter_warnings(result, {"error", "warning"})
        self.assertIn("error msg", filtered)
        self.assertIn("warning msg", filtered)
        self.assertNotIn("suggestion msg", filtered)

    def test_filter_all(self):
        cmds = self._make_cmds()
        result = self._make_result(["error", "warning", "suggestion"])
        filtered = cmds._filter_warnings(result, {"error", "warning", "suggestion"})
        self.assertIn("error msg", filtered)
        self.assertIn("warning msg", filtered)
        self.assertIn("suggestion msg", filtered)

    def test_filter_empty_when_no_match(self):
        cmds = self._make_cmds()
        result = self._make_result(["suggestion"])
        filtered = cmds._filter_warnings(result, {"error"})
        self.assertEqual(filtered, "")

    # -- coder sync --------------------------------------------------------

    def test_cmd_lint_level_syncs_to_coder(self):
        cmds = self._make_cmds()
        self.coder._novel_lint_level = "warning"
        cmds.cmd_lint_level("error")
        self.assertEqual(self.coder._novel_lint_level, "error")


class TestMakeMarkdownLinter(unittest.TestCase):
    """Test the make_markdown_linter factory for auto-lint integration."""

    def _make_vale_data(self, severities):
        """Build Vale JSON output with one issue per severity."""
        issues = []
        for i, sev in enumerate(severities, 1):
            issues.append({
                "Check": f"test.Check{i}",
                "Line": i * 10,
                "Message": f"{sev} msg",
                "Severity": sev,
                "Match": "x",
            })
        return json.dumps({"test.md": issues})

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    def test_returns_callable(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            fn = make_markdown_linter(tmpdir)
            self.assertTrue(callable(fn))

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_returns_none_for_clean(self, mock_run, _):
        mock_run.return_value = json.dumps({})
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            fn = make_markdown_linter(tmpdir)
            result = fn(fname, "novel/test.md", "Clean prose.")
            self.assertIsNone(result)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_returns_lint_result_with_issues(self, mock_run, _):
        from aider.linter import LintResult

        mock_run.return_value = self._make_vale_data(["warning"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            fn = make_markdown_linter(tmpdir)
            result = fn(fname, "novel/test.md", "Bad prose.")
            self.assertIsNotNone(result)
            self.assertIsInstance(result, LintResult)
            self.assertIn("warning msg", result.text)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_lines_are_zero_indexed(self, mock_run, _):
        mock_run.return_value = self._make_vale_data(["error"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            fn = make_markdown_linter(tmpdir)
            result = fn(fname, "novel/test.md", "Bad prose.")
            # Vale line 10 → 0-indexed 9
            self.assertIn(9, result.lines)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_filters_by_coder_lint_level(self, mock_run, _):
        mock_run.return_value = self._make_vale_data(["error", "warning", "suggestion"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            coder = MagicMock()
            coder._novel_lint_level = "error"
            fn = make_markdown_linter(tmpdir, coder=coder)
            result = fn(fname, "novel/test.md", "Bad prose.")
            self.assertIn("error msg", result.text)
            self.assertNotIn("warning msg", result.text)
            self.assertNotIn("suggestion msg", result.text)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_works_without_coder(self, mock_run, _):
        """Without a coder, defaults to 'warning' level."""
        mock_run.return_value = self._make_vale_data(["error", "warning", "suggestion"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            fn = make_markdown_linter(tmpdir, coder=None)
            result = fn(fname, "novel/test.md", "Bad prose.")
            self.assertIn("error msg", result.text)
            self.assertIn("warning msg", result.text)
            self.assertNotIn("suggestion msg", result.text)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_returns_none_when_all_filtered(self, mock_run, _):
        """If all issues are below the lint level, returns None."""
        mock_run.return_value = self._make_vale_data(["suggestion"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "novel", "test.md")
            coder = MagicMock()
            coder._novel_lint_level = "error"
            fn = make_markdown_linter(tmpdir, coder=coder)
            result = fn(fname, "novel/test.md", "Some prose.")
            self.assertIsNone(result)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_skips_files_outside_novel_dir(self, mock_run, _):
        """Files outside novel/ (e.g. db/core/style.md) should not be linted."""
        mock_run.return_value = self._make_vale_data(["error"])
        with tempfile.TemporaryDirectory() as tmpdir:
            db_fname = os.path.join(tmpdir, "db", "core", "style.md")
            fn = make_markdown_linter(tmpdir)
            result = fn(db_fname, "db/core/style.md", "Bad prose.")
            self.assertIsNone(result)
            # Vale should never even be called for non-novel files
            mock_run.assert_not_called()

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_skips_root_level_files(self, mock_run, _):
        """Files at the repo root should not be linted."""
        mock_run.return_value = self._make_vale_data(["error"])
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, "README.md")
            fn = make_markdown_linter(tmpdir)
            result = fn(fname, "README.md", "Some text.")
            self.assertIsNone(result)
            mock_run.assert_not_called()


class TestLintFiles(unittest.TestCase):
    """Test ValeLinter.lint_files batch invocation."""

    def _vale_json(self, file_results):
        """Build Vale JSON output: {fname: [{Check, Line, ...}, ...]}."""
        data = {}
        for fname, issues in file_results.items():
            data[fname] = [
                {
                    "Check": w.get("check", "test.Check"),
                    "Line": w.get("line", 1),
                    "Message": w.get("message", "msg"),
                    "Severity": w.get("severity", "warning"),
                    "Match": w.get("match", "x"),
                }
                for w in issues
            ]
        return json.dumps(data)

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_batch_returns_results_per_file(self, mock_run, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "a.md")
            f2 = os.path.join(tmpdir, "b.md")
            mock_run.return_value = self._vale_json({
                f1: [{"line": 1, "severity": "warning", "message": "w1"}],
                f2: [{"line": 5, "severity": "error", "message": "e1"}],
            })
            linter = ValeLinter(root=tmpdir)
            results = linter.lint_files([f1, f2])
            self.assertIn(os.path.normpath(f1), results)
            self.assertIn(os.path.normpath(f2), results)
            self.assertEqual(results[os.path.normpath(f1)].warnings[0]["message"], "w1")
            self.assertEqual(results[os.path.normpath(f2)].warnings[0]["message"], "e1")

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_batch_empty_when_clean(self, mock_run, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value = json.dumps({})
            linter = ValeLinter(root=tmpdir)
            results = linter.lint_files([os.path.join(tmpdir, "a.md")])
            self.assertEqual(results, {})

    @patch("composez_core.vale_linter._vale_bin", return_value="/path/to/vale_bin")
    @patch.object(ValeLinter, "_run_vale")
    def test_batch_normalises_relative_keys(self, mock_run, _):
        """Keys in Vale output that are relative should be resolved to absolute."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value = self._vale_json({
                "novel/scene.md": [{"line": 3, "severity": "warning"}],
            })
            linter = ValeLinter(root=tmpdir)
            results = linter.lint_files(
                [os.path.join(tmpdir, "novel", "scene.md")]
            )
            expected_key = os.path.normpath(
                os.path.join(tmpdir, "novel", "scene.md")
            )
            self.assertIn(expected_key, results)

    def test_batch_empty_fnames(self):
        linter = ValeLinter(root="/tmp")
        self.assertEqual(linter.lint_files([]), {})

    @patch("composez_core.vale_linter._vale_bin", return_value=None)
    def test_batch_no_vale(self, _):
        linter = ValeLinter(root="/tmp")
        self.assertEqual(linter.lint_files(["a.md"]), {})


class TestGetChangedLines(unittest.TestCase):
    """Test _get_changed_lines diff parser."""

    def test_parses_added_lines(self):
        from composez_core.novel_coder import _get_changed_lines

        repo = MagicMock()
        repo.repo.git.diff.return_value = (
            "diff --git a/novel/scene.md b/novel/scene.md\n"
            "--- a/novel/scene.md\n"
            "+++ b/novel/scene.md\n"
            "@@ -10,0 +11,3 @@\n"
            "+new line 1\n"
            "+new line 2\n"
            "+new line 3\n"
        ).encode("utf-8")
        result = _get_changed_lines(repo, ["novel/scene.md"])
        self.assertIn("novel/scene.md", result)
        self.assertEqual(result["novel/scene.md"], {11, 12, 13})

    def test_parses_modified_lines(self):
        from composez_core.novel_coder import _get_changed_lines

        repo = MagicMock()
        repo.repo.git.diff.return_value = (
            "diff --git a/novel/ch1.md b/novel/ch1.md\n"
            "--- a/novel/ch1.md\n"
            "+++ b/novel/ch1.md\n"
            "@@ -5,2 +5,2 @@\n"
            "-old line\n"
            "-old line 2\n"
            "+new line\n"
            "+new line 2\n"
        ).encode("utf-8")
        result = _get_changed_lines(repo, ["novel/ch1.md"])
        self.assertIn("novel/ch1.md", result)
        self.assertEqual(result["novel/ch1.md"], {5, 6})

    def test_single_line_hunk(self):
        from composez_core.novel_coder import _get_changed_lines

        repo = MagicMock()
        repo.repo.git.diff.return_value = (
            "diff --git a/f.md b/f.md\n"
            "--- a/f.md\n"
            "+++ b/f.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ).encode("utf-8")
        result = _get_changed_lines(repo, ["f.md"])
        self.assertEqual(result["f.md"], {1})

    def test_multiple_files(self):
        from composez_core.novel_coder import _get_changed_lines

        repo = MagicMock()
        repo.repo.git.diff.return_value = (
            "diff --git a/a.md b/a.md\n"
            "+++ b/a.md\n"
            "@@ -1 +1 @@\n"
            "+x\n"
            "diff --git a/b.md b/b.md\n"
            "+++ b/b.md\n"
            "@@ -5,2 +5,3 @@\n"
            "+y\n"
        ).encode("utf-8")
        result = _get_changed_lines(repo, ["a.md", "b.md"])
        self.assertEqual(result["a.md"], {1})
        self.assertEqual(result["b.md"], {5, 6, 7})

    def test_no_repo(self):
        from composez_core.novel_coder import _get_changed_lines

        self.assertEqual(_get_changed_lines(None, ["a.md"]), {})

    def test_git_error(self):
        from composez_core.novel_coder import _get_changed_lines

        repo = MagicMock()
        repo.repo.git.diff.side_effect = Exception("git error")
        self.assertEqual(_get_changed_lines(repo, ["a.md"]), {})


class TestNovelAutoLint(unittest.TestCase):
    """Test _install_novel_auto_lint wrapper on auto_commit."""

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.io = MagicMock()
        coder.repo = MagicMock()
        coder.root = tempfile.mkdtemp()
        coder.abs_root_path_cache = {}
        coder.auto_lint = True
        coder.auto_commits = True
        coder.dry_run = False
        coder._novel_auto_lint = True
        coder._novel_lint_level = "warning"
        coder.edit_format = "whole"
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.files_content_gpt_edits_no_repo = "no edits"
        coder.gpt_prompts.files_content_gpt_edits = MagicMock()
        coder.show_diffs = False
        coder.aider_commit_hashes = set()
        coder.last_aider_commit_hash = None
        coder.last_aider_commit_message = None
        coder.reflected_message = None
        coder.lint_outcome = None
        coder.cur_messages = []
        coder.done_messages = []
        coder.partial_response_content = ""
        return coder

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_lint_clean_commits_normally(self, MockValeLinter, _):
        """When Vale finds no issues, auto_commit proceeds normally."""
        from composez_core.novel_coder import _install_novel_auto_lint

        mock_vale_instance = MockValeLinter.return_value
        mock_vale_instance.lint_files.return_value = {}

        coder = self._make_coder()
        coder.repo.commit.return_value = ("abc123", "test commit")
        original_auto_commit = type(coder).auto_commit

        _install_novel_auto_lint(coder)

        result = coder.auto_commit({"novel/scene.md"})
        # Should have called lint_files
        mock_vale_instance.lint_files.assert_called_once()
        # Should have committed
        coder.repo.commit.assert_called_once()
        # No reflected_message
        self.assertIsNone(coder.reflected_message)

    @patch("composez_core.novel_coder._get_changed_lines")
    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_lint_issues_on_changed_lines_reflects(
        self, MockValeLinter, _, mock_changed
    ):
        """Lint issues on changed lines should set reflected_message and skip commit."""
        from composez_core.novel_coder import _install_novel_auto_lint

        mock_changed.return_value = {"novel/scene.md": {5, 6, 7}}

        coder = self._make_coder()
        abs_path = os.path.normpath(
            os.path.join(coder.root, "novel", "scene.md")
        )
        mock_vale_instance = MockValeLinter.return_value
        mock_vale_instance.lint_files.return_value = {
            abs_path: ValeLintResult(
                text="## Vale lint: novel/scene.md\nwarning",
                lines=[5],
                warnings=[{
                    "line": 5,
                    "check": "test.Check",
                    "message": "bad prose",
                    "severity": "warning",
                    "match": "x",
                }],
            ),
        }
        mock_vale_instance._rel.return_value = "novel/scene.md"
        mock_vale_instance._format_warnings.return_value = "## Vale lint: novel/scene.md\nwarning"

        _install_novel_auto_lint(coder)

        result = coder.auto_commit({"novel/scene.md"})
        # Should NOT have committed
        coder.repo.commit.assert_not_called()
        # Should have set reflected_message for auto-fix
        self.assertIsNotNone(coder.reflected_message)
        self.assertIn("Fix any errors", coder.reflected_message)

    @patch("composez_core.novel_coder._get_changed_lines")
    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_lint_issues_on_unchanged_lines_ignored(
        self, MockValeLinter, _, mock_changed
    ):
        """Lint issues on lines NOT changed should be ignored."""
        from composez_core.novel_coder import _install_novel_auto_lint

        mock_changed.return_value = {"novel/scene.md": {1, 2}}

        coder = self._make_coder()
        abs_path = os.path.normpath(
            os.path.join(coder.root, "novel", "scene.md")
        )
        mock_vale_instance = MockValeLinter.return_value
        mock_vale_instance.lint_files.return_value = {
            abs_path: ValeLintResult(
                text="## Vale lint\n",
                lines=[50],
                warnings=[{
                    "line": 50,
                    "check": "test.Check",
                    "message": "old issue",
                    "severity": "warning",
                    "match": "x",
                }],
            ),
        }

        coder.repo.commit.return_value = ("abc123", "test commit")
        _install_novel_auto_lint(coder)

        result = coder.auto_commit({"novel/scene.md"})
        # Should have committed (issue was on unchanged line 50)
        coder.repo.commit.assert_called_once()
        self.assertIsNone(coder.reflected_message)

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_context_commit_passes_through(self, MockValeLinter, _):
        """Commits with context (e.g. 'Ran the linter') bypass novel lint."""
        from composez_core.novel_coder import _install_novel_auto_lint

        coder = self._make_coder()
        coder.repo.commit.return_value = ("abc123", "lint fix")
        _install_novel_auto_lint(coder)

        coder.auto_commit({"novel/scene.md"}, context="Ran the linter")
        # Should have committed without running lint
        MockValeLinter.return_value.lint_files.assert_not_called()
        coder.repo.commit.assert_called_once()

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_disabled_passes_through(self, MockValeLinter, _):
        """When _novel_auto_lint is False, skip novel lint entirely."""
        from composez_core.novel_coder import _install_novel_auto_lint

        coder = self._make_coder()
        coder.repo.commit.return_value = ("abc123", "edit")
        _install_novel_auto_lint(coder)
        # Disable AFTER install (install sets it to True)
        coder._novel_auto_lint = False

        coder.auto_commit({"novel/scene.md"})
        MockValeLinter.return_value.lint_files.assert_not_called()
        coder.repo.commit.assert_called_once()

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_query_mode_passes_through(self, MockValeLinter, _):
        """Query mode should not run lint."""
        from composez_core.novel_coder import _install_novel_auto_lint

        coder = self._make_coder()
        coder.edit_format = "query"
        coder.repo.commit.return_value = ("abc123", "edit")
        _install_novel_auto_lint(coder)

        coder.auto_commit({"novel/scene.md"})
        MockValeLinter.return_value.lint_files.assert_not_called()

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_non_prose_files_pass_through(self, MockValeLinter, _):
        """Non-.md files should not trigger novel lint."""
        from composez_core.novel_coder import _install_novel_auto_lint

        coder = self._make_coder()
        coder.repo.commit.return_value = ("abc123", "edit")
        _install_novel_auto_lint(coder)

        coder.auto_commit({"src/main.py"})
        MockValeLinter.return_value.lint_files.assert_not_called()
        coder.repo.commit.assert_called_once()

    @patch("composez_core.vale_linter.vale_available", return_value=True)
    @patch("composez_core.vale_linter.ValeLinter")
    def test_sets_auto_lint_false(self, MockValeLinter, _):
        """Installing novel auto-lint should disable base auto_lint."""
        from composez_core.novel_coder import _install_novel_auto_lint

        coder = self._make_coder()
        coder.auto_lint = True
        _install_novel_auto_lint(coder)
        self.assertFalse(coder.auto_lint)
        self.assertTrue(coder._novel_auto_lint)


if __name__ == "__main__":
    unittest.main()
