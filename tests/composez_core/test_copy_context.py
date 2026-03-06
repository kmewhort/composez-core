"""Tests for novel copy-context and paste-response commands."""

import unittest
from unittest.mock import MagicMock, patch

from aider.coders.chat_chunks import ChatChunks
from composez_core.novel_commands import (
    NovelCommands,
    _extract_text,
    _format_message,
    apply_pasted_response,
    build_copy_context_markdown,
)


def _make_chunks():
    """Build a ChatChunks instance with representative content."""
    return ChatChunks(
        system=[dict(role="system", content="You are a fiction writer.")],
        examples=[
            dict(role="user", content="Write a scene."),
            dict(role="assistant", content="Here is a scene."),
        ],
        readonly_files=[
            dict(role="user", content="db/characters/sarah.md:\nSarah is brave."),
            dict(role="assistant", content="Ok."),
        ],
        repo=[dict(role="user", content="act/1/chapter/1/scene/1/PROSE.md")],
        done=[
            dict(role="user", content="Previous turn message."),
            dict(role="assistant", content="Previous turn response."),
        ],
        chat_files=[
            dict(
                role="user",
                content="act/1/chapter/1/scene/1/PROSE.md:\nThe rain fell.",
            ),
            dict(role="assistant", content="Ok."),
        ],
        cur=[dict(role="user", content="Make the scene more vivid.")],
        reminder=[dict(role="system", content="Remember the edit format.")],
    )


class TestExtractText(unittest.TestCase):
    """Test the _extract_text helper."""

    def test_string_content(self):
        self.assertEqual(_extract_text("hello"), "hello")

    def test_list_content(self):
        content = [
            {"type": "text", "text": "Part one."},
            {"type": "text", "text": "Part two."},
        ]
        self.assertEqual(_extract_text(content), "Part one.\nPart two.")

    def test_none_content(self):
        self.assertEqual(_extract_text(None), "")

    def test_mixed_list(self):
        content = [
            {"type": "image_url", "url": "http://example.com/img.png"},
            {"type": "text", "text": "Caption."},
        ]
        self.assertEqual(_extract_text(content), "Caption.")


class TestFormatMessage(unittest.TestCase):
    """Test the _format_message helper."""

    def test_system_message(self):
        msg = dict(role="system", content="You are helpful.")
        result = _format_message(msg)
        self.assertIn("## SYSTEM", result)
        self.assertIn("You are helpful.", result)

    def test_user_message(self):
        msg = dict(role="user", content="Do something.")
        result = _format_message(msg)
        self.assertIn("## USER", result)

    def test_assistant_message(self):
        msg = dict(role="assistant", content="Done.")
        result = _format_message(msg)
        self.assertIn("## ASSISTANT", result)

    def test_empty_content_returns_empty(self):
        msg = dict(role="user", content="")
        result = _format_message(msg)
        self.assertEqual(result, "")


