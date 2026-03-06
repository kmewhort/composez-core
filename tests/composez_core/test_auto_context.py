"""Tests for the auto-context feature (novel_coder.py + novel_context_prompts.py)."""

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from composez_core.config import load_config, save_config
from composez_core.novel_context_prompts import NovelContextPrompts
from composez_core.novel_coder import (
    build_db_listing,
    _extract_backtick_paths,
    _install_auto_context,
    _maybe_run_auto_context,
    _reply_completed_no_reflect,
    _space_aware_get_file_mentions,
    run_auto_context,
)


class TestNovelContextPrompts(unittest.TestCase):
    """Test that NovelContextPrompts builds correctly."""

    def test_default_db_listing(self):
        prompts = NovelContextPrompts()
        self.assertIn("no db entries found", prompts.main_system)

    def test_custom_db_listing_baked_in(self):
        listing = "- ``db/characters/sarah.md`` — Sarah is a detective"
        prompts = NovelContextPrompts(db_listing=listing)
        self.assertIn("sarah.md", prompts.main_system)
        self.assertIn("Sarah is a detective", prompts.main_system)

    def test_language_placeholder_preserved(self):
        """The {language} placeholder should survive for fmt_system_prompt."""
        prompts = NovelContextPrompts()
        self.assertIn("{language}", prompts.main_system)

    def test_system_reminder_forbids_code(self):
        prompts = NovelContextPrompts()
        self.assertIn("NEVER RETURN CODE", prompts.system_reminder)

    def test_mentions_narrative_structure(self):
        prompts = NovelContextPrompts()
        self.assertIn("novel/", prompts.main_system)
        self.assertIn("SUMMARY.md", prompts.main_system)
        self.assertIn("PROSE.md", prompts.main_system)

    def test_mentions_db_structure(self):
        prompts = NovelContextPrompts()
        self.assertIn("db/", prompts.main_system)

    def test_query_mode_uses_examine_language(self):
        """In query mode the prompt should ask for files to examine, not modify."""
        prompts = NovelContextPrompts(query_mode=True)
        self.assertIn("files that are relevant to answering", prompts.main_system)
        self.assertIn("Files to examine", prompts.main_system)
        # Must NOT contain "modify" language
        self.assertNotIn("files which will need to be modified", prompts.main_system)
        self.assertNotIn("Files to modify", prompts.main_system)

    def test_query_mode_db_listing_baked_in(self):
        listing = "- ``db/characters/teo.md`` — Teomitl is a warrior"
        prompts = NovelContextPrompts(db_listing=listing, query_mode=True)
        self.assertIn("teo.md", prompts.main_system)
        self.assertIn("Teomitl is a warrior", prompts.main_system)

    def test_query_mode_language_placeholder_preserved(self):
        prompts = NovelContextPrompts(query_mode=True)
        self.assertIn("{language}", prompts.main_system)

    def test_default_mode_uses_modify_language(self):
        """Default (non-query) mode should still use 'modify' language."""
        prompts = NovelContextPrompts()
        self.assertIn("files which will need to be modified", prompts.main_system)
        self.assertIn("Files to modify", prompts.main_system)
        self.assertNotIn("Files to examine", prompts.main_system)


