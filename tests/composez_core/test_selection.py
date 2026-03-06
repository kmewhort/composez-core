"""Tests for the selection edit format — coder, prompts, parser, and commands."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aider.coders.selection_coder import SelectionCoder, _apply_selection_replacement
from aider.coders.selection_prompts import SelectionPrompts


class TestSelectionPrompts(unittest.TestCase):
    """Verify the SelectionPrompts content."""

    def setUp(self):
        self.prompts = SelectionPrompts()

    def test_main_system_mentions_selection_mode(self):
        self.assertIn("selection mode", self.prompts.main_system)

    def test_system_reminder_mentions_replacement_delimiters(self):
        self.assertIn("REPLACEMENT TEXT START", self.prompts.system_reminder)
        self.assertIn("REPLACEMENT TEXT END", self.prompts.system_reminder)

    def test_system_reminder_mentions_selected_text_delimiters(self):
        self.assertIn("SELECTED TEXT START", self.prompts.system_reminder)
        self.assertIn("SELECTED TEXT END", self.prompts.system_reminder)

    def test_example_messages_present(self):
        self.assertEqual(len(self.prompts.example_messages), 4)
        self.assertEqual(self.prompts.example_messages[0]["role"], "user")
        self.assertEqual(self.prompts.example_messages[1]["role"], "assistant")
        self.assertEqual(self.prompts.example_messages[2]["role"], "user")
        self.assertEqual(self.prompts.example_messages[3]["role"], "assistant")

    def test_example_user_has_selection_block(self):
        user = self.prompts.example_messages[0]["content"]
        self.assertIn("SELECTED TEXT", user)
        self.assertIn("Range:", user)
        self.assertIn("SELECTED TEXT START", user)


class TestApplySelectionReplacement(unittest.TestCase):
    """Test _apply_selection_replacement for various range scenarios."""

    def test_single_line_replacement(self):
        content = "line zero\nline one\nline two\nline three\n"
        sel_range = {
            "start": {"line": 1, "character": 5},
            "end": {"line": 1, "character": 8},
        }
        result = _apply_selection_replacement(content, sel_range, "ONE")
        self.assertEqual(result, "line zero\nline ONE\nline two\nline three\n")

    def test_multi_line_replacement(self):
        content = "aaa\nbbb\nccc\nddd\neee\n"
        sel_range = {
            "start": {"line": 1, "character": 0},
            "end": {"line": 3, "character": 3},
        }
        result = _apply_selection_replacement(content, sel_range, "REPLACED")
        self.assertEqual(result, "aaa\nREPLACED\neee\n")

    def test_replacement_at_start_of_file(self):
        content = "hello world\nsecond line\n"
        sel_range = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        }
        result = _apply_selection_replacement(content, sel_range, "HELLO")
        self.assertEqual(result, "HELLO world\nsecond line\n")

    def test_replacement_at_end_of_line(self):
        content = "hello world\n"
        sel_range = {
            "start": {"line": 0, "character": 6},
            "end": {"line": 0, "character": 11},
        }
        result = _apply_selection_replacement(content, sel_range, "earth")
        self.assertEqual(result, "hello earth\n")

    def test_replacement_spanning_entire_content(self):
        content = "old stuff"
        sel_range = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 9},
        }
        result = _apply_selection_replacement(content, sel_range, "new stuff")
        self.assertEqual(result, "new stuff")

    def test_replacement_with_more_lines(self):
        content = "aaa\nbbb\nccc\n"
        sel_range = {
            "start": {"line": 1, "character": 0},
            "end": {"line": 1, "character": 3},
        }
        result = _apply_selection_replacement(content, sel_range, "xxx\nyyy\nzzz")
        self.assertEqual(result, "aaa\nxxx\nyyy\nzzz\nccc\n")


class TestSelectionCoderParsing(unittest.TestCase):
    """Test the SelectionCoder's fenced-block parser."""

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")
        coder.partial_response_content = ""
        coder.selection_filename = "test.md"
        coder.selection_range = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        }
        coder.selection_text = "hello"
        return coder

    def test_extract_simple_fenced_block(self):
        coder = self._make_coder()
        content = "Here is the replacement:\n\n```\nnew text here\n```\n"
        result = coder._extract_fenced_block(content)
        self.assertEqual(result, "new text here")

    def test_extract_fenced_block_with_language(self):
        coder = self._make_coder()
        content = "```markdown\nsome **bold** text\n```\n"
        result = coder._extract_fenced_block(content)
        self.assertEqual(result, "some **bold** text")

    def test_extract_multiline_fenced_block(self):
        coder = self._make_coder()
        content = "```\nline one\nline two\nline three\n```\n"
        result = coder._extract_fenced_block(content)
        self.assertEqual(result, "line one\nline two\nline three")

    def test_no_fenced_block_returns_none(self):
        coder = self._make_coder()
        result = coder._extract_fenced_block("Just some plain text without fences.")
        self.assertIsNone(result)

    def test_get_edits_returns_tuple(self):
        coder = self._make_coder()
        coder.partial_response_content = "```\nreplacement\n```\n"
        edits = coder.get_edits()
        self.assertEqual(len(edits), 1)
        fname, sel_range, replacement = edits[0]
        self.assertEqual(fname, "test.md")
        self.assertEqual(replacement, "replacement")

    def test_get_edits_raises_on_no_fence(self):
        coder = self._make_coder()
        coder.partial_response_content = "I have no fenced block."
        with self.assertRaises(ValueError):
            coder.get_edits()