class TestBuildCopyContextMarkdown(unittest.TestCase):
    """Test build_copy_context_markdown with a mock coder."""

    def _make_coder(self, chunks):
        coder = MagicMock()
        coder.format_chat_chunks.return_value = chunks
        return coder

    def test_full_context_includes_all_chunks(self):
        chunks = _make_chunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder)

        # System prompt
        self.assertIn("## SYSTEM", result)
        self.assertIn("fiction writer", result)
        # Examples
        self.assertIn("Write a scene.", result)
        self.assertIn("Here is a scene.", result)
        # Read-only files
        self.assertIn("Sarah is brave.", result)
        # Repo map
        self.assertIn("PROSE.md", result)
        # Done (history)
        self.assertIn("Previous turn message.", result)
        self.assertIn("Previous turn response.", result)
        # Chat files
        self.assertIn("The rain fell.", result)
        # Current message
        self.assertIn("Make the scene more vivid.", result)
        # Reminder
        self.assertIn("Remember the edit format.", result)

    def test_full_context_preserves_roles(self):
        chunks = _make_chunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder)

        # Check that all roles appear
        self.assertIn("## SYSTEM", result)
        self.assertIn("## USER", result)
        self.assertIn("## ASSISTANT", result)

    def test_continue_mode_excludes_cached(self):
        chunks = _make_chunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder, continue_only=True)

        # Should NOT include system, examples, readonly, repo, done
        self.assertNotIn("fiction writer", result)
        self.assertNotIn("Write a scene.", result)
        self.assertNotIn("Sarah is brave.", result)
        self.assertNotIn("Previous turn message.", result)
        # Should include chat_files, cur, reminder
        self.assertIn("The rain fell.", result)
        self.assertIn("Make the scene more vivid.", result)
        self.assertIn("Remember the edit format.", result)

    def test_extra_instruction_appended(self):
        chunks = _make_chunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder, extra="Focus on dialogue.")
        self.assertIn("Focus on dialogue.", result)

    def test_empty_chunks(self):
        chunks = ChatChunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder)
        self.assertEqual(result.strip(), "")

    def test_multipart_content(self):
        chunks = ChatChunks(
            system=[dict(role="system", content=[
                {"type": "text", "text": "System part one."},
                {"type": "text", "text": "System part two."},
            ])],
        )
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder)
        self.assertIn("System part one.", result)
        self.assertIn("System part two.", result)

    def test_message_order_matches_llm(self):
        """The order should be: system, examples, readonly, repo, done, chat, cur, reminder."""
        chunks = _make_chunks()
        coder = self._make_coder(chunks)

        result = build_copy_context_markdown(coder)

        # Extract positions
        pos_system = result.index("fiction writer")
        pos_example = result.index("Write a scene.")
        pos_readonly = result.index("Sarah is brave.")
        pos_repo = result.index("act/1/chapter/1/scene/1/PROSE.md")
        pos_done = result.index("Previous turn message.")
        pos_chat = result.index("The rain fell.")
        pos_cur = result.index("Make the scene more vivid.")
        pos_reminder = result.index("Remember the edit format.")

        self.assertLess(pos_system, pos_example)
        self.assertLess(pos_example, pos_readonly)
        self.assertLess(pos_readonly, pos_repo)
        self.assertLess(pos_repo, pos_done)
        self.assertLess(pos_done, pos_chat)
        self.assertLess(pos_chat, pos_cur)
        self.assertLess(pos_cur, pos_reminder)


class TestCmdCopyContext(unittest.TestCase):
    """Test the NovelCommands.cmd_copy_context slash command."""

    def _make_cmd(self):
        io = MagicMock()
        coder = MagicMock()
        coder.format_chat_chunks.return_value = _make_chunks()
        cmds = NovelCommands(io, coder, root="/tmp/test")
        return cmds, io, coder

    @patch("composez_core.novel_commands.pyperclip")
    def test_copies_to_clipboard(self, mock_pyperclip):
        cmds, io, coder = self._make_cmd()
        cmds.cmd_copy_context("")

        mock_pyperclip.copy.assert_called_once()
        copied_text = mock_pyperclip.copy.call_args[0][0]
        self.assertIn("fiction writer", copied_text)
        self.assertIn("Make the scene more vivid.", copied_text)
        io.tool_output.assert_called_once()
        self.assertIn("full context", io.tool_output.call_args[0][0])

    @patch("composez_core.novel_commands.pyperclip")
    def test_continue_mode(self, mock_pyperclip):
        cmds, io, coder = self._make_cmd()
        cmds.cmd_copy_context("continue")

        mock_pyperclip.copy.assert_called_once()
        copied_text = mock_pyperclip.copy.call_args[0][0]
        # Should NOT include system prompt in continue mode
        self.assertNotIn("fiction writer", copied_text)
        # Should include current content
        self.assertIn("The rain fell.", copied_text)
        io.tool_output.assert_called_once()
        self.assertIn("continuation", io.tool_output.call_args[0][0])

    @patch("composez_core.novel_commands.pyperclip")
    def test_extra_args_passed_through(self, mock_pyperclip):
        cmds, io, coder = self._make_cmd()
        cmds.cmd_copy_context("Focus on dialogue.")

        copied_text = mock_pyperclip.copy.call_args[0][0]
        self.assertIn("Focus on dialogue.", copied_text)

    @patch("composez_core.novel_commands.pyperclip")
    def test_clipboard_error_handled(self, mock_pyperclip):
        mock_pyperclip.copy.side_effect = Exception("No clipboard")
        cmds, io, coder = self._make_cmd()
        cmds.cmd_copy_context("")

        io.tool_error.assert_called_once()
        self.assertIn("clipboard", io.tool_error.call_args[0][0].lower())


