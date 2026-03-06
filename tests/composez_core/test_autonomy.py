"""Tests for the autonomy strategy system (aider/coders/autonomy.py).

Validates that every combination of edit mode × autonomy level produces
the correct coder type, prompts, and orchestration behaviour.
"""

import unittest
from unittest.mock import MagicMock, patch

from aider.coders.autonomy import (
    AUTONOMY_LEVELS,
    AgentStrategy,
    AutonomyStrategy,
    ComposeStrategy,
    get_strategy,
)


# ---------------------------------------------------------------------------
# Strategy basics
# ---------------------------------------------------------------------------


class TestGetStrategy(unittest.TestCase):
    """get_strategy() returns the correct strategy for each level."""

    def test_direct(self):
        s = get_strategy("direct")
        self.assertIsInstance(s, AutonomyStrategy)
        self.assertEqual(s.name, "direct")

    def test_compose(self):
        s = get_strategy("compose")
        self.assertIsInstance(s, ComposeStrategy)
        self.assertEqual(s.name, "compose")

    def test_agent(self):
        s = get_strategy("agent")
        self.assertIsInstance(s, AgentStrategy)
        self.assertEqual(s.name, "agent")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            get_strategy("bogus")

    def test_autonomy_levels_constant(self):
        self.assertEqual(AUTONOMY_LEVELS, ("direct", "compose", "agent"))


# ---------------------------------------------------------------------------
# Direct strategy
# ---------------------------------------------------------------------------


class TestDirectStrategy(unittest.TestCase):
    """Direct strategy is a no-op."""

    def test_reply_completed_returns_none(self):
        s = AutonomyStrategy()
        result = s.reply_completed(MagicMock())
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Compose strategy
# ---------------------------------------------------------------------------


class TestComposeStrategy(unittest.TestCase):
    """ComposeStrategy spawns an editor coder to implement the plan."""

    def _make_coder(self, edit_format="query"):
        coder = MagicMock()
        coder.edit_format = edit_format
        coder.auto_accept_architect = True
        coder.partial_response_content = "Here is the plan for the changes."
        coder.verbose = False
        coder.total_cost = 0.5
        coder.main_model = MagicMock()
        coder.main_model.editor_model = MagicMock()
        coder.main_model.editor_edit_format = "editor-diff"
        coder.io = MagicMock()
        return coder

    def test_returns_true(self):
        """Must return True so apply_updates is skipped."""
        strategy = ComposeStrategy()
        coder = self._make_coder()
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            mock_editor = MagicMock()
            MockCoder.create.return_value = mock_editor
            result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_returns_true_on_empty_content(self):
        strategy = ComposeStrategy()
        coder = self._make_coder()
        coder.partial_response_content = ""
        result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_returns_true_on_decline(self):
        strategy = ComposeStrategy()
        coder = self._make_coder()
        coder.auto_accept_architect = False
        coder.io.confirm_ask.return_value = False
        result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_spawns_editor_coder(self):
        strategy = ComposeStrategy()
        coder = self._make_coder()
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            mock_editor = MagicMock()
            MockCoder.create.return_value = mock_editor
            strategy.reply_completed(coder)
            MockCoder.create.assert_called_once()
            mock_editor.run.assert_called_once()

    def test_query_compose_uses_query_format(self):
        """query + compose → editor phase should use 'query' edit format."""
        strategy = ComposeStrategy()
        coder = self._make_coder(edit_format="query")
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            call_kwargs = MockCoder.create.call_args[1]
            self.assertEqual(call_kwargs["edit_format"], "query")

    def test_query_compose_skips_move_back(self):
        """query + compose → no 'I made those changes' message."""
        strategy = ComposeStrategy()
        coder = self._make_coder(edit_format="query")
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            coder.move_back_cur_messages.assert_not_called()

    def test_edit_compose_uses_editor_format(self):
        """edit + compose → editor phase should use model's editor format."""
        strategy = ComposeStrategy()
        coder = self._make_coder(edit_format="diff")
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            call_kwargs = MockCoder.create.call_args[1]
            self.assertEqual(call_kwargs["edit_format"], "editor-diff")

    def test_edit_compose_calls_move_back(self):
        """edit + compose → 'I made those changes' message."""
        strategy = ComposeStrategy()
        coder = self._make_coder(edit_format="diff")
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            coder.move_back_cur_messages.assert_called_once()

    def test_selection_compose_preserves_format(self):
        """selection + compose → editor phase should use 'selection' format."""
        strategy = ComposeStrategy()
        coder = self._make_coder(edit_format="selection")
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            call_kwargs = MockCoder.create.call_args[1]
            self.assertEqual(call_kwargs["edit_format"], "selection")

    def test_editor_phase_uses_direct_autonomy(self):
        """Editor phase must always be direct (no recursion)."""
        strategy = ComposeStrategy()
        coder = self._make_coder()
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            call_kwargs = MockCoder.create.call_args[1]
            self.assertEqual(call_kwargs["autonomy"], "direct")

    def test_auto_accept_true_skips_confirm(self):
        strategy = ComposeStrategy()
        coder = self._make_coder()
        coder.auto_accept_architect = True
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            coder.io.confirm_ask.assert_not_called()

    def test_auto_accept_false_asks_confirm(self):
        strategy = ComposeStrategy()
        coder = self._make_coder()
        coder.auto_accept_architect = False
        coder.io.confirm_ask.return_value = True
        with patch("aider.coders.base_coder.Coder") as MockCoder:
            MockCoder.create.return_value = MagicMock()
            strategy.reply_completed(coder)
            coder.io.confirm_ask.assert_called_once_with("Edit the files?")