class TestSelectionCoderSelectionPrompt(unittest.TestCase):
    """Test the selection_prompt() method that builds the SELECTION block."""

    def test_selection_prompt_output(self):
        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")
        coder.io = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            coder.root = tmpdir
            fname = "test.md"
            full_path = os.path.join(tmpdir, fname)
            Path(full_path).write_text(
                "line 0\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\n"
            )

            coder.selection_filename = fname
            coder.selection_range = {
                "start": {"line": 3, "character": 0},
                "end": {"line": 4, "character": 6},
            }
            coder.selection_text = "line 3\nline 4"
            coder.io.read_text.return_value = Path(full_path).read_text()

            # Need abs_root_path method
            coder.abs_root_path = lambda f: os.path.join(tmpdir, f)

            prompt = coder.selection_prompt()

            self.assertIn("SELECTED TEXT", prompt)
            self.assertIn('"start"', prompt)
            self.assertIn("before the selection", prompt)
            self.assertIn("SELECTED TEXT START", prompt)
            self.assertIn("after the selection", prompt)
            self.assertIn("line 3\nline 4", prompt)

    def test_selection_prompt_with_no_selection(self):
        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")
        prompt = coder.selection_prompt()
        self.assertEqual(prompt, "")


class TestSelectionCoderRunStream(unittest.TestCase):
    """Test that run_stream() prepends the selection block to the user message."""

    def test_run_stream_prepends_selection_block(self):
        """run_stream should prepend the SELECTED TEXT block to the user message."""
        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")
        coder.io = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            coder.root = tmpdir
            fname = "test.md"
            full_path = os.path.join(tmpdir, fname)
            Path(full_path).write_text(
                "line 0\nline 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\n"
            )

            coder.selection_filename = fname
            coder.selection_range = {
                "start": {"line": 3, "character": 0},
                "end": {"line": 4, "character": 6},
            }
            coder.selection_text = "line 3\nline 4"
            coder.io.read_text.return_value = Path(full_path).read_text()
            coder.abs_root_path = lambda f: os.path.join(tmpdir, f)

            # Capture what run_stream passes to the parent's run_stream
            captured = {}

            def fake_parent_run_stream(self_arg, user_message):
                captured["message"] = user_message
                return iter([])  # yield nothing

            # Patch the parent's run_stream
            from aider.coders.base_coder import Coder

            original_run_stream = Coder.run_stream
            Coder.run_stream = fake_parent_run_stream
            try:
                list(coder.run_stream("Change bed to head"))
            finally:
                Coder.run_stream = original_run_stream

            self.assertIn("SELECTED TEXT START", captured["message"])
            self.assertIn("line 3\nline 4", captured["message"])
            self.assertIn("Change bed to head", captured["message"])