class TestApplyPastedResponse(unittest.TestCase):
    """Test the apply_pasted_response helper."""

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.io = MagicMock()
        coder.partial_response_content = ""
        coder.partial_response_function_call = []
        coder.cur_messages = []
        coder.aider_edited_files = set()
        coder.auto_lint = False
        coder.gpt_prompts = MagicMock()
        return coder

    def test_sets_partial_response_content(self):
        coder = self._make_coder()

        with patch.object(type(coder), "apply_updates", return_value=set()), \
             patch.object(type(coder), "add_assistant_reply_to_cur_messages"):
            apply_pasted_response(coder, "Here is a response.")

        self.assertEqual(coder.partial_response_content, "Here is a response.")

    def test_calls_apply_updates(self):
        coder = self._make_coder()

        with patch.object(type(coder), "apply_updates", return_value=set()) as mock_apply, \
             patch.object(type(coder), "add_assistant_reply_to_cur_messages"):
            apply_pasted_response(coder, "Response text.")

        mock_apply.assert_called_once()

    def test_auto_commits_on_edits(self):
        coder = self._make_coder()
        edited_files = {"file.md"}

        with patch.object(type(coder), "apply_updates", return_value=edited_files), \
             patch.object(type(coder), "auto_commit", return_value="saved") as mock_commit, \
             patch.object(type(coder), "move_back_cur_messages") as mock_move, \
             patch.object(type(coder), "add_assistant_reply_to_cur_messages"):
            apply_pasted_response(coder, "Edits here.")

        mock_commit.assert_called_once_with(edited_files)
        mock_move.assert_called_once_with("saved")

    def test_shows_no_edits_message(self):
        coder = self._make_coder()

        with patch.object(type(coder), "apply_updates", return_value=set()), \
             patch.object(type(coder), "add_assistant_reply_to_cur_messages"):
            apply_pasted_response(coder, "Just a comment, no edits.")

        coder.io.tool_output.assert_called_with("No edits found in the pasted response.")

    def test_handles_apply_error(self):
        coder = self._make_coder()

        with patch.object(type(coder), "apply_updates", side_effect=ValueError("parse error")), \
             patch.object(type(coder), "add_assistant_reply_to_cur_messages"):
            apply_pasted_response(coder, "Bad response.")

        coder.io.tool_error.assert_called_once()


class TestCmdPasteResponse(unittest.TestCase):
    """Test the NovelCommands.cmd_paste_response slash command."""

    def _make_cmd(self):
        io = MagicMock()
        coder = MagicMock()
        coder.partial_response_content = ""
        coder.partial_response_function_call = []
        coder.cur_messages = []
        coder.aider_edited_files = set()
        coder.auto_lint = False
        coder.apply_updates.return_value = set()
        cmds = NovelCommands(io, coder, root="/tmp/test")
        return cmds, io, coder

    @patch("composez_core.novel_commands.apply_pasted_response")
    @patch("composez_core.novel_commands.pyperclip")
    def test_reads_from_clipboard(self, mock_pyperclip, mock_apply):
        mock_pyperclip.paste.return_value = "LLM response text."
        cmds, io, coder = self._make_cmd()
        cmds.cmd_paste_response("")

        mock_pyperclip.paste.assert_called_once()
        mock_apply.assert_called_once_with(coder, "LLM response text.")

    @patch("composez_core.novel_commands.apply_pasted_response")
    def test_uses_args_if_provided(self, mock_apply):
        cmds, io, coder = self._make_cmd()
        cmds.cmd_paste_response("Direct response text.")

        mock_apply.assert_called_once_with(coder, "Direct response text.")

    @patch("composez_core.novel_commands.pyperclip")
    def test_error_on_empty_clipboard(self, mock_pyperclip):
        mock_pyperclip.paste.return_value = ""
        cmds, io, coder = self._make_cmd()
        cmds.cmd_paste_response("")

        io.tool_error.assert_called_once()
        self.assertIn("No response text", io.tool_error.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
