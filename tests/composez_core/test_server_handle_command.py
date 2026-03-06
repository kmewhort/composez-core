"""Tests for _handle_command in the Novel UI server.

Verifies that:
- LLM response text (ai_output) is captured and sent to the WebSocket
- SwitchCoder exceptions are handled gracefully (not treated as errors)
- Novel commands IO is synced with CaptureIO
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

try:
    from composez_core.server.app import _handle_command
    from aider.commands import SwitchCoder
    from aider.io import InputOutput

    HAS_DEPS = True
except (ImportError, ModuleNotFoundError):
    HAS_DEPS = False


class FakeWebSocket:
    """Records messages sent via send_json."""

    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)

    def messages_of_type(self, msg_type):
        return [m for m in self.messages if m.get("type") == msg_type]


class FakeNovelCommands:
    """Minimal stand-in for NovelCommands to track IO swaps."""

    def __init__(self, io):
        self.io = io


class FakeCommands:
    """Minimal Commands stub that allows controlling what run() does."""

    def __init__(self, io):
        self.io = io
        self._novel_commands = None
        self._run_side_effect = None

    def run(self, prompt):
        if self._run_side_effect:
            self._run_side_effect(prompt)


class FakeCoder:
    """Minimal Coder stub."""

    def __init__(self):
        self.io = InputOutput(pretty=False, yes=True)
        self.commands = FakeCommands(self.io)


def _run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipUnless(HAS_DEPS, "Missing server dependencies")
class TestHandleCommandCapturesAiOutput(unittest.TestCase):
    """ai_output from the LLM should be forwarded as command_output."""

    def test_ai_output_captured(self):
        """When a command triggers ai_output, it appears in command_output messages."""
        coder = FakeCoder()
        ws = FakeWebSocket()

        def side_effect(prompt):
            # Simulate what happens inside coder.run() — the IO's ai_output
            # is called with the LLM response text.
            coder.commands.io.ai_output("Here is the generated prose.")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/write 1 1 1"))

        outputs = ws.messages_of_type("command_output")
        texts = [m["text"] for m in outputs]
        self.assertTrue(
            any("generated prose" in t for t in texts),
            f"Expected ai_output text in command_output messages, got: {texts}",
        )

    def test_ai_output_empty_ignored(self):
        """Empty or whitespace-only ai_output should not produce messages."""
        coder = FakeCoder()
        ws = FakeWebSocket()

        def side_effect(prompt):
            coder.commands.io.ai_output("")
            coder.commands.io.ai_output("   ")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/test"))

        outputs = ws.messages_of_type("command_output")
        # Only command_start and command_end, no actual output lines
        self.assertEqual(outputs, [])


@unittest.skipUnless(HAS_DEPS, "Missing server dependencies")
class TestHandleCommandSwitchCoder(unittest.TestCase):
    """SwitchCoder should not produce an error in the WebSocket output."""

    @patch("composez_core.server.app._apply_switch_coder")
    def test_switch_coder_no_error(self, mock_apply):
        """SwitchCoder is silently handled — no command_error messages."""
        mock_apply.return_value = MagicMock(edit_format="whole", selection_filename=None)
        coder = FakeCoder()
        ws = FakeWebSocket()

        def side_effect(prompt):
            coder.commands.io.tool_output("Writing scene...")
            raise SwitchCoder(
                edit_format="whole",
                summarize_from_coder=False,
            )

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/write 1 1 1"))

        errors = ws.messages_of_type("command_error")
        self.assertEqual(errors, [], f"Expected no errors, got: {errors}")

        # The tool_output before SwitchCoder should still be captured
        outputs = ws.messages_of_type("command_output")
        texts = [m["text"] for m in outputs]
        self.assertIn("Writing scene...", texts)

    @patch("composez_core.server.app._apply_switch_coder")
    def test_switch_coder_command_end_sent(self, mock_apply):
        """command_end is always sent even when SwitchCoder is raised."""
        mock_apply.return_value = MagicMock(edit_format="whole", selection_filename=None)
        coder = FakeCoder()
        ws = FakeWebSocket()

        def side_effect(prompt):
            raise SwitchCoder(edit_format="whole")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/write 1"))

        ends = ws.messages_of_type("command_end")
        self.assertEqual(len(ends), 1)


@unittest.skipUnless(HAS_DEPS, "Missing server dependencies")
class TestHandleCommandNovelIOSync(unittest.TestCase):
    """When novel commands are cached, their IO must be swapped to CaptureIO."""

    def test_novel_io_synced_during_command(self):
        """Cached novel commands IO is temporarily set to CaptureIO."""
        coder = FakeCoder()
        ws = FakeWebSocket()

        original_io = coder.commands.io
        nc = FakeNovelCommands(original_io)
        coder.commands._novel_commands = nc

        observed_ios = []

        def side_effect(prompt):
            # During command execution, novel commands IO should be CaptureIO
            observed_ios.append(nc.io)
            nc.io.tool_output("from novel commands")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/summarize 1"))

        # During execution, the IO should NOT have been the original
        self.assertNotEqual(observed_ios[0], original_io)

        # After execution, the IO should be restored
        self.assertIs(nc.io, original_io)

        # The output should have been captured
        outputs = ws.messages_of_type("command_output")
        texts = [m["text"] for m in outputs]
        self.assertIn("from novel commands", texts)

    def test_io_restored_after_exception(self):
        """IO is restored even when the command raises an exception."""
        coder = FakeCoder()
        ws = FakeWebSocket()

        original_io = coder.commands.io
        nc = FakeNovelCommands(original_io)
        coder.commands._novel_commands = nc

        def side_effect(prompt):
            raise RuntimeError("Something went wrong")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/write 1"))

        # IO should be restored
        self.assertIs(coder.commands.io, original_io)
        self.assertIs(nc.io, original_io)

        # Error should be captured
        errors = ws.messages_of_type("command_error")
        self.assertTrue(any("Something went wrong" in e["text"] for e in errors))


@unittest.skipUnless(HAS_DEPS, "Missing server dependencies")
class TestHandleCommandRegularException(unittest.TestCase):
    """Regular exceptions should still be reported as errors."""

    def test_generic_exception_reported(self):
        coder = FakeCoder()
        ws = FakeWebSocket()

        def side_effect(prompt):
            raise ValueError("bad input")

        coder.commands._run_side_effect = side_effect

        _run_async(_handle_command(ws, coder, "/bad-command"))

        errors = ws.messages_of_type("command_error")
        self.assertTrue(any("bad input" in e["text"] for e in errors))


if __name__ == "__main__":
    unittest.main()