class TestBuildDbListing(unittest.TestCase):
    """Test the db listing builder."""

    def test_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = build_db_listing(tmp)
            self.assertEqual(result, "(no db entries)")

    def test_single_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a db entry
            char_dir = os.path.join(tmp, "db", "characters")
            os.makedirs(char_dir)
            Path(os.path.join(char_dir, "sarah.md")).write_text(
                "Sarah is a detective who loves puzzles."
            )
            result = build_db_listing(tmp)
            self.assertIn("sarah.md", result)
            self.assertIn("detective", result)

    def test_preview_truncated_at_50_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            char_dir = os.path.join(tmp, "db", "characters")
            os.makedirs(char_dir)
            # Create content with more than 50 words
            long_text = " ".join(f"word{i}" for i in range(80))
            Path(os.path.join(char_dir, "verbose.md")).write_text(long_text)
            result = build_db_listing(tmp)
            self.assertIn("…", result)
            # Should contain the first 50 words
            self.assertIn("word0", result)
            self.assertIn("word49", result)
            # Should NOT contain word 50+
            self.assertNotIn("word50", result)

    def test_multiple_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cat in ("characters", "locations"):
                d = os.path.join(tmp, "db", cat)
                os.makedirs(d)
                Path(os.path.join(d, "test.md")).write_text(f"A {cat} entry.")
            result = build_db_listing(tmp)
            self.assertIn("characters", result)
            self.assertIn("locations", result)


    def test_non_utf8_file_does_not_crash(self):
        """A binary file in db/ should not break build_db_listing."""
        with tempfile.TemporaryDirectory() as tmp:
            char_dir = os.path.join(tmp, "db", "characters")
            os.makedirs(char_dir)
            Path(os.path.join(char_dir, "sarah.md")).write_text(
                "Sarah is a detective."
            )
            # Write a binary file that can't be decoded as UTF-8
            Path(os.path.join(char_dir, "portrait.png")).write_bytes(
                b"\xff\xd8\xff\xe0\x00\x10JFIF"
            )
            result = build_db_listing(tmp)
            # Should succeed and include the valid entry
            self.assertIn("sarah.md", result)
            # The binary file entry should be present but with empty preview
            self.assertIn("portrait.png", result)


class TestExtractBacktickPaths(unittest.TestCase):
    """Test backtick-delimited path extraction for paths with spaces."""

    def test_simple_path_no_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "db", "characters", "teo.md")
            os.makedirs(os.path.dirname(f))
            Path(f).write_text("Teomitl")
            content = "- `db/characters/teo.md` — character profile"
            found = _extract_backtick_paths(content, tmp)
            self.assertEqual(found, {f})

    def test_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene_dir = os.path.join(
                tmp, "novel", "Act 1 - Acolytes",
                "Chapter 1 - Cold Water", "Scene 1 - Title"
            )
            os.makedirs(scene_dir)
            prose = os.path.join(scene_dir, "PROSE.md")
            Path(prose).write_text("Some prose")
            content = (
                "- `novel/Act 1 - Acolytes/Chapter 1 - Cold Water"
                "/Scene 1 - Title/PROSE.md` — scene prose"
            )
            found = _extract_backtick_paths(content, tmp)
            self.assertEqual(found, {prose})

    def test_multiple_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            f1 = os.path.join(tmp, "db", "characters", "teo.md")
            os.makedirs(os.path.dirname(f1))
            Path(f1).write_text("Teo")
            scene_dir = os.path.join(
                tmp, "novel", "Act 1 - Title", "Chapter 1 - Title",
                "Scene 1 - Title"
            )
            os.makedirs(scene_dir)
            f2 = os.path.join(scene_dir, "SUMMARY.md")
            Path(f2).write_text("Summary")
            content = (
                "- `db/characters/teo.md` — character\n"
                "- `novel/Act 1 - Title/Chapter 1 - Title"
                "/Scene 1 - Title/SUMMARY.md` — summary"
            )
            found = _extract_backtick_paths(content, tmp)
            self.assertEqual(found, {f1, f2})

    def test_nonexistent_path_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "- `db/characters/nonexistent.md` — ghost file"
            found = _extract_backtick_paths(content, tmp)
            self.assertEqual(found, set())

    def test_empty_backticks_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "some text `` more text"
            found = _extract_backtick_paths(content, tmp)
            self.assertEqual(found, set())