# ---------------------------------------------------------------------------
# Agent strategy
# ---------------------------------------------------------------------------


class TestAgentStrategy(unittest.TestCase):
    """AgentStrategy parses a YAML plan and executes it."""

    def _make_coder(self):
        coder = MagicMock()
        coder.partial_response_content = "```yaml\nplan:\n  - step: 1\n```"
        coder.io = MagicMock()
        return coder

    def test_returns_true_on_success(self):
        strategy = AgentStrategy()
        coder = self._make_coder()
        coder.io.confirm_ask.return_value = True
        with patch("composez_core.agent_runner.AgentRunner") as MockRunner:
            runner = MockRunner.return_value
            runner.parse_plan.return_value = {"steps": []}
            result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_returns_true_on_empty_content(self):
        strategy = AgentStrategy()
        coder = self._make_coder()
        coder.partial_response_content = ""
        result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_returns_true_on_parse_failure(self):
        """Parse failure sets reflected_message but still returns True."""
        strategy = AgentStrategy()
        coder = self._make_coder()
        with patch("composez_core.agent_runner.AgentRunner") as MockRunner:
            runner = MockRunner.return_value
            runner.parse_plan.return_value = None
            result = strategy.reply_completed(coder)
        self.assertTrue(result)
        self.assertTrue(coder.reflected_message)

    def test_returns_true_on_decline(self):
        strategy = AgentStrategy()
        coder = self._make_coder()
        coder.io.confirm_ask.return_value = False
        with patch("composez_core.agent_runner.AgentRunner") as MockRunner:
            runner = MockRunner.return_value
            runner.parse_plan.return_value = {"steps": []}
            result = strategy.reply_completed(coder)
        self.assertTrue(result)

    def test_executes_plan_on_confirm(self):
        strategy = AgentStrategy()
        coder = self._make_coder()
        coder.io.confirm_ask.return_value = True
        with patch("composez_core.agent_runner.AgentRunner") as MockRunner:
            runner = MockRunner.return_value
            runner.parse_plan.return_value = {"steps": []}
            strategy.reply_completed(coder)
            runner.execute.assert_called_once()

    def test_does_not_execute_on_decline(self):
        strategy = AgentStrategy()
        coder = self._make_coder()
        coder.io.confirm_ask.return_value = False
        with patch("composez_core.agent_runner.AgentRunner") as MockRunner:
            runner = MockRunner.return_value
            runner.parse_plan.return_value = {"steps": []}
            strategy.reply_completed(coder)
            runner.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Coder.create() integration — edit mode × autonomy
# ---------------------------------------------------------------------------