class TestSelectionCoderUpdateAfterReplace(unittest.TestCase):
    """Test that _update_selection_after_replace updates range and text."""

    def test_single_line_replacement_updates_range(self):
        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")

        old_content = "hello world"
        sel_range = {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        }
        replacement = "goodbye"

        coder._update_selection_after_replace(old_content, sel_range, replacement)

        self.assertEqual(coder.selection_text, "goodbye")
        self.assertEqual(coder.selection_range["start"]["line"], 0)
        self.assertEqual(coder.selection_range["start"]["character"], 0)
        self.assertEqual(coder.selection_range["end"]["line"], 0)
        self.assertEqual(coder.selection_range["end"]["character"], 7)

    def test_multi_line_replacement_updates_range(self):
        coder = SelectionCoder.__new__(SelectionCoder)
        coder.fence = ("```", "```")

        old_content = "single line"
        sel_range = {
            "start": {"line": 2, "character": 5},
            "end": {"line": 2, "character": 11},
        }
        replacement = "multi\nline\nreplacement"

        coder._update_selection_after_replace(old_content, sel_range, replacement)

        self.assertEqual(coder.selection_text, "multi\nline\nreplacement")
        self.assertEqual(coder.selection_range["start"]["line"], 2)
        self.assertEqual(coder.selection_range["start"]["character"], 5)
        self.assertEqual(coder.selection_range["end"]["line"], 4)
        self.assertEqual(coder.selection_range["end"]["character"], 11)


class TestParseSelectionArg(unittest.TestCase):
    """Test the _parse_selection_arg helper in novel_commands."""

    def _make_commands(self, tmpdir):
        from composez_core.novel_commands import NovelCommands

        io = MagicMock()
        coder = MagicMock()
        coder.root = tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(io, coder, root=tmpdir)
        return cmds

    def test_valid_arg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = "test.md"
            full = os.path.join(tmpdir, fname)
            Path(full).write_text("line 0\nline 1\nline 2\n")

            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg("test.md:1:1-2:6")

            self.assertIsNotNone(result)
            rel_fname, sel_range, sel_text = result
            self.assertEqual(rel_fname, "test.md")
            # 1-based (1:1-2:6) -> 0-based (0:0-1:5)
            self.assertEqual(sel_range["start"]["line"], 0)
            self.assertEqual(sel_range["start"]["character"], 0)
            self.assertEqual(sel_range["end"]["line"], 1)
            self.assertEqual(sel_range["end"]["character"], 5)

    def test_invalid_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg("badformat")
            self.assertIsNone(result)

    def test_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg("nonexistent.md:1:1-1:5")
            self.assertIsNone(result)

    def test_line_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = "test.md"
            Path(os.path.join(tmpdir, fname)).write_text("one line\n")

            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg("test.md:1:1-99:1")
            self.assertIsNone(result)

    def test_single_line_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = "test.md"
            Path(os.path.join(tmpdir, fname)).write_text("hello world\n")

            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg("test.md:1:1-1:5")

            self.assertIsNotNone(result)
            _, sel_range, sel_text = result
            self.assertEqual(sel_text, "hell")  # 0:0 to 0:4 -> "hell"

    def test_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "act", "1 - Title", "chapter", "1 - Ch")
            os.makedirs(subdir)
            fname_rel = "act/1 - Title/chapter/1 - Ch/PROSE.md"
            full = os.path.join(tmpdir, fname_rel)
            Path(full).write_text("some prose here\n")

            cmds = self._make_commands(tmpdir)
            result = cmds._parse_selection_arg(
                "act/1 - Title/chapter/1 - Ch/PROSE.md:1:1-1:10"
            )
            self.assertIsNotNone(result)
            _, _, sel_text = result
            self.assertEqual(sel_text, "some pros")


class TestSelectionCoderEditFormat(unittest.TestCase):
    """Verify SelectionCoder has the right edit_format."""

    def test_edit_format(self):
        self.assertEqual(SelectionCoder.edit_format, "selection")

    def test_registered_in_coders(self):
        import aider.coders as coders

        formats = [c.edit_format for c in coders.__all__ if hasattr(c, "edit_format")]
        self.assertIn("selection", formats)


if __name__ == "__main__":
    unittest.main()