class TestSpaceAwareGetFileMentions(unittest.TestCase):
    """Test _space_aware_get_file_mentions finds paths with spaces."""

    def _make_coder_with_files(self, tmp, rel_paths):
        """Create a stub coder with known files for get_file_mentions."""
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = str(tmp)
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.io = MagicMock()

        # Create the actual files on disk and track them
        for rp in rel_paths:
            abs_p = os.path.join(str(tmp), rp)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("content")

        # Stub out the file-listing methods to return our paths
        coder.get_all_relative_files = MagicMock(return_value=rel_paths)
        coder.get_addable_relative_files = MagicMock(return_value=rel_paths)
        coder.get_inchat_relative_files = MagicMock(return_value=[])

        return coder

    def test_finds_path_with_spaces(self):
        """Paths with spaces should be found via substring matching."""
        rel = "novel/Act 1 - Acolytes/Chapter 1 - Cold Water/Scene 1 - Title/PROSE.md"
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder_with_files(tmp, [rel])
            content = (
                "## Files to examine:\n"
                f"- {rel}\n"
                "  - Contains dialogue to analyse\n"
            )
            result = _space_aware_get_file_mentions(coder, content, ignore_current=True)
            self.assertIn(rel, result)

    def test_simple_path_still_works(self):
        """Paths without spaces should still be found by the original method."""
        rel = "db/characters/Teomitl.md"
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder_with_files(tmp, [rel])
            content = f"- {rel} — character profile"
            result = _space_aware_get_file_mentions(coder, content, ignore_current=True)
            self.assertIn(rel, result)

    def test_mixed_paths(self):
        """Both simple paths and paths with spaces should be found together."""
        simple = "db/characters/Teomitl.md"
        spaced = "novel/Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md"
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder_with_files(tmp, [simple, spaced])
            content = (
                f"- {simple} — character\n"
                f"- {spaced} — scene prose\n"
            )
            result = _space_aware_get_file_mentions(coder, content, ignore_current=True)
            self.assertIn(simple, result)
            self.assertIn(spaced, result)

    def test_nonexistent_path_not_matched(self):
        """Paths not in the repo file list should not be returned."""
        rel = "novel/Act 1 - Acolytes/Chapter 1 - Cold Water/Scene 1 - Title/PROSE.md"
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder_with_files(tmp, [rel])
            content = "- novel/Act 99 - Fake/Chapter 1 - Fake/Scene 1 - Fake/PROSE.md"
            result = _space_aware_get_file_mentions(coder, content, ignore_current=True)
            self.assertEqual(result, set())


class TestAutoContextConfig(unittest.TestCase):
    """Test the .composez auto_context setting."""

    def test_default_is_true(self):
        from composez_core.config import get_auto_context
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(get_auto_context(tmp))

    def test_explicit_false(self):
        from composez_core.config import get_auto_context
        with tempfile.TemporaryDirectory() as tmp:
            save_config(tmp, {"levels": ["Act", "Chapter", "Scene"], "auto_context": False})
            self.assertFalse(get_auto_context(tmp))

    def test_explicit_true(self):
        from composez_core.config import get_auto_context
        with tempfile.TemporaryDirectory() as tmp:
            save_config(tmp, {"levels": ["Act", "Chapter", "Scene"], "auto_context": True})
            self.assertTrue(get_auto_context(tmp))