class TestCoderCreateAutonomy(unittest.TestCase):
    """Coder.create() attaches the right strategy and prompts for each combo."""

    @patch("aider.coders.base_coder.Coder.__init__", return_value=None)
    def _create(self, edit_format, autonomy, mock_init):
        """Helper: call Coder.create() with mocked internals."""
        from aider.coders.base_coder import Coder
        from aider.models import Model

        mock_model = MagicMock(spec=Model)
        mock_model.edit_format = "diff"
        mock_model.editor_model = MagicMock()
        mock_model.editor_edit_format = "editor-diff"
        mock_model.weak_model = mock_model
        mock_model.name = "test-model"
        mock_model.info = {}

        mock_io = MagicMock()

        with patch("composez_core.novel_coder.activate_novel_mode"):
            with patch("composez_core.novel_coder.activate_novel_query_mode"):
                with patch("composez_core.novel_coder.activate_novel_agent_mode"):
                    coder = Coder.create(
                        main_model=mock_model,
                        edit_format=edit_format,
                        io=mock_io,
                        autonomy=autonomy,
                    )
        return coder

    # --- Strategy attachment ---

    def test_query_direct_gets_direct_strategy(self):
        coder = self._create("query", "direct")
        self.assertEqual(coder.autonomy_strategy.name, "direct")

    def test_query_compose_gets_compose_strategy(self):
        coder = self._create("query", "compose")
        self.assertEqual(coder.autonomy_strategy.name, "compose")

    def test_query_agent_gets_agent_strategy(self):
        coder = self._create("query", "agent")
        self.assertEqual(coder.autonomy_strategy.name, "agent")

    def test_diff_direct_gets_direct_strategy(self):
        coder = self._create("diff", "direct")
        self.assertEqual(coder.autonomy_strategy.name, "direct")

    def test_diff_compose_gets_compose_strategy(self):
        coder = self._create("diff", "compose")
        self.assertEqual(coder.autonomy_strategy.name, "compose")

    def test_selection_direct_gets_direct_strategy(self):
        coder = self._create("selection", "direct")
        self.assertEqual(coder.autonomy_strategy.name, "direct")

    def test_selection_compose_gets_compose_strategy(self):
        coder = self._create("selection", "compose")
        self.assertEqual(coder.autonomy_strategy.name, "compose")

    # --- Prompt overrides ---

    def test_compose_sets_architect_prompts(self):
        from aider.coders.architect_prompts import ArchitectPrompts

        coder = self._create("diff", "compose")
        self.assertIsInstance(coder.gpt_prompts, ArchitectPrompts)

    def test_agent_sets_agent_prompts(self):
        from aider.coders.agent_prompts import AgentPrompts

        coder = self._create("query", "agent")
        self.assertIsInstance(coder.gpt_prompts, AgentPrompts)

    def test_direct_keeps_native_prompts(self):
        from aider.coders.architect_prompts import ArchitectPrompts
        from aider.coders.agent_prompts import AgentPrompts

        coder = self._create("diff", "direct")
        self.assertNotIsInstance(coder.gpt_prompts, ArchitectPrompts)
        self.assertNotIsInstance(coder.gpt_prompts, AgentPrompts)

    # --- Coder class selection ---

    def test_query_mode_creates_query_coder(self):
        from aider.coders.query_coder import QueryCoder

        coder = self._create("query", "direct")
        self.assertIsInstance(coder, QueryCoder)

    def test_diff_mode_creates_editblock_coder(self):
        from aider.coders.editblock_coder import EditBlockCoder

        coder = self._create("diff", "direct")
        self.assertIsInstance(coder, EditBlockCoder)

    def test_selection_mode_creates_selection_coder(self):
        from aider.coders.selection_coder import SelectionCoder

        coder = self._create("selection", "direct")
        self.assertIsInstance(coder, SelectionCoder)

    def test_compose_with_diff_creates_editblock_coder(self):
        """Compose wraps the edit format — coder class matches edit format."""
        from aider.coders.editblock_coder import EditBlockCoder

        coder = self._create("diff", "compose")
        self.assertIsInstance(coder, EditBlockCoder)

    def test_agent_with_selection_creates_selection_coder(self):
        """Agent inherits edit format — coder class matches."""
        from aider.coders.selection_coder import SelectionCoder

        coder = self._create("selection", "agent")
        self.assertIsInstance(coder, SelectionCoder)

    # --- Legacy edit_format resolution ---

    def test_legacy_architect_maps_to_compose(self):
        coder = self._create("architect", None)
        self.assertEqual(coder.autonomy_strategy.name, "compose")

    def test_legacy_agent_maps_to_agent(self):
        coder = self._create("agent", None)
        self.assertEqual(coder.autonomy_strategy.name, "agent")


# ---------------------------------------------------------------------------
# reply_completed delegation
# ---------------------------------------------------------------------------


class TestReplyCompletedDelegation(unittest.TestCase):
    """Base coder delegates reply_completed to the autonomy strategy."""

    def test_delegates_to_strategy(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        mock_strategy = MagicMock()
        mock_strategy.reply_completed.return_value = True
        coder.autonomy_strategy = mock_strategy

        result = coder.reply_completed()

        mock_strategy.reply_completed.assert_called_once_with(coder)
        self.assertTrue(result)

    def test_no_strategy_returns_none(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        # No autonomy_strategy attribute
        result = coder.reply_completed()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