class TestInstallAutoContext(unittest.TestCase):
    """Test the auto-context monkey-patching of run_one."""

    def _make_coder(self, tmp_path, auto_context=True):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = str(tmp_path)
        coder.io = MagicMock()
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.main_model = MagicMock()
        coder.main_model.weak_model = MagicMock()
        coder.commands = MagicMock()
        coder.commands.is_command = MagicMock(return_value=False)
        coder.edit_format = "diff"

        # Write .composez with auto_context setting
        save_config(str(tmp_path), {
            "levels": ["Act", "Chapter", "Scene"],
            "auto_context": auto_context,
        })

        return coder

    def test_installs_run_one_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            _install_auto_context(coder)
            # The run_one method should be a bound method on the instance
            self.assertTrue(hasattr(coder, "run_one"))
            self.assertTrue(hasattr(coder, "_auto_context_enabled"))
            self.assertTrue(coder._auto_context_enabled)

    def test_disabled_auto_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp), auto_context=False)
            _install_auto_context(coder)
            self.assertFalse(coder._auto_context_enabled)

    def test_skips_commands(self):
        """Auto-context should not run for slash commands."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            coder.commands.is_command = MagicMock(return_value=True)

            # We need to capture the original before _install wraps it
            from aider.coders.base_coder import Coder
            original_run_one = Coder.run_one

            with patch.object(Coder, "run_one") as mock_original:
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context") as mock_auto:
                    coder.run_one("/help", True)
                    mock_auto.assert_not_called()
                    mock_original.assert_called_once()

    def test_temporary_files_removed_after_run(self):
        """Files added by auto-context should be removed after run_one completes."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))

            fake_files = {os.path.join(tmp, "db", "characters", "alice.md")}

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_one"):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context", return_value=fake_files):
                    coder.run_one("add alice's backstory", True)

            # After run_one completes, the temporarily added files should be gone
            # (edit mode adds to abs_fnames, query mode to abs_read_only_fnames)
            self.assertEqual(len(coder.abs_fnames & fake_files), 0)
            self.assertEqual(len(coder.abs_read_only_fnames & fake_files), 0)

    def test_query_mode_passes_query_mode_to_prompts(self):
        """When coder.edit_format is 'query', run_auto_context should use query-mode prompts."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            coder.edit_format = "query"

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create") as mock_create:
                mock_ctx = MagicMock()
                mock_ctx.abs_fnames = set()
                mock_ctx.abs_read_only_fnames = set()
                mock_ctx.partial_response_content = ""
                mock_create.return_value = mock_ctx

                run_auto_context(coder, "Analyze Teo's voice")

                # Check that NovelContextPrompts was assigned with ask-mode content
                assigned_prompts = mock_ctx.gpt_prompts
                self.assertIn("files that are relevant to answering", assigned_prompts.main_system)

    def test_edit_mode_passes_modify_prompts(self):
        """When coder.edit_format is 'diff', run_auto_context should use modify prompts."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            coder.edit_format = "diff"

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create") as mock_create:
                mock_ctx = MagicMock()
                mock_ctx.abs_fnames = set()
                mock_ctx.abs_read_only_fnames = set()
                mock_ctx.partial_response_content = ""
                mock_create.return_value = mock_ctx

                run_auto_context(coder, "Update Teo's dialogue")

                assigned_prompts = mock_ctx.gpt_prompts
                self.assertIn("files which will need to be modified", assigned_prompts.main_system)

    def test_temporary_files_removed_on_exception(self):
        """Files should be removed even if run_one raises."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))

            fake_files = {os.path.join(tmp, "db", "characters", "alice.md")}

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_one", side_effect=RuntimeError("boom")):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context", return_value=fake_files):
                    with self.assertRaises(RuntimeError):
                        coder.run_one("test", True)

            self.assertEqual(len(coder.abs_fnames & fake_files), 0)
            self.assertEqual(len(coder.abs_read_only_fnames & fake_files), 0)


class TestInstallAutoContextRunStream(unittest.TestCase):
    """Test the auto-context monkey-patching of run_stream (web UI path)."""

    def _make_coder(self, tmp_path, auto_context=True):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = str(tmp_path)
        coder.io = MagicMock()
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.main_model = MagicMock()
        coder.main_model.weak_model = MagicMock()
        coder.commands = MagicMock()
        coder.commands.is_command = MagicMock(return_value=False)
        coder.edit_format = "diff"

        save_config(str(tmp_path), {
            "levels": ["Act", "Chapter", "Scene"],
            "auto_context": auto_context,
        })

        return coder

    def test_installs_run_stream_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            _install_auto_context(coder)
            # run_stream should be a bound method on the instance
            self.assertTrue(hasattr(coder, "run_stream"))
            # It should be an instance-bound method, not the class method
            from aider.coders.base_coder import Coder
            self.assertIsNot(coder.run_stream, Coder.run_stream)

    def test_run_stream_calls_auto_context(self):
        """Auto-context should run when run_stream is called (web UI path)."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", return_value=iter(["chunk"])):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context") as mock_auto:
                    mock_auto.return_value = set()
                    # Consume the generator
                    list(coder.run_stream("Analyze the scene"))
                    mock_auto.assert_called_once()

    def test_run_stream_skips_commands(self):
        """Auto-context should not run for slash commands via run_stream."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            coder.commands.is_command = MagicMock(return_value=True)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", return_value=iter([])):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context") as mock_auto:
                    list(coder.run_stream("/help"))
                    mock_auto.assert_not_called()

    def test_run_stream_temporary_files_removed(self):
        """Files added by auto-context should be removed after run_stream completes."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            fake_files = {os.path.join(tmp, "db", "characters", "alice.md")}

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", return_value=iter(["chunk"])):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context", return_value=fake_files):
                    list(coder.run_stream("add alice's backstory"))

            self.assertEqual(len(coder.abs_fnames & fake_files), 0)
            self.assertEqual(len(coder.abs_read_only_fnames & fake_files), 0)

    def test_run_stream_temporary_files_removed_on_exception(self):
        """Files should be removed even if run_stream raises."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            fake_files = {os.path.join(tmp, "db", "characters", "alice.md")}

            def bad_stream(self, msg):
                raise RuntimeError("boom")
                yield  # noqa: unreachable — makes it a generator

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", bad_stream):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context", return_value=fake_files):
                    with self.assertRaises(RuntimeError):
                        list(coder.run_stream("test"))

            self.assertEqual(len(coder.abs_fnames & fake_files), 0)
            self.assertEqual(len(coder.abs_read_only_fnames & fake_files), 0)

    def test_run_stream_disabled_auto_context(self):
        """Auto-context should not run when disabled, even via run_stream."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp), auto_context=False)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", return_value=iter([])):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context") as mock_auto:
                    list(coder.run_stream("test"))
                    mock_auto.assert_not_called()

    def test_run_stream_yields_chunks(self):
        """The wrapper should pass through all chunks from the original run_stream."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            expected_chunks = ["Hello ", "world", "!"]

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "run_stream", return_value=iter(expected_chunks)):
                _install_auto_context(coder)
                with patch("composez_core.novel_coder.run_auto_context", return_value=set()):
                    result = list(coder.run_stream("test"))

            self.assertEqual(result, expected_chunks)


class TestAutoContextToggleCommand(unittest.TestCase):
    """Test the /auto-context slash command."""

    def _make_commands(self, tmp_path, auto_context=True):
        from composez_core.novel_commands import NovelCommands

        coder = MagicMock()
        coder.root = str(tmp_path)
        coder._auto_context_enabled = auto_context
        io = MagicMock()

        save_config(str(tmp_path), {
            "levels": ["Act", "Chapter", "Scene"],
            "auto_context": auto_context,
        })

        return NovelCommands(io=io, coder=coder, root=str(tmp_path))

    def test_toggle_on_to_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = self._make_commands(Path(tmp), auto_context=True)
            cmds.cmd_auto_context("")
            self.assertFalse(cmds.coder._auto_context_enabled)
            # Should be persisted
            config = load_config(str(tmp))
            self.assertFalse(config["auto_context"])

    def test_toggle_off_to_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = self._make_commands(Path(tmp), auto_context=False)
            cmds.cmd_auto_context("")
            self.assertTrue(cmds.coder._auto_context_enabled)
            config = load_config(str(tmp))
            self.assertTrue(config["auto_context"])

    def test_explicit_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = self._make_commands(Path(tmp), auto_context=False)
            cmds.cmd_auto_context("on")
            self.assertTrue(cmds.coder._auto_context_enabled)

    def test_explicit_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = self._make_commands(Path(tmp), auto_context=True)
            cmds.cmd_auto_context("off")
            self.assertFalse(cmds.coder._auto_context_enabled)

    def test_output_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmds = self._make_commands(Path(tmp), auto_context=True)
            cmds.cmd_auto_context("off")
            cmds.io.tool_output.assert_called_with("Auto-context disabled.")


class TestReplyCompletedBug(unittest.TestCase):
    """Demonstrate and fix the max_reflections=1 bug in ContextCoder.reply_completed.

    ContextCoder.reply_completed() ties file addition to the reflection
    mechanism.  The guard ``num_reflections >= max_reflections - 1``
    evaluates to ``0 >= 0`` when max_reflections=1, causing an early
    return *before* files are ever added to abs_fnames.
    """

    def _make_stub_coder(self, tmp, rel_paths):
        """Create a stub coder with real files on disk and working file methods."""
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = os.path.realpath(tmp)
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.abs_root_path_cache = {}
        coder.io = MagicMock()
        coder.warning_given = True  # skip check_added_files

        # Create files on disk
        for rp in rel_paths:
            abs_p = os.path.join(str(tmp), rp)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("content")

        coder.get_all_relative_files = MagicMock(return_value=rel_paths)
        coder.get_addable_relative_files = MagicMock(return_value=rel_paths)
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        return coder

    def test_original_reply_completed_skips_files_with_max_reflections_1(self):
        """Prove the bug: with max_reflections=1, reply_completed adds no files."""
        from aider.coders.context_coder import ContextCoder

        db_rel = "db/characters/teo.md"
        prose_rel = "novel/Act 1 - Title/Chapter 1/Scene 1/PROSE.md"

        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_stub_coder(tmp, [db_rel, prose_rel])
            coder.num_reflections = 0
            coder.max_reflections = 1
            coder.gpt_prompts = MagicMock()
            coder.partial_response_content = (
                "## Files to examine:\n"
                f"- {db_rel}\n"
                f"- {prose_rel}\n"
            )

            # Monkey-patch space-aware get_file_mentions
            coder.get_file_mentions = types.MethodType(
                _space_aware_get_file_mentions, coder
            )

            # Call the ORIGINAL reply_completed
            ContextCoder.reply_completed(coder)

            # Bug: abs_fnames should have files but is EMPTY because
            # num_reflections (0) >= max_reflections - 1 (0) returns early
            self.assertEqual(len(coder.abs_fnames), 0)

    def test_override_reply_completed_adds_files(self):
        """With the fix override, files are added to abs_fnames."""
        db_rel = "db/characters/teo.md"
        prose_rel = "novel/Act 1 - Title/Chapter 1/Scene 1/PROSE.md"

        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_stub_coder(tmp, [db_rel, prose_rel])
            coder.partial_response_content = (
                "## Files to examine:\n"
                f"- {db_rel}\n"
                f"- {prose_rel}\n"
            )

            # Monkey-patch space-aware get_file_mentions
            coder.get_file_mentions = types.MethodType(
                _space_aware_get_file_mentions, coder
            )

            # Apply the fix override
            coder.reply_completed = types.MethodType(
                _reply_completed_no_reflect, coder
            )

            coder.reply_completed()

            # Both files should be in abs_fnames
            self.assertEqual(len(coder.abs_fnames), 2)
            abs_paths = {os.path.normpath(p) for p in coder.abs_fnames}
            resolved_tmp = os.path.realpath(tmp)
            expected = {
                os.path.normpath(os.path.join(resolved_tmp, db_rel)),
                os.path.normpath(os.path.join(resolved_tmp, prose_rel)),
            }
            self.assertEqual(abs_paths, expected)

    def test_override_no_reflection_triggered(self):
        """The override should NOT set reflected_message."""
        db_rel = "db/characters/teo.md"
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_stub_coder(tmp, [db_rel])
            coder.partial_response_content = f"- {db_rel}\n"
            coder.reflected_message = None

            coder.get_file_mentions = types.MethodType(
                _space_aware_get_file_mentions, coder
            )
            coder.reply_completed = types.MethodType(
                _reply_completed_no_reflect, coder
            )

            coder.reply_completed()

            # No reflection should be triggered
            self.assertIsNone(coder.reflected_message)

    def test_override_handles_empty_response(self):
        """The override should handle empty/blank LLM responses gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_stub_coder(tmp, ["db/characters/teo.md"])
            coder.partial_response_content = ""

            coder.reply_completed = types.MethodType(
                _reply_completed_no_reflect, coder
            )

            result = coder.reply_completed()
            self.assertTrue(result)
            self.assertEqual(len(coder.abs_fnames), 0)


class TestRunAutoContextIntegration(unittest.TestCase):
    """Integration tests for run_auto_context that verify the full pipeline.

    These tests create real files on disk, mock the LLM response (via
    ctx_coder.run), and verify that identified files end up in the main
    coder's abs_read_only_fnames.
    """

    def _make_main_coder(self, tmp):
        """Create a stub main coder for testing run_auto_context."""
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = os.path.realpath(tmp)
        coder.io = MagicMock()
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.main_model = MagicMock()
        coder.main_model.weak_model = MagicMock()
        coder.edit_format = "query"
        return coder

    def _make_mock_ctx_coder(self, tmp, rel_paths, llm_response):
        """Create a mock ctx_coder that simulates ContextCoder behaviour.

        The mock's run() method sets partial_response_content and calls
        reply_completed() — which by the time run() is called will have
        been overridden by run_auto_context.
        """
        mock_ctx = MagicMock()
        mock_ctx.root = os.path.realpath(tmp)
        mock_ctx.abs_fnames = set()
        mock_ctx.abs_read_only_fnames = set()
        mock_ctx.abs_root_path_cache = {}
        mock_ctx.warning_given = True
        mock_ctx.partial_response_content = ""

        # File listing methods
        mock_ctx.get_all_relative_files.return_value = rel_paths
        mock_ctx.get_addable_relative_files.return_value = rel_paths
        mock_ctx.get_inchat_relative_files.return_value = []

        # abs_root_path: resolve relative → absolute
        def abs_root_path(path):
            from aider import utils
            res = Path(tmp) / path
            return utils.safe_abs_path(res)
        mock_ctx.abs_root_path.side_effect = abs_root_path

        # add_rel_fname: resolve path and add to abs_fnames
        def add_rel_fname(rel_fname):
            mock_ctx.abs_fnames.add(mock_ctx.abs_root_path(rel_fname))
        mock_ctx.add_rel_fname.side_effect = add_rel_fname

        # run(): simulate the LLM response and call reply_completed
        # (which will have been overridden by run_auto_context)
        def fake_run(with_message=None, preproc=True):
            mock_ctx.partial_response_content = llm_response
            # At this point, run_auto_context has already monkey-patched
            # reply_completed with _reply_completed_no_reflect
            mock_ctx.reply_completed()
            return llm_response
        mock_ctx.run.side_effect = fake_run

        return mock_ctx

    def test_files_with_spaces_are_identified_and_added(self):
        """Novel paths with spaces are identified and added to main coder context."""
        db_rel = "db/characters/teo.md"
        prose_rel = "novel/Act 1 - Acolytes/Chapter 1 - Cold Water/Scene 1 - Title/PROSE.md"
        all_rels = [db_rel, prose_rel]

        llm_response = (
            "## Files to examine:\n\n"
            f"- {prose_rel}\n"
            "  - Contains the scene with Teo's dialogue\n"
            f"- {db_rel}\n"
            "  - Teo's character profile\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Create real files on disk
            for rel in all_rels:
                abs_p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(abs_p), exist_ok=True)
                Path(abs_p).write_text("content")

            main_coder = self._make_main_coder(tmp)
            mock_ctx = self._make_mock_ctx_coder(tmp, all_rels, llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Analyze Teo's voice")

            # Both files should be found
            self.assertEqual(len(result), 2)

            # Verify they were added to main coder's read-only context
            resolved_tmp = os.path.realpath(tmp)
            rel_names = {os.path.relpath(f, resolved_tmp) for f in main_coder.abs_read_only_fnames}
            self.assertIn(db_rel, rel_names)
            self.assertIn(prose_rel, rel_names)

    def test_only_simple_paths_are_identified(self):
        """Paths without spaces are identified correctly."""
        db_rel = "db/characters/teo.md"
        llm_response = (
            "## Files to examine:\n\n"
            f"- {db_rel}\n"
            "  - Character profile\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            abs_p = os.path.join(tmp, db_rel)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("content")

            main_coder = self._make_main_coder(tmp)
            mock_ctx = self._make_mock_ctx_coder(tmp, [db_rel], llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Who is Teo?")

            self.assertEqual(len(result), 1)
            resolved_tmp = os.path.realpath(tmp)
            rel_names = {os.path.relpath(f, resolved_tmp) for f in result}
            self.assertIn(db_rel, rel_names)

    def test_already_present_files_not_re_added(self):
        """Files already in the main coder's context should not be re-added."""
        db_rel = "db/characters/teo.md"
        llm_response = f"## Files:\n- {db_rel}\n"

        with tempfile.TemporaryDirectory() as tmp:
            abs_p = os.path.join(tmp, db_rel)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("content")

            main_coder = self._make_main_coder(tmp)
            # File is already in context
            from aider import utils
            main_coder.abs_read_only_fnames.add(
                utils.safe_abs_path(Path(tmp) / db_rel)
            )

            mock_ctx = self._make_mock_ctx_coder(tmp, [db_rel], llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Who is Teo?")

            # No new files should be added
            self.assertEqual(len(result), 0)

    def test_empty_llm_response_adds_no_files(self):
        """An empty LLM response should not cause errors or add files."""
        with tempfile.TemporaryDirectory() as tmp:
            main_coder = self._make_main_coder(tmp)
            mock_ctx = self._make_mock_ctx_coder(tmp, [], "")

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "What happens?")

            self.assertEqual(len(result), 0)

    def test_query_mode_uses_query_prompts(self):
        """In query mode, NovelContextPrompts should use 'examine' language."""
        with tempfile.TemporaryDirectory() as tmp:
            main_coder = self._make_main_coder(tmp)
            main_coder.edit_format = "query"

            mock_ctx = self._make_mock_ctx_coder(tmp, [], "No files needed.")

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                run_auto_context(main_coder, "Analyze the theme")

            # Check the prompts assigned to ctx_coder
            assigned_prompts = mock_ctx.gpt_prompts
            self.assertIn("files that are relevant to answering", assigned_prompts.main_system)

    def test_edit_mode_uses_modify_prompts(self):
        """In edit mode, NovelContextPrompts should use 'modify' language."""
        with tempfile.TemporaryDirectory() as tmp:
            main_coder = self._make_main_coder(tmp)
            main_coder.edit_format = "diff"

            mock_ctx = self._make_mock_ctx_coder(tmp, [], "No files needed.")

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                run_auto_context(main_coder, "Update the dialogue")

            assigned_prompts = mock_ctx.gpt_prompts
            self.assertIn("files which will need to be modified", assigned_prompts.main_system)

    def test_nonexistent_files_in_response_ignored(self):
        """Files mentioned by LLM but not on disk should be ignored."""
        real_rel = "db/characters/teo.md"
        fake_rel = "db/characters/nonexistent.md"
        llm_response = (
            "## Files:\n"
            f"- {real_rel}\n"
            f"- {fake_rel}\n"
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Only create the real file
            abs_p = os.path.join(tmp, real_rel)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("content")

            main_coder = self._make_main_coder(tmp)
            # Only the real file is in the repo file list
            mock_ctx = self._make_mock_ctx_coder(tmp, [real_rel], llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Who is Teo?")

            # Only the real file should be added
            self.assertEqual(len(result), 1)
            resolved_tmp = os.path.realpath(tmp)
            rel_names = {os.path.relpath(f, resolved_tmp) for f in result}
            self.assertIn(real_rel, rel_names)

    def test_edit_mode_adds_files_as_editable(self):
        """In edit mode, auto-context files should be added to abs_fnames (editable)."""
        prose_rel = "novel/Act 1 - Title/Chapter 1/Scene 1/PROSE.md"
        llm_response = f"## Files to modify:\n- {prose_rel}\n"

        with tempfile.TemporaryDirectory() as tmp:
            abs_p = os.path.join(tmp, prose_rel)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("Some prose")

            main_coder = self._make_main_coder(tmp)
            main_coder.edit_format = "diff"  # edit mode
            mock_ctx = self._make_mock_ctx_coder(tmp, [prose_rel], llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Make the opening more profound")

            self.assertEqual(len(result), 1)
            # File should be in abs_fnames (editable), NOT abs_read_only_fnames
            self.assertEqual(len(main_coder.abs_fnames), 1)
            self.assertEqual(len(main_coder.abs_read_only_fnames), 0)

    def test_query_mode_adds_files_as_readonly(self):
        """In query mode, auto-context files should be added to abs_read_only_fnames."""
        prose_rel = "novel/Act 1 - Title/Chapter 1/Scene 1/PROSE.md"
        llm_response = f"## Files to examine:\n- {prose_rel}\n"

        with tempfile.TemporaryDirectory() as tmp:
            abs_p = os.path.join(tmp, prose_rel)
            os.makedirs(os.path.dirname(abs_p), exist_ok=True)
            Path(abs_p).write_text("Some prose")

            main_coder = self._make_main_coder(tmp)
            main_coder.edit_format = "query"  # query mode
            mock_ctx = self._make_mock_ctx_coder(tmp, [prose_rel], llm_response)

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                result = run_auto_context(main_coder, "Analyze the opening")

            self.assertEqual(len(result), 1)
            # File should be in abs_read_only_fnames, NOT abs_fnames
            self.assertEqual(len(main_coder.abs_fnames), 0)
            self.assertEqual(len(main_coder.abs_read_only_fnames), 1)

    def test_auto_context_disabled_flag_on_ctx_coder(self):
        """The ctx_coder should have _auto_context_enabled=False to prevent recursion."""
        with tempfile.TemporaryDirectory() as tmp:
            main_coder = self._make_main_coder(tmp)
            mock_ctx = self._make_mock_ctx_coder(tmp, [], "No files.")

            from aider.coders.base_coder import Coder
            with patch.object(Coder, "create", return_value=mock_ctx):
                run_auto_context(main_coder, "test")

            # _auto_context_enabled should be False on the ctx_coder
            self.assertFalse(mock_ctx._auto_context_enabled)


if __name__ == "__main__":
    unittest.main()
