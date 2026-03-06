"""Tests for the agent mode: plan parsing, execution, and dynamic review loop."""

import unittest
from unittest.mock import MagicMock, patch

from composez_core.agent_runner import (
    MAX_ASK_USER_RETRIES,
    MAX_REVIEW_ITERATIONS,
    AgentRunner,
    PlanStep,
    _step_failed,
    _truncate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stub_coder():
    """Build a minimal coder-like object for unit tests."""
    from aider.coders.base_coder import Coder

    coder = Coder.__new__(Coder)
    coder.io = MagicMock(spec=[
        "tool_output", "tool_error", "tool_warning",
        "user_input", "confirm_ask", "prompt_ask",
    ])
    coder.io.tool_output = MagicMock()
    coder.io.tool_error = MagicMock()
    coder.io.tool_warning = MagicMock()
    coder.io.user_input = MagicMock(return_value="user answer")
    coder.io.prompt_ask = MagicMock(return_value="user answer")
    coder.io.confirm_ask = MagicMock(return_value=True)
    coder.commands = MagicMock()
    coder.commands.run = MagicMock(return_value=None)
    coder.auto_commits = True
    coder.main_model = MagicMock()
    coder.main_model.weak_model = MagicMock()
    return coder


def _make_review_coder(responses):
    """Return a mock coder that returns successive review responses."""
    coder = MagicMock()
    coder.run = MagicMock(side_effect=list(responses))
    return coder


SAMPLE_PLAN_YAML = """\
Here's my plan:

```yaml
plan:
  - step: 1
    description: "Analyze the story settings"
    command: "/query What locations are established?"

  - step: 2
    description: "Ask user about tone"
    ask_user: "Should the caravan feel hopeful or foreboding?"

  - step: 3
    description: "Create new scenes"
    parallel:
      - command: "/new scene Caravan Morning"
      - command: "/new scene Caravan Evening"

  - step: 4
    description: "Write the scenes"
    depends_on: [2, 3]
    parallel:
      - command: "/write act 1 chapter 2 scene 3"
      - command: "/write act 1 chapter 2 scene 4"

  - step: 5
    description: "Commit"
    command: "/git add -A && git commit -m 'Add caravan scenes'"
```
"""

MULTI_CMD_PLAN_YAML = """\
```yaml
plan:
  - step: 1
    description: "Analyze chapter 1"
    commands:
      - "/add act 1 chapter 1"
      - "/query What are the settings?"

  - step: 2
    description: "Write scenes in parallel"
    parallel:
      - commands:
          - "/add act 1 chapter 1"
          - "/write act 1 chapter 1 scene 1"
      - commands:
          - "/add act 1 chapter 2"
          - "/write act 1 chapter 2 scene 1"

  - step: 3
    description: "Commit"
    command: "/git commit -m 'Update scenes'"
```
"""


# ---------------------------------------------------------------------------
# Plan parsing tests
# ---------------------------------------------------------------------------

class TestPlanParsing(unittest.TestCase):
    """Test YAML plan extraction and parsing."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_parse_valid_plan(self):
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        self.assertIsNotNone(steps)
        self.assertEqual(len(steps), 5)

    def test_step_numbers(self):
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        numbers = [s.number for s in steps]
        self.assertEqual(numbers, [1, 2, 3, 4, 5])

    def test_command_step_normalised_to_commands(self):
        """A single `command:` field is normalised to `commands` list."""
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        step1 = steps[0]
        self.assertEqual(
            step1.commands, ["/query What locations are established?"]
        )
        self.assertFalse(step1.parallel)
        self.assertIsNone(step1.ask_user)

    def test_ask_user_step(self):
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        step2 = steps[1]
        self.assertFalse(step2.commands)
        self.assertFalse(step2.parallel)
        self.assertEqual(
            step2.ask_user,
            "Should the caravan feel hopeful or foreboding?",
        )

    def test_parallel_step(self):
        """Parallel entries are normalised to list-of-lists (scripts)."""
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        step3 = steps[2]
        self.assertFalse(step3.commands)
        self.assertEqual(len(step3.parallel), 2)
        self.assertIn(["/new scene Caravan Morning"], step3.parallel)
        self.assertIn(["/new scene Caravan Evening"], step3.parallel)

    def test_depends_on_silently_ignored(self):
        """depends_on in YAML is accepted but not stored on PlanStep."""
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        step4 = steps[3]
        self.assertFalse(hasattr(step4, "depends_on"))

    def test_no_yaml_returns_none(self):
        result = self.runner.parse_plan("Just some regular text, no plan here.")
        self.assertIsNone(result)
        self.coder.io.tool_error.assert_called()

    def test_invalid_yaml_returns_none(self):
        bad_yaml = "```yaml\n: this is not: valid: yaml: [\n```"
        result = self.runner.parse_plan(bad_yaml)
        self.assertIsNone(result)

    def test_no_plan_key_returns_none(self):
        no_plan = "```yaml\nsteps:\n  - do: something\n```"
        result = self.runner.parse_plan(no_plan)
        self.assertIsNone(result)

    def test_empty_plan_returns_none(self):
        empty = "```yaml\nplan: []\n```"
        result = self.runner.parse_plan(empty)
        self.assertIsNone(result)

    def test_step_missing_number_returns_none(self):
        bad_step = (
            "```yaml\nplan:\n  - description: no number\n"
            "    command: /query hi\n```"
        )
        result = self.runner.parse_plan(bad_step)
        self.assertIsNone(result)

    def test_step_no_action_returns_none(self):
        bad = "```yaml\nplan:\n  - step: 1\n    description: no action\n```"
        result = self.runner.parse_plan(bad)
        self.assertIsNone(result)

    def test_step_multiple_actions_returns_none(self):
        bad = (
            "```yaml\nplan:\n  - step: 1\n    command: /query hi\n"
            "    ask_user: what?\n```"
        )
        result = self.runner.parse_plan(bad)
        self.assertIsNone(result)

    def test_parallel_string_entries(self):
        """Parallel entries can be plain strings (single-command scripts)."""
        yaml_text = (
            "```yaml\nplan:\n  - step: 1\n    description: test\n"
            "    parallel:\n      - /write scene 1\n      - /write scene 2\n```"
        )
        steps = self.runner.parse_plan(yaml_text)
        self.assertIsNotNone(steps)
        self.assertEqual(
            steps[0].parallel, [["/write scene 1"], ["/write scene 2"]]
        )

    def test_unfenced_yaml(self):
        """Plans without ``` fences should still parse."""
        unfenced = (
            "plan:\n"
            "  - step: 1\n"
            "    description: test\n"
            "    command: /query hi\n"
        )
        steps = self.runner.parse_plan(unfenced)
        self.assertIsNotNone(steps)
        self.assertEqual(len(steps), 1)

    def test_multi_command_step(self):
        """Steps with `commands:` (list) parse correctly."""
        steps = self.runner.parse_plan(MULTI_CMD_PLAN_YAML)
        self.assertIsNotNone(steps)
        step1 = steps[0]
        self.assertEqual(len(step1.commands), 2)
        self.assertEqual(step1.commands[0], "/add act 1 chapter 1")
        self.assertEqual(step1.commands[1], "/query What are the settings?")

    def test_parallel_multi_command_scripts(self):
        """Parallel entries with `commands:` list parse as scripts."""
        steps = self.runner.parse_plan(MULTI_CMD_PLAN_YAML)
        step2 = steps[1]
        self.assertEqual(len(step2.parallel), 2)
        self.assertEqual(step2.parallel[0],
                         ["/add act 1 chapter 1",
                          "/write act 1 chapter 1 scene 1"])
        self.assertEqual(step2.parallel[1],
                         ["/add act 1 chapter 2",
                          "/write act 1 chapter 2 scene 1"])


# ---------------------------------------------------------------------------
# PlanStep backward compatibility
# ---------------------------------------------------------------------------

class TestPlanStepCompat(unittest.TestCase):
    """Test that the PlanStep constructor accepts both old and new formats."""

    def test_command_kwarg(self):
        step = PlanStep(1, "test", command="/query hello")
        self.assertEqual(step.commands, ["/query hello"])

    def test_commands_kwarg(self):
        step = PlanStep(1, "test", commands=["/add x", "/query y"])
        self.assertEqual(step.commands, ["/add x", "/query y"])

    def test_parallel_strings_normalised(self):
        step = PlanStep(1, "test", parallel=["/write 1", "/write 2"])
        self.assertEqual(step.parallel, [["/write 1"], ["/write 2"]])

    def test_parallel_lists_kept(self):
        step = PlanStep(
            1, "test",
            parallel=[["/add x", "/write 1"], ["/add y", "/write 2"]],
        )
        self.assertEqual(step.parallel[0], ["/add x", "/write 1"])
        self.assertEqual(step.parallel[1], ["/add y", "/write 2"])


# ---------------------------------------------------------------------------
# Plan display tests
# ---------------------------------------------------------------------------

class TestPlanDisplay(unittest.TestCase):
    """Test plan pretty-printing."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_show_plan_calls_tool_output(self):
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)
        self.runner.show_plan(steps)
        self.assertTrue(self.coder.io.tool_output.call_count >= 5)


# ---------------------------------------------------------------------------
# _should_review tests
# ---------------------------------------------------------------------------

class TestShouldReview(unittest.TestCase):
    """Test the heuristic for when to trigger orchestrator review."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_ask_user_step_not_reviewed(self):
        """ask_user steps are handled in _execute_loop, not via _should_review."""
        step = PlanStep(1, "question", ask_user="What color?")
        step.result = "blue"
        self.assertFalse(self.runner._should_review(step))

    def test_query_command_reviewed(self):
        step = PlanStep(1, "analysis", command="/query What is the theme?")
        self.assertTrue(self.runner._should_review(step))

    def test_write_command_reviewed(self):
        step = PlanStep(1, "write", command="/write act 1 chapter 1 scene 1")
        self.assertTrue(self.runner._should_review(step))

    def test_summarize_command_reviewed(self):
        step = PlanStep(1, "summarize", command="/summarize act 1")
        self.assertTrue(self.runner._should_review(step))

    def test_edit_command_reviewed(self):
        step = PlanStep(1, "edit", command="/edit Fix the dialogue")
        self.assertTrue(self.runner._should_review(step))

    def test_add_command_not_reviewed(self):
        step = PlanStep(1, "add files", command="/add act 1")
        self.assertFalse(self.runner._should_review(step))

    def test_git_command_not_reviewed(self):
        step = PlanStep(1, "commit", command="/git commit -m 'save'")
        self.assertFalse(self.runner._should_review(step))

    def test_new_command_not_reviewed(self):
        step = PlanStep(1, "create", command="/new scene Intro")
        self.assertFalse(self.runner._should_review(step))

    def test_error_result_reviewed(self):
        step = PlanStep(1, "commit", command="/git commit")
        step.result = "ERROR: something broke"
        self.assertTrue(self.runner._should_review(step))

    def test_multi_command_script_reviewed_if_any_reviewable(self):
        step = PlanStep(
            1, "add+query",
            commands=["/add act 1", "/query What is the theme?"],
        )
        self.assertTrue(self.runner._should_review(step))

    def test_multi_command_script_not_reviewed_if_none_reviewable(self):
        step = PlanStep(
            1, "add+drop",
            commands=["/add act 1", "/drop act 2"],
        )
        self.assertFalse(self.runner._should_review(step))

    def test_parallel_with_write_reviewed(self):
        step = PlanStep(
            1, "write scenes",
            parallel=[["/add x", "/write scene 1"],
                      ["/add y", "/write scene 2"]],
        )
        self.assertTrue(self.runner._should_review(step))

    def test_parallel_without_review_commands(self):
        step = PlanStep(
            1, "create scenes",
            parallel=[["/new scene A"], ["/new scene B"]],
        )
        self.assertFalse(self.runner._should_review(step))


# ---------------------------------------------------------------------------
# _parse_review_response tests
# ---------------------------------------------------------------------------

class TestParseReviewResponse(unittest.TestCase):
    """Test parsing of orchestrator review responses."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_fenced_continue(self):
        response = "Looks good.\n```yaml\naction: continue\n```"
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "continue")

    def test_fenced_done(self):
        response = '```yaml\naction: done\nsummary: "All finished"\n```'
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "done")
        self.assertEqual(action["summary"], "All finished")

    def test_fenced_ask_user(self):
        response = '```yaml\naction: ask_user\nquestion: "Which tone?"\n```'
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "ask_user")
        self.assertEqual(action["question"], "Which tone?")

    def test_fenced_revise(self):
        response = (
            "```yaml\n"
            "action: revise\n"
            "plan:\n"
            "  - step: 5\n"
            "    description: revised step\n"
            "    command: /query new question\n"
            "```"
        )
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "revise")
        self.assertEqual(len(action["steps"]), 1)
        # LLM number 5 gets renumbered to 1 (no completed steps)
        self.assertEqual(action["steps"][0].number, 1)

    def test_unfenced_action(self):
        response = "I think we should continue.\naction: continue"
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "continue")

    def test_no_keyword_fallback(self):
        """Bare text containing 'continue' should NOT be parsed as an action."""
        response = "Everything looks good, let's continue."
        action = self.runner._parse_review_response(response)
        self.assertIsNone(action)

    def test_empty_response(self):
        self.assertIsNone(self.runner._parse_review_response(""))
        self.assertIsNone(self.runner._parse_review_response(None))

    def test_ask_user_no_question_defaults_to_continue(self):
        response = "```yaml\naction: ask_user\n```"
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "continue")

    def test_revise_empty_plan_defaults_to_continue(self):
        response = "```yaml\naction: revise\nplan: []\n```"
        action = self.runner._parse_review_response(response)
        self.assertEqual(action["type"], "continue")

    def test_unknown_action_returns_none(self):
        response = "```yaml\naction: explode\n```"
        action = self.runner._parse_review_response(response)
        self.assertIsNone(action)


# ---------------------------------------------------------------------------
# _build_review_prompt tests
# ---------------------------------------------------------------------------

class TestBuildReviewPrompt(unittest.TestCase):
    """Test review prompt construction."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_contains_latest_result(self):
        step = PlanStep(1, "analysis", command="/query What?")
        step.result = "Some analysis output"
        self.runner._completed_steps[1] = step
        prompt = self.runner._build_review_prompt(step, [])
        self.assertIn("Some analysis output", prompt)
        self.assertIn("Latest Step Result", prompt)

    def test_contains_remaining_plan(self):
        step = PlanStep(1, "analysis", command="/query What?")
        step.result = "done"
        remaining = [
            PlanStep(2, "next step", command="/write scene 1"),
            PlanStep(3, "final", command="/git commit"),
        ]
        prompt = self.runner._build_review_prompt(step, remaining)
        self.assertIn("Remaining Plan", prompt)
        self.assertIn("/write scene 1", prompt)
        self.assertIn("/git commit", prompt)

    def test_contains_instructions(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = None
        prompt = self.runner._build_review_prompt(step, [])
        self.assertIn("action: continue", prompt)
        self.assertIn("action: revise", prompt)
        self.assertIn("action: ask_user", prompt)
        self.assertIn("action: done", prompt)

    def test_no_remaining_steps(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = None
        prompt = self.runner._build_review_prompt(step, [])
        self.assertIn("No remaining steps", prompt)

    def test_truncates_long_results(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = "x" * 5000
        self.runner._completed_steps[1] = step
        prompt = self.runner._build_review_prompt(step, [])
        self.assertIn("...", prompt)
        self.assertNotIn("x" * 5000, prompt)


# ---------------------------------------------------------------------------
# Execution tests
# ---------------------------------------------------------------------------

class TestExecution(unittest.TestCase):
    """Test plan execution logic."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_execute_script_step(self):
        """Single-command script runs in a subprocess with /save ctx appended."""
        step = PlanStep(1, "test", command="/query hello")
        with patch.object(
            self.runner, "_run_subprocess", return_value="Analysis result"
        ) as mock_sub:
            self.runner._execute_script(step)

        script = mock_sub.call_args[0][0]
        self.assertEqual(script[0], "/query hello")
        # Last command is the auto-save context
        self.assertTrue(script[-1].startswith("/save ctx agents/"))
        self.assertIn("_after_context_", script[-1])
        self.assertEqual(step.result, "Analysis result")

    def test_execute_multi_command_script(self):
        """Multi-command script sends all commands to one subprocess."""
        step = PlanStep(
            1, "test",
            commands=["/add act 1", "/query What settings?"],
        )
        with patch.object(
            self.runner, "_run_subprocess", return_value="Settings found"
        ) as mock_sub:
            self.runner._execute_script(step)

        script = mock_sub.call_args[0][0]
        self.assertEqual(script[0], "/add act 1")
        self.assertEqual(script[1], "/query What settings?")
        self.assertTrue(script[-1].startswith("/save ctx agents/"))
        self.assertEqual(step.result, "Settings found")

    def test_execute_ask_user_step(self):
        step = PlanStep(2, "question", ask_user="What color?")
        self.runner._execute_ask_user(step)
        self.coder.io.prompt_ask.assert_called_with("What color?")
        self.assertEqual(self.runner.user_answers[2], "user answer")
        self.assertEqual(step.result, "user answer")

    def test_execute_parallel_step(self):
        """Parallel scripts run in subprocesses."""
        step = PlanStep(
            3, "parallel test",
            parallel=[["/write 1"], ["/write 2"]],
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Scene written successfully."
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            self.runner._execute_parallel(step)

        # A subprocess was launched for each parallel script
        self.assertEqual(mock_run.call_count, 2)
        # Results collected
        self.assertEqual(len(step.result), 2)
        for r in step.result:
            self.assertEqual(r, "Scene written successfully.")

    def test_interpolate_answer(self):
        self.runner.user_answers[2] = "foreboding"
        result = self.runner._interpolate("/edit Make the tone {answer:2}")
        self.assertEqual(result, "/edit Make the tone foreboding")

    def test_interpolate_missing_answer(self):
        result = self.runner._interpolate("{answer:99}")
        self.assertEqual(result, "(no answer for step 99)")

    def test_execute_full_plan(self):
        """Full plan executes with review coder returning 'continue'."""
        steps = self.runner.parse_plan(SAMPLE_PLAN_YAML)

        mock_review = _make_review_coder(
            ["```yaml\naction: continue\n```"] * 10
        )

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Done."
        mock_proc.stderr = ""

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_proc,
        ):
            self.runner.execute(steps)

        # Verify plan completed
        output_strs = [
            str(c) for c in self.coder.io.tool_output.call_args_list
        ]
        self.assertTrue(any("Plan complete" in s for s in output_strs))

    def test_auto_commits_disabled_during_execution(self):
        """Auto-commits are disabled during plan execution and restored after."""
        self.coder.auto_commits = True

        steps = [PlanStep(1, "test", command="/git status")]

        with patch.object(
            self.runner, "_run_subprocess", return_value=None
        ):
            self.runner.execute(steps)

        # Restored after execution
        self.assertTrue(self.coder.auto_commits)

    def test_auto_commits_disabled_during_execution_checked(self):
        """Auto-commits is False while steps are running."""
        self.coder.auto_commits = True
        observed = {}

        def capture_auto_commits(script, **kwargs):
            observed["during"] = self.coder.auto_commits
            return None

        with patch.object(
            self.runner, "_run_subprocess", side_effect=capture_auto_commits
        ):
            self.runner.execute([PlanStep(1, "test", command="/git status")])

        self.assertFalse(observed["during"])
        self.assertTrue(self.coder.auto_commits)

    def test_bare_text_becomes_ask(self):
        """A command without / prefix is wrapped in /query for subprocess."""
        step = PlanStep(1, "test", command="What is the weather?")
        with patch.object(
            self.runner, "_run_subprocess", return_value=None
        ) as mock_sub:
            self.runner._execute_script(step)

        script = mock_sub.call_args[0][0]
        self.assertEqual(script[0], "/query What is the weather?")
        self.assertTrue(script[-1].startswith("/save ctx agents/"))
        # Should also emit a warning about bare text
        self.coder.io.tool_warning.assert_called()

    def test_serial_step_does_not_mutate_orchestrator(self):
        """Serial steps run in subprocess — orchestrator coder is unchanged."""
        original_coder = self.runner.coder
        original_commands = self.runner.commands

        step = PlanStep(1, "test", command="/write scene 1")
        with patch.object(
            self.runner, "_run_subprocess", return_value="Written"
        ):
            self.runner._execute_script(step)

        self.assertIs(self.runner.coder, original_coder)
        self.assertIs(self.runner.commands, original_commands)

    def test_failed_subprocess_reports_error(self):
        """Subprocess error string is stored in step.result."""
        step = PlanStep(1, "test", command="/git test")
        with patch.object(
            self.runner, "_run_subprocess",
            return_value="ERROR: subprocess exited with code 1"
        ):
            self.runner._execute_script(step)

        self.assertTrue(step.result.startswith("ERROR:"))

    def test_failed_step_asks_to_continue(self):
        """If a step fails and review is unavailable, ask user to continue."""
        self.coder.io.confirm_ask.return_value = False

        steps = [
            PlanStep(1, "will fail", command="/git test"),
            PlanStep(2, "should not run", command="/git test2"),
        ]

        call_count = [0]
        def failing_subprocess(script, **kwargs):
            call_count[0] += 1
            return "ERROR: subprocess exited with code 1"

        with patch.object(
            self.runner, "_run_subprocess", side_effect=failing_subprocess
        ), patch.object(
            self.runner, "_get_review_coder", return_value=None
        ):
            self.runner.execute(steps)

        self.assertEqual(call_count[0], 1)


# ---------------------------------------------------------------------------
# Subprocess execution tests
# ---------------------------------------------------------------------------

class TestSubprocessExecution(unittest.TestCase):
    """Test subprocess-based execution (used by both serial and parallel steps)."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.coder.main_model = MagicMock()
        self.coder.main_model.name = "gpt-4"
        self.coder.root = "/tmp/test-repo"
        self.runner = AgentRunner(self.coder)

    def test_build_subprocess_args(self):
        """Subprocess args include required flags and model."""
        args = self.runner._build_subprocess_args()
        self.assertIn("--yes-always", args)
        self.assertIn("--no-auto-commits", args)
        self.assertIn("--no-auto-lint", args)
        self.assertIn("--auto-context", args)
        self.assertIn("--no-fancy-input", args)
        self.assertIn("--no-stream", args)
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], "gpt-4")

    def test_build_subprocess_args_no_model(self):
        """Subprocess args work when no model is set."""
        del self.coder.main_model
        args = self.runner._build_subprocess_args()
        self.assertNotIn("--model", args)

    def test_get_repo_root(self):
        self.assertEqual(self.runner._get_repo_root(), "/tmp/test-repo")

    def test_get_repo_root_fallback(self):
        del self.coder.root
        import os
        self.assertEqual(self.runner._get_repo_root(), os.getcwd())

    def test_run_subprocess_success(self):
        """Successful subprocess returns stdout."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Scene written.\nAll done."
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            result = self.runner._run_subprocess(["/add x", "/write y"])

        self.assertEqual(result, "Scene written.\nAll done.")
        # Verify the input was piped correctly
        call_kwargs = mock_run.call_args
        self.assertEqual(call_kwargs.kwargs["input"], "/add x\n/write y\n")
        self.assertEqual(call_kwargs.kwargs["cwd"], "/tmp/test-repo")

    def test_run_subprocess_nonzero_exit(self):
        """Non-zero exit code returns ERROR."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "output"
        mock_result.stderr = "something broke"

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ):
            result = self.runner._run_subprocess(["/write x"])

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("code 1", result)

    def test_run_subprocess_timeout(self):
        """Timeout returns ERROR."""
        import subprocess as sp

        with patch(
            "composez_core.agent_runner.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="aider", timeout=600),
        ):
            result = self.runner._run_subprocess(["/write x"])

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("timed out", result)

    def test_run_subprocess_exception(self):
        """General exception returns ERROR."""
        with patch(
            "composez_core.agent_runner.subprocess.run",
            side_effect=OSError("no such file"),
        ):
            result = self.runner._run_subprocess(["/write x"])

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("no such file", result)

    def test_parallel_pipes_correct_scripts(self):
        """Each parallel script is piped as separate subprocess input."""
        step = PlanStep(
            1, "write scenes",
            parallel=[["/add ch1", "/write sc1"], ["/add ch2", "/write sc2"]],
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Done"
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            self.runner._execute_parallel(step)

        self.assertEqual(mock_run.call_count, 2)
        inputs = sorted(
            c.kwargs["input"] for c in mock_run.call_args_list
        )
        # Each script should contain the user commands plus auto-save
        self.assertTrue(any("/add ch1\n/write sc1\n" in i for i in inputs))
        self.assertTrue(any("/add ch2\n/write sc2\n" in i for i in inputs))
        # Auto-save should be appended to each
        for i in inputs:
            self.assertIn("/save ctx agents/", i)

    def test_parallel_collects_subprocess_errors(self):
        """Subprocess failures in parallel are captured per-script."""
        step = PlanStep(
            1, "mixed",
            parallel=[["/write ok"], ["/write fail"]],
        )

        results = [
            MagicMock(returncode=0, stdout="Success", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="boom"),
        ]

        with patch(
            "composez_core.agent_runner.subprocess.run",
            side_effect=results,
        ):
            self.runner._execute_parallel(step)

        # One success, one error — order may vary due to threads
        result_strs = [str(r) for r in step.result]
        self.assertTrue(
            any("Success" in r for r in result_strs)
        )
        self.assertTrue(
            any("ERROR:" in str(r) for r in result_strs)
        )

    def test_serial_step_uses_subprocess(self):
        """Sequential steps also run in subprocesses for isolation."""
        step = PlanStep(1, "analyze", command="/query What?")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Analysis output"
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            self.runner._execute_script(step)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        input_text = call_kwargs.kwargs["input"]
        # Should start with the user command and end with auto-save
        self.assertTrue(input_text.startswith("/query What?\n"))
        self.assertIn("/save ctx agents/", input_text)
        self.assertEqual(step.result, "Analysis output")

    def test_serial_multi_command_subprocess(self):
        """Multi-command serial scripts pipe all commands to subprocess."""
        step = PlanStep(
            1, "setup+analyze",
            commands=["/add act 1", "/query What?", "/save ctx analysis"],
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Context saved"
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            self.runner._execute_script(step)

        call_kwargs = mock_run.call_args
        input_text = call_kwargs.kwargs["input"]
        # User commands should be present
        self.assertIn("/add act 1\n", input_text)
        self.assertIn("/query What?\n", input_text)
        self.assertIn("/save ctx analysis\n", input_text)
        # Auto-save should be appended at the end
        lines = input_text.strip().split("\n")
        self.assertTrue(lines[-1].startswith("/save ctx agents/"))


# ---------------------------------------------------------------------------
# Dynamic review loop tests
# ---------------------------------------------------------------------------

class TestReviewLoop(unittest.TestCase):
    """Test the dynamic review loop."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_review_continue(self):
        """Orchestrator says continue — execution proceeds normally."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "commit", command="/git commit"),
        ]

        mock_review = _make_review_coder(
            ["```yaml\naction: continue\n```"]
        )
        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        self.assertEqual(sub_calls[0], 2)
        self.assertEqual(mock_review.run.call_count, 1)

    def test_review_done_stops_execution(self):
        """Orchestrator says done — execution stops early."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "should not run", command="/write scene 1"),
        ]

        mock_review = _make_review_coder(
            ['```yaml\naction: done\nsummary: "Analysis complete"\n```']
        )
        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Analysis output"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        # Only the first step should have run
        self.assertEqual(sub_calls[0], 1)
        output_calls = [
            str(c) for c in self.coder.io.tool_output.call_args_list
        ]
        self.assertTrue(
            any("Analysis complete" in c for c in output_calls)
        )

    def test_review_revise_replaces_remaining(self):
        """Orchestrator revises the plan — remaining steps are replaced."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "old step", command="/write scene 1"),
            PlanStep(3, "old step 2", command="/write scene 2"),
        ]

        revised_yaml = (
            "```yaml\n"
            "action: revise\n"
            "plan:\n"
            "  - step: 4\n"
            "    description: new step\n"
            "    command: /git commit\n"
            "```"
        )
        mock_review = _make_review_coder([revised_yaml])
        sub_scripts = []
        def record_subprocess(script, **kw):
            sub_scripts.append(script)
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=record_subprocess
        ):
            self.runner.execute(steps)

        self.assertEqual(len(sub_scripts), 2)
        # First script: /query What? + auto-save
        self.assertEqual(sub_scripts[0][0], "/query What?")
        self.assertTrue(sub_scripts[0][-1].startswith("/save ctx agents/"))
        # Second script: /git commit + auto-save
        self.assertEqual(sub_scripts[1][0], "/git commit")
        self.assertTrue(sub_scripts[1][-1].startswith("/save ctx agents/"))

    def test_review_ask_user_bubbles_question(self):
        """Orchestrator asks user a question, then continues."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "commit", command="/git commit"),
        ]

        mock_review = _make_review_coder([
            '```yaml\naction: ask_user\nquestion: "Which style?"\n```',
            "```yaml\naction: continue\n```",
        ])
        self.coder.io.prompt_ask.return_value = "formal style"

        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        prompt_ask_calls = self.coder.io.prompt_ask.call_args_list
        self.assertTrue(
            any("Which style?" in str(c) for c in prompt_ask_calls)
        )
        self.assertEqual(sub_calls[0], 2)

    def test_review_failure_defaults_to_continue(self):
        """If the review coder fails, execution continues as planned."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "commit", command="/git commit"),
        ]

        mock_review = MagicMock()
        mock_review.run.side_effect = RuntimeError("LLM error")

        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        self.assertEqual(sub_calls[0], 2)

    def test_review_coder_unavailable_defaults_to_continue(self):
        """If review coder can't be created, execution continues."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "commit", command="/git commit"),
        ]

        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=None
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        self.assertEqual(sub_calls[0], 2)

    def test_non_reviewed_steps_skip_review(self):
        """Steps like /add and /git don't trigger review."""
        steps = [
            PlanStep(1, "add files", command="/add scene 1"),
            PlanStep(2, "create", command="/new scene Intro"),
            PlanStep(3, "commit", command="/git commit"),
        ]

        mock_review = _make_review_coder([])
        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute(steps)

        self.assertEqual(sub_calls[0], 3)
        self.assertEqual(mock_review.run.call_count, 0)

    def test_error_step_triggers_review(self):
        """A subprocess error triggers orchestrator review."""
        steps = [
            PlanStep(1, "will fail", command="/query broken"),
            PlanStep(2, "should still run", command="/git commit"),
        ]

        sub_results = iter([
            "ERROR: subprocess exited with code 1",
            "Done",
        ])
        def sequential_subprocess(script, **kw):
            return next(sub_results)

        mock_review = _make_review_coder(
            ["```yaml\naction: continue\n```"]
        )
        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=sequential_subprocess
        ):
            self.runner.execute(steps)

        # Review was triggered for the error, then step 2 still ran
        self.assertEqual(mock_review.run.call_count, 1)

    def test_review_revise_replaces_remaining(self):
        """Revised plan replaces remaining steps and renumbers them."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "write", command="/write scene 1"),
        ]

        revised_yaml = (
            "```yaml\n"
            "action: revise\n"
            "plan:\n"
            "  - step: 99\n"
            "    description: write based on analysis\n"
            "    command: /write scene 2\n"
            "```"
        )
        mock_review = _make_review_coder([revised_yaml])
        sub_scripts = []
        def record_subprocess(script, **kw):
            sub_scripts.append(script)
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=record_subprocess
        ):
            self.runner.execute(steps)

        # Step 1 ran, then revision replaced step 2 with a new step
        self.assertEqual(len(sub_scripts), 2)

    def test_revision_renumbers_steps(self):
        """Revised steps are renumbered from the last completed step."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "write", command="/write scene 1"),
        ]

        revised_yaml = (
            "```yaml\n"
            "action: revise\n"
            "plan:\n"
            "  - step: 99\n"
            "    description: revised step A\n"
            "    command: /write scene 2\n"
            "  - step: 100\n"
            "    description: revised step B\n"
            "    command: /git add -A\n"
            "```"
        )
        mock_review = _make_review_coder([revised_yaml])
        executed_steps = []
        def record_subprocess(script, **kw):
            executed_steps.append(script)
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=record_subprocess
        ):
            self.runner.execute(steps)

        # Step 1 completed, revision should renumber to 2, 3
        self.assertIn(2, self.runner._completed_steps)
        self.assertIn(3, self.runner._completed_steps)
        self.assertEqual(
            self.runner._completed_steps[2].description, "revised step A"
        )

    def test_revision_caps_net_new_steps(self):
        """Revisions adding too many net-new steps are trimmed."""
        steps = [
            PlanStep(1, "analyze", command="/query What?"),
            PlanStep(2, "write", command="/write scene 1"),
        ]

        # Revision replaces 1 remaining step with 6 (net +5, over limit of 3)
        revised_yaml = (
            "```yaml\n"
            "action: revise\n"
            "plan:\n"
            + "".join(
                f"  - step: {i}\n"
                f"    description: step {i}\n"
                f"    command: /git status\n"
                for i in range(10, 16)
            )
            + "```"
        )
        mock_review = _make_review_coder([revised_yaml])
        executed = []
        def record_subprocess(script, **kw):
            executed.append(script)
            return "Done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ), patch.object(
            self.runner, "_run_subprocess", side_effect=record_subprocess
        ):
            self.runner.execute(steps)

        # Step 1 + at most (1 remaining + MAX_NET_NEW_STEPS=3) = 4 revised steps
        # Total: 1 original + 4 trimmed = 5
        self.assertLessEqual(len(executed), 5)


# ---------------------------------------------------------------------------
# _truncate tests
# ---------------------------------------------------------------------------

class TestTruncate(unittest.TestCase):
    def test_short_text(self):
        self.assertEqual(_truncate("hello", 10), "hello")

    def test_exact_length(self):
        self.assertEqual(_truncate("hello", 5), "hello")

    def test_long_text(self):
        result = _truncate("hello world", 5)
        self.assertEqual(result, "hello...")


# ---------------------------------------------------------------------------
# Agent autonomy strategy tests
# ---------------------------------------------------------------------------

class TestAgentAutonomyStrategy(unittest.TestCase):
    """Test that the agent autonomy strategy is properly registered."""

    def test_agent_strategy_exists(self):
        from aider.coders.autonomy import AgentStrategy, get_strategy

        strategy = get_strategy("agent")
        self.assertIsInstance(strategy, AgentStrategy)

    def test_agent_strategy_name(self):
        from aider.coders.autonomy import get_strategy

        strategy = get_strategy("agent")
        self.assertEqual(strategy.name, "agent")

    def test_all_autonomy_levels(self):
        from aider.coders.autonomy import AUTONOMY_LEVELS

        self.assertIn("direct", AUTONOMY_LEVELS)
        self.assertIn("compose", AUTONOMY_LEVELS)
        self.assertIn("agent", AUTONOMY_LEVELS)


class TestNovelAgentPrompts(unittest.TestCase):
    """Test the NovelAgentPrompts content."""

    def test_mentions_plan_format(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("plan", prompts.main_system.lower())
        self.assertIn("yaml", prompts.main_system.lower())

    def test_mentions_novel_structure(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("PROSE.md", prompts.main_system)
        self.assertIn("SUMMARY.md", prompts.main_system)

    def test_has_available_commands_placeholder(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("{available_commands}", prompts.main_system)

    def test_mentions_commands_format(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("commands", prompts.main_system)

    def test_mentions_sequential_context_flow(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("context", prompts.main_system.lower())
        self.assertIn("prior steps", prompts.main_system.lower())

    def test_mentions_subprocess_isolation(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("subprocess", prompts.main_system.lower())

    def test_generic_prompts_have_placeholder(self):
        from aider.coders.agent_prompts import AgentPrompts

        prompts = AgentPrompts()
        self.assertIn("{available_commands}", prompts.main_system)
        self.assertIn("subprocess", prompts.main_system.lower())


class TestActivateAgentMode(unittest.TestCase):
    """Test the activate_novel_agent_mode function."""

    def test_replaces_prompts(self):
        from aider.coders.base_coder import Coder
        from composez_core.novel_coder import activate_novel_agent_mode
        from composez_core.novel_prompts import NovelAgentPrompts

        coder = Coder.__new__(Coder)
        coder.gpt_prompts = MagicMock()
        activate_novel_agent_mode(coder)
        self.assertIsInstance(coder.gpt_prompts, NovelAgentPrompts)


# ---------------------------------------------------------------------------
# _step_failed tests
# ---------------------------------------------------------------------------

class TestStepFailed(unittest.TestCase):
    """Test the _step_failed helper function."""

    def test_none_result_not_failed(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = None
        self.assertFalse(_step_failed(step))

    def test_success_result_not_failed(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = "Analysis complete"
        self.assertFalse(_step_failed(step))

    def test_error_result_is_failed(self):
        step = PlanStep(1, "test", command="/query hi")
        step.result = "ERROR: subprocess exited with code 1"
        self.assertTrue(_step_failed(step))

    def test_parallel_all_success(self):
        step = PlanStep(1, "test", parallel=[["/write 1"], ["/write 2"]])
        step.result = ["Done", "Done"]
        self.assertFalse(_step_failed(step))

    def test_parallel_one_error(self):
        step = PlanStep(1, "test", parallel=[["/write 1"], ["/write 2"]])
        step.result = ["Done", "ERROR: failed"]
        self.assertTrue(_step_failed(step))


# ---------------------------------------------------------------------------
# Dependency result validation tests
# ---------------------------------------------------------------------------

class TestSequentialStepExecution(unittest.TestCase):
    """Test that steps execute sequentially and error stops the plan."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_failed_ask_user_stops_plan(self):
        """A failed ask_user step stops the entire plan — no further steps run."""
        # Simulate a failed ask_user by having io return empty answers
        self.coder.io.prompt_ask = MagicMock(return_value=None)

        ask_step = PlanStep(1, "ask user", ask_user="What?")
        next_step = PlanStep(2, "use answer", command="/git status")

        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute([ask_step, next_step])

        # Step 2 should not have run — plan stops on failed ask_user
        self.assertEqual(sub_calls[0], 0)
        # Step 2 should NOT be in completed_steps since plan stopped early
        self.assertNotIn(2, self.runner._completed_steps)
        # Step 1 should be in completed_steps with an error
        self.assertIn(1, self.runner._completed_steps)
        self.assertTrue(
            self.runner._completed_steps[1].result.startswith("ERROR:")
        )

    def test_successful_step_allows_next(self):
        """A step that succeeds allows the next step to run."""
        # Simulate a successful ask_user
        self.coder.io.user_input = MagicMock(return_value="user answer")

        ask_step = PlanStep(1, "ask user", ask_user="What?")
        next_step = PlanStep(2, "use answer", command="/git status")

        sub_calls = [0]
        def count_subprocess(script, **kw):
            sub_calls[0] += 1
            return "Done"

        with patch.object(
            self.runner, "_run_subprocess", side_effect=count_subprocess
        ):
            self.runner.execute([ask_step, next_step])

        # Step 2 should have run
        self.assertEqual(sub_calls[0], 1)


# ---------------------------------------------------------------------------
# ask_user empty answer handling tests
# ---------------------------------------------------------------------------

class TestAskUserEmptyAnswer(unittest.TestCase):
    """Test that ask_user handles empty/None answers correctly."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_empty_answer_retries(self):
        """Empty answers should be retried up to MAX_ASK_USER_RETRIES times."""
        self.coder.io.prompt_ask = MagicMock(
            side_effect=[None, "", "actual answer"]
        )
        step = PlanStep(1, "question", ask_user="What color?")
        self.runner._execute_ask_user(step)

        self.assertEqual(step.result, "actual answer")
        self.assertEqual(self.runner.user_answers[1], "actual answer")
        self.assertEqual(
            self.coder.io.prompt_ask.call_count,
            3,
        )

    def test_all_empty_answers_marks_error(self):
        """If all retries return empty, the step should be marked as ERROR."""
        self.coder.io.prompt_ask = MagicMock(return_value=None)
        step = PlanStep(1, "question", ask_user="What color?")
        self.runner._execute_ask_user(step)

        self.assertTrue(step.result.startswith("ERROR:"))
        self.assertNotIn(1, self.runner.user_answers)
        self.assertEqual(
            self.coder.io.prompt_ask.call_count,
            1 + MAX_ASK_USER_RETRIES,
        )

    def test_whitespace_only_answer_counts_as_empty(self):
        """Whitespace-only answers should be treated as empty."""
        self.coder.io.prompt_ask = MagicMock(
            side_effect=["   ", "\t\n", "real answer"]
        )
        step = PlanStep(1, "question", ask_user="What?")
        self.runner._execute_ask_user(step)

        self.assertEqual(step.result, "real answer")


# ---------------------------------------------------------------------------
# Review loop max iterations tests
# ---------------------------------------------------------------------------

class TestReviewLoopMaxIterations(unittest.TestCase):
    """Test that the review loop is bounded by MAX_REVIEW_ITERATIONS."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_review_loop_bounded(self):
        """Review loop with repeated ask_user should stop at max iterations."""
        # Review coder always asks user, user always answers
        ask_responses = [
            '```yaml\naction: ask_user\nquestion: "What?"\n```'
        ] * (MAX_REVIEW_ITERATIONS + 5)
        mock_review = _make_review_coder(ask_responses)
        self.coder.io.prompt_ask.return_value = "some answer"

        step = PlanStep(1, "test", command="/query What?")
        step.result = "some result"
        self.runner._completed_steps[1] = step

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ):
            action = self.runner._review_step(step, [])

        # Should have stopped at MAX_REVIEW_ITERATIONS
        self.assertEqual(action["type"], "continue")
        self.assertLessEqual(
            mock_review.run.call_count, MAX_REVIEW_ITERATIONS
        )

    def test_review_loop_exits_early_on_continue(self):
        """Review loop should exit immediately on a 'continue' action."""
        mock_review = _make_review_coder([
            "```yaml\naction: continue\n```"
        ])

        step = PlanStep(1, "test", command="/query What?")
        step.result = "done"
        self.runner._completed_steps[1] = step

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ):
            action = self.runner._review_step(step, [])

        self.assertEqual(action["type"], "continue")
        self.assertEqual(mock_review.run.call_count, 1)


# ---------------------------------------------------------------------------
# Review loop empty user answer tests
# ---------------------------------------------------------------------------

class TestReviewAskUserEmptyAnswer(unittest.TestCase):
    """Test that empty user answers in the review loop cause a continue."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_empty_answer_in_review_continues(self):
        """If user gives empty answer during review ask_user, continue."""
        mock_review = _make_review_coder([
            '```yaml\naction: ask_user\nquestion: "What?"\n```'
        ])
        self.coder.io.prompt_ask.return_value = None

        step = PlanStep(1, "test", command="/query What?")
        step.result = "done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ):
            action = self.runner._review_step(step, [])

        self.assertEqual(action["type"], "continue")
        # Only one review call — didn't loop
        self.assertEqual(mock_review.run.call_count, 1)

    def test_whitespace_answer_in_review_continues(self):
        """Whitespace-only answer during review ask_user should continue."""
        mock_review = _make_review_coder([
            '```yaml\naction: ask_user\nquestion: "What?"\n```'
        ])
        self.coder.io.prompt_ask.return_value = "   \t  "

        step = PlanStep(1, "test", command="/query What?")
        step.result = "done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ):
            action = self.runner._review_step(step, [])

        self.assertEqual(action["type"], "continue")


# ---------------------------------------------------------------------------
# Review coder identity tests
# ---------------------------------------------------------------------------

class TestReviewCoderIdentity(unittest.TestCase):
    """Test that the review coder gets the correct identity."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_review_coder_has_review_prompt(self):
        """Review coder should have the review-specific system prompt."""
        from composez_core.agent_runner import _REVIEW_SYSTEM_PROMPT

        mock_instance = MagicMock()
        mock_instance.gpt_prompts = MagicMock()

        with patch(
            "aider.coders.base_coder.Coder.create",
            return_value=mock_instance,
        ):
            review = self.runner._get_review_coder()

        self.assertEqual(
            review.gpt_prompts.main_system,
            _REVIEW_SYSTEM_PROMPT,
        )

    def test_review_coder_auto_context_disabled(self):
        """Review coder should have auto-context disabled."""
        mock_instance = MagicMock()
        mock_instance.gpt_prompts = MagicMock()

        with patch(
            "aider.coders.base_coder.Coder.create",
            return_value=mock_instance,
        ):
            review = self.runner._get_review_coder()

        self.assertFalse(review._auto_context_enabled)


# ---------------------------------------------------------------------------
# Keyword fallback removal tests
# ---------------------------------------------------------------------------

class TestKeywordFallbackRemoved(unittest.TestCase):
    """Test that the greedy keyword fallback was removed."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_bare_continue_text_returns_none(self):
        """Text mentioning 'continue' without YAML should return None."""
        response = "Before I can continue, I need more information."
        self.assertIsNone(self.runner._parse_review_response(response))

    def test_conversational_text_returns_none(self):
        """Conversational text without any action should return None."""
        response = (
            "I'm ready to help you analyze your manuscript. "
            "Please tell me what you'd like to work on."
        )
        self.assertIsNone(self.runner._parse_review_response(response))

    def test_fenced_yaml_still_works(self):
        """Fenced YAML continue should still be parsed."""
        response = "```yaml\naction: continue\n```"
        action = self.runner._parse_review_response(response)
        self.assertIsNotNone(action)
        self.assertEqual(action["type"], "continue")

    def test_unfenced_action_still_works(self):
        """Unfenced action: line should still be parsed."""
        response = "action: continue"
        action = self.runner._parse_review_response(response)
        self.assertIsNotNone(action)
        self.assertEqual(action["type"], "continue")

    def test_unfenced_done_with_trailing_text(self):
        """Unfenced 'done' followed by prose must not be swallowed."""
        response = (
            "action: done\n"
            'summary: "Task complete"\n'
            "\n"
            "I recommend proceeding with the next step."
        )
        action = self.runner._parse_review_response(response)
        self.assertIsNotNone(action)
        self.assertEqual(action["type"], "done")
        self.assertEqual(action["summary"], "Task complete")

    def test_unfenced_revise_with_trailing_text(self):
        """Unfenced 'revise' block followed by commentary."""
        response = (
            "Based on the failure:\n\n"
            "action: revise\n"
            "plan:\n"
            "  - step: 3\n"
            '    description: "retry"\n'
            '    command: "/query test"\n'
            "\n"
            "This should fix the issue."
        )
        action = self.runner._parse_review_response(response)
        self.assertIsNotNone(action)
        self.assertEqual(action["type"], "revise")
        self.assertEqual(len(action["steps"]), 1)


# ---------------------------------------------------------------------------
# No-parseable-action handling tests
# ---------------------------------------------------------------------------

class TestNoParsableAction(unittest.TestCase):
    """Test that unparseable review responses default to continue."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_unparseable_response_defaults_to_continue(self):
        """If review coder returns gibberish, _review_step should continue."""
        mock_review = _make_review_coder([
            "I have no idea what to do next."
        ])

        step = PlanStep(1, "test", command="/query What?")
        step.result = "done"

        with patch.object(
            self.runner, "_get_review_coder", return_value=mock_review
        ):
            action = self.runner._review_step(step, [])

        self.assertEqual(action["type"], "continue")
        # Warning should have been emitted
        self.coder.io.tool_warning.assert_called()


# ---------------------------------------------------------------------------
# Agent prompt improvements tests
# ---------------------------------------------------------------------------

class TestAgentPromptImprovements(unittest.TestCase):
    """Test that agent prompts instruct immediate YAML output."""

    def test_novel_agent_prompt_has_yaml_instruction(self):
        from composez_core.novel_prompts import NovelAgentPrompts

        prompts = NovelAgentPrompts()
        self.assertIn("MUST contain a ```yaml plan block", prompts.main_system)

    def test_generic_agent_prompt_has_yaml_instruction(self):
        from aider.coders.agent_prompts import AgentPrompts

        prompts = AgentPrompts()
        self.assertIn("MUST contain a ```yaml plan block", prompts.main_system)


# ---------------------------------------------------------------------------
# Bare text warning tests
# ---------------------------------------------------------------------------

class TestBareTextWarning(unittest.TestCase):
    """Test that bare text in commands emits a warning."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)

    def test_slash_command_no_warning(self):
        """Slash commands should not emit a warning."""
        step = PlanStep(1, "test", command="/query What?")
        with patch.object(
            self.runner, "_run_subprocess", return_value=None
        ):
            self.runner._execute_script(step)

        self.coder.io.tool_warning.assert_not_called()

    def test_bare_text_emits_warning(self):
        """Bare text should emit a warning before wrapping as /query."""
        step = PlanStep(1, "test", command="Read the files")
        with patch.object(
            self.runner, "_run_subprocess", return_value=None
        ):
            self.runner._execute_script(step)

        self.coder.io.tool_warning.assert_called()
        warning_text = str(self.coder.io.tool_warning.call_args)
        self.assertIn("bare text", warning_text)


# ---------------------------------------------------------------------------
# File-based context passing tests
# ---------------------------------------------------------------------------

class TestFileBasedContextPassing(unittest.TestCase):
    """Test file-based context passing between dependent steps."""

    def setUp(self):
        self.coder = _make_stub_coder()
        self.runner = AgentRunner(self.coder)
        # Use a temp directory as root so file operations work
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.coder.root = self._tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_run_dir_created(self):
        """Agent execution creates agents/{run_id}/ directory."""
        import os
        run_dir = self.runner._get_run_dir()
        self.assertTrue(os.path.isdir(run_dir))
        self.assertIn("agents/", run_dir)
        self.assertIn(self.runner._run_id, run_dir)

    def test_step_dir_created(self):
        """Step directory is created under run dir."""
        import os
        step_dir = self.runner._step_dir(1)
        self.assertTrue(os.path.isdir(step_dir))
        self.assertIn("step_1", step_dir)

    def test_step_dir_with_sub_id(self):
        """Sub-directories for parallel steps are created."""
        import os
        sub_dir = self.runner._step_dir(3, sub_id=0)
        self.assertTrue(os.path.isdir(sub_dir))
        self.assertIn("step_3", sub_dir)
        self.assertIn("/0", sub_dir)

    def test_slugify(self):
        """Slugify converts descriptions to safe filenames."""
        self.assertEqual(
            AgentRunner._slugify("Analyze the story settings"),
            "analyze_the_story_settings",
        )
        self.assertEqual(
            AgentRunner._slugify("Step with  special!@# chars"),
            "step_with_special_chars",
        )
        self.assertEqual(
            AgentRunner._slugify("A" * 100, max_len=10),
            "a" * 10,
        )

    def test_strip_chrome_without_commands(self):
        """_strip_chrome removes startup noise and body chrome."""
        raw = (
            "─" * 80 + "\n"
            "Aider v0.86.3\n"
            "Model: openai/haiku\n"
            "─" * 80 + "\n"
            ">\n"
            "Restored 7 files from agents/run_123/_before.yml\n"
            ">\n"
            "Deleted act 1, chapter 1, scene 1\n"
            ">\n"
            "Writing Act 1, Chapter 1...\n"
            "This is the actual LLM response.\n"
            "─" * 80 + "\n"
            "Tokens: 1 sent, 100 received.\n"
            "─" * 80 + "\n"
            "Saved 5 files to agents/run_123/_after.yml\n"
            ">\n"
        )
        cleaned = AgentRunner._strip_chrome(raw)
        # Startup banner stripped
        self.assertNotIn("Aider v0.86", cleaned)
        # Body content preserved
        self.assertIn("Restored 7 files", cleaned)
        self.assertIn("Deleted act 1, chapter 1, scene 1", cleaned)
        self.assertIn("Writing Act 1, Chapter 1", cleaned)
        self.assertIn("This is the actual LLM response.", cleaned)
        # Body chrome stripped
        self.assertNotIn("Tokens:", cleaned)
        self.assertNotIn("Saved 5 files", cleaned)

    def test_strip_chrome_with_commands(self):
        """_strip_chrome interleaves commands with output sections."""
        raw = (
            "─" * 80 + "\n"
            "Aider v0.86.3\n"
            "─" * 80 + "\n"
            ">\n"                      # /load ctx
            "Restored 7 files.\n"
            ">\n"                      # /add act 1 chapter 8
            "Added chapter 8.\n"
            ">\n"                      # /delete scene 1
            "Deleted scene 1.\n"
            ">\n"                      # /write
            "Writing chapter...\n"
            "The LLM response.\n"
            "─" * 80 + "\n"
            "Tokens: 100 sent.\n"
            "─" * 80 + "\n"
            ">\n"                      # /save ctx
            "Saved 5 files to agents/foo.yml\n"
            ">\n"
        )
        commands = [
            "/load ctx agents/before.yml",
            "/add act 1 chapter 8",
            "/delete act 1 chapter 8 scene 1",
            "/write act 1 chapter 8",
            "/save ctx agents/after.yml",
        ]
        cleaned = AgentRunner._strip_chrome(raw, commands=commands)
        # Auto-added commands hidden
        self.assertNotIn("> /load ctx", cleaned)
        self.assertNotIn("> /save ctx", cleaned)
        # User commands shown
        self.assertIn("> /add act 1 chapter 8", cleaned)
        self.assertIn("> /delete act 1 chapter 8 scene 1", cleaned)
        self.assertIn("> /write act 1 chapter 8", cleaned)
        # Output preserved
        self.assertIn("Restored 7 files", cleaned)
        self.assertIn("Deleted scene 1.", cleaned)
        self.assertIn("The LLM response.", cleaned)
        # Body chrome stripped
        self.assertNotIn("Tokens:", cleaned)
        self.assertNotIn("Saved 5 files", cleaned)

    def test_strip_chrome_no_prompt(self):
        """_strip_chrome keeps everything if there is no prompt line."""
        raw = "Some output without a prompt.\nMore lines.\n"
        cleaned = AgentRunner._strip_chrome(raw)
        self.assertIn("Some output without a prompt.", cleaned)
        self.assertIn("More lines.", cleaned)

    def test_strip_chrome_edit_format_prompt(self):
        """_strip_chrome recognises edit-format-prefixed prompts like 'diff>'."""
        raw = (
            "─" * 80 + "\n"
            "Aider v0.86\n"
            "diff>\n"
            "Deleted act 1, chapter 1, scene 1\n"
            "The LLM response.\n"
            "Tokens: 100 sent\n"
        )
        cleaned = AgentRunner._strip_chrome(raw)
        self.assertNotIn("Aider v0.86", cleaned)
        self.assertIn("Deleted act 1", cleaned)
        self.assertIn("The LLM response.", cleaned)
        self.assertNotIn("Tokens:", cleaned)

    def test_ensure_gitignore_creates(self):
        """_ensure_gitignore creates .gitignore with agents/ if absent."""
        import os
        AgentRunner._ensure_gitignore(self._tmpdir)
        gitignore = os.path.join(self._tmpdir, ".gitignore")
        self.assertTrue(os.path.isfile(gitignore))
        with open(gitignore) as f:
            self.assertIn("agents/", f.read())

    def test_ensure_gitignore_appends(self):
        """_ensure_gitignore appends agents/ to existing .gitignore."""
        import os
        gitignore = os.path.join(self._tmpdir, ".gitignore")
        with open(gitignore, "w") as f:
            f.write("*.pyc\n")
        AgentRunner._ensure_gitignore(self._tmpdir)
        with open(gitignore) as f:
            content = f.read()
        self.assertIn("*.pyc", content)
        self.assertIn("agents/", content)

    def test_ensure_gitignore_idempotent(self):
        """_ensure_gitignore doesn't duplicate agents/ entry."""
        import os
        gitignore = os.path.join(self._tmpdir, ".gitignore")
        with open(gitignore, "w") as f:
            f.write("agents/\n")
        AgentRunner._ensure_gitignore(self._tmpdir)
        with open(gitignore) as f:
            content = f.read()
        self.assertEqual(content.count("agents/"), 1)

    def test_after_context_saved_in_script(self):
        """_execute_script appends /save ctx to the script."""
        step = PlanStep(1, "analyze", command="/query What?")
        sub_scripts = []

        def record(script, **kw):
            sub_scripts.append(list(script))
            return "Analysis result"

        with patch.object(
            self.runner, "_run_subprocess", side_effect=record
        ):
            self.runner._execute_script(step)

        # Last command should be /save ctx with agents/ path
        last_cmd = sub_scripts[0][-1]
        self.assertTrue(last_cmd.startswith("/save ctx agents/"))
        self.assertIn("_after_context_", last_cmd)

    def test_analysis_saved_to_disk(self):
        """Step results are written to _analysis_{slug}.txt."""
        import os
        step = PlanStep(1, "analyze things", command="/query What?")

        with patch.object(
            self.runner, "_run_subprocess", return_value="Found 3 issues"
        ):
            self.runner._execute_script(step)

        analysis_path = self.runner._analysis_path(1, "analyze things")
        self.assertTrue(os.path.isfile(analysis_path))
        with open(analysis_path) as f:
            content = f.read()
        # Analysis includes the command prefix and output.
        self.assertIn("/query What?", content)
        self.assertIn("Found 3 issues", content)

    def test_analysis_not_saved_for_failed_step(self):
        """Failed steps don't get analysis files."""
        import os
        step = PlanStep(1, "will fail", command="/query What?")

        with patch.object(
            self.runner, "_run_subprocess",
            return_value="ERROR: subprocess failed",
        ):
            self.runner._execute_script(step)

        analysis_path = self.runner._analysis_path(1, "will fail")
        self.assertFalse(os.path.isfile(analysis_path))

    def test_sequential_step_loads_before_context(self):
        """A step following a completed step gets /load ctx prepended."""
        import os
        import yaml

        # Simulate step 1 having completed with context saved
        step1 = PlanStep(1, "gather info", command="/query What?")
        step1.result = "Found important context"
        self.runner._completed_steps[1] = step1

        # Write a fake after-context for step 1
        after_path = self.runner._after_context_path(1, "gather info")
        os.makedirs(os.path.dirname(after_path), exist_ok=True)
        with open(after_path, "w") as f:
            yaml.dump({"editable": ["file1.md"], "read_only": []}, f)

        # Write fake analysis
        analysis_path = self.runner._analysis_path(1, "gather info")
        with open(analysis_path, "w") as f:
            f.write("Found important context")

        # Now run step 2 — context flows automatically from step 1
        step2 = PlanStep(2, "use context", command="/edit Fix it")
        sub_scripts = []

        def record(script, **kw):
            sub_scripts.append(list(script))
            return "Done"

        with patch.object(
            self.runner, "_run_subprocess", side_effect=record
        ):
            self.runner._execute_script(step2)

        # First command should be /load ctx with the before-context path
        first_cmd = sub_scripts[0][0]
        self.assertTrue(first_cmd.startswith("/load ctx agents/"))
        self.assertIn("_before_context_", first_cmd)

    def test_before_context_includes_analysis_as_read_only(self):
        """Analysis files appear in read_only list of before-context."""
        import os
        import yaml

        step1 = PlanStep(1, "analyze", command="/query What?")
        step1.result = "Analysis output"
        self.runner._completed_steps[1] = step1

        # Write after-context and analysis
        after_path = self.runner._after_context_path(1, "analyze")
        os.makedirs(os.path.dirname(after_path), exist_ok=True)
        with open(after_path, "w") as f:
            yaml.dump({"editable": [], "read_only": ["db/chars.md"]}, f)
        analysis_path = self.runner._analysis_path(1, "analyze")
        with open(analysis_path, "w") as f:
            f.write("Analysis output")

        step2 = PlanStep(2, "edit", command="/edit Fix")
        before_path = self.runner._build_before_context(step2)

        self.assertIsNotNone(before_path)
        with open(before_path) as f:
            data = yaml.safe_load(f)

        # Analysis file should be in read_only
        analysis_rel = os.path.relpath(analysis_path, self._tmpdir)
        self.assertIn(analysis_rel, data["read_only"])
        # Original read_only from step 1 should be merged in
        self.assertIn("db/chars.md", data["read_only"])

    def test_failed_step_excluded_from_before_context(self):
        """Failed prior steps are not included in before-context."""
        step1 = PlanStep(1, "will fail", command="/query What?")
        step1.result = "ERROR: failed"
        self.runner._completed_steps[1] = step1

        step2 = PlanStep(2, "next step", command="/edit Fix")
        before_path = self.runner._build_before_context(step2)

        # No before-context since the only prior step failed
        self.assertIsNone(before_path)

    def test_no_before_context_for_first_step(self):
        """The first step (no completed predecessors) gets no before-context."""
        step = PlanStep(1, "first step", command="/query What?")
        before_path = self.runner._build_before_context(step)
        self.assertIsNone(before_path)

    def test_cleanup_removes_run_dir(self):
        """Cleanup removes agents/{run_id}/ when DEBUG is not set."""
        import os

        # Create the run dir
        self.runner._get_run_dir()
        self.assertTrue(os.path.isdir(self.runner._run_dir))

        with patch.dict(os.environ, {}, clear=True):
            self.runner._cleanup_run_dir()

        self.assertFalse(os.path.isdir(self.runner._run_dir))

    def test_cleanup_preserves_on_debug(self):
        """Cleanup preserves agents/{run_id}/ when DEBUG=1."""
        import os

        self.runner._get_run_dir()
        self.assertTrue(os.path.isdir(self.runner._run_dir))

        with patch.dict(os.environ, {"DEBUG": "1"}):
            self.runner._cleanup_run_dir()

        self.assertTrue(os.path.isdir(self.runner._run_dir))

    def test_before_context_uses_only_immediate_prior(self):
        """Before-context loads only from the immediate prior step."""
        import os
        import yaml

        step1 = PlanStep(1, "gather A", command="/query A")
        step1.result = "Result A"
        self.runner._completed_steps[1] = step1

        step2 = PlanStep(2, "gather B", command="/query B")
        step2.result = "Result B"
        self.runner._completed_steps[2] = step2

        # Write after-contexts
        for num, desc, files in [
            (1, "gather A", ["file_a.md"]),
            (2, "gather B", ["file_b.md"]),
        ]:
            path = self.runner._after_context_path(num, desc)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                yaml.dump({"editable": files, "read_only": []}, f)
            analysis = self.runner._analysis_path(num, desc)
            with open(analysis, "w") as f:
                f.write(f"Result {chr(64 + num)}")

        step3 = PlanStep(3, "combine", command="/edit Combine")
        before_path = self.runner._build_before_context(step3)

        self.assertIsNotNone(before_path)
        with open(before_path) as f:
            data = yaml.safe_load(f)

        # Only step 2's context (the immediate prior), not step 1's
        self.assertNotIn("file_a.md", data["editable"])
        self.assertIn("file_b.md", data["editable"])
        # Only step 2's analysis file in read_only
        self.assertEqual(len([r for r in data["read_only"]
                             if "_analysis_" in r]), 1)

    def test_analysis_truncated(self):
        """Analysis files are truncated to _MAX_ANALYSIS_LEN."""
        import os
        step = PlanStep(1, "long output", command="/query What?")

        long_text = "x" * 20000
        with patch.object(
            self.runner, "_run_subprocess", return_value=long_text
        ):
            self.runner._execute_script(step)

        analysis_path = self.runner._analysis_path(1, "long output")
        with open(analysis_path) as f:
            content = f.read()

        self.assertLessEqual(
            len(content), self.runner._MAX_ANALYSIS_LEN + 10
        )

    def test_debug_log_written_for_script(self):
        """When DEBUG=1, _execute_script writes a log file to step dir."""
        import os

        step = PlanStep(1, "analyze things", command="/query What?")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Found 3 issues"
        mock_result.stderr = ""

        with patch.dict(os.environ, {"DEBUG": "1"}):
            with patch(
                "composez_core.agent_runner.subprocess.run",
                return_value=mock_result,
            ):
                self.runner._execute_script(step)

        step_dir = self.runner._step_dir(1)
        log_files = [f for f in os.listdir(step_dir) if f.startswith("_log_")]
        self.assertEqual(len(log_files), 1)

        log_path = os.path.join(step_dir, log_files[0])
        with open(log_path) as f:
            content = f.read()

        self.assertIn("=== INPUT", content)
        self.assertIn("/query What?", content)
        self.assertIn("=== OUTPUT", content)
        self.assertIn("Found 3 issues", content)

    def test_no_debug_log_without_env(self):
        """Without DEBUG set, no log file is created."""
        import os

        step = PlanStep(1, "analyze things", command="/query What?")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""

        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "composez_core.agent_runner.subprocess.run",
                return_value=mock_result,
            ):
                self.runner._execute_script(step)

        step_dir = self.runner._step_dir(1)
        log_files = [f for f in os.listdir(step_dir) if f.startswith("_log_")]
        self.assertEqual(len(log_files), 0)

    def test_debug_log_written_for_parallel(self):
        """When DEBUG=1, _execute_parallel writes log files per sub-task."""
        import os

        step = PlanStep(
            1, "parallel work",
            parallel=[["/write scene A"], ["/write scene B"]],
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Done"
        mock_result.stderr = ""

        with patch.dict(os.environ, {"DEBUG": "1"}):
            with patch(
                "composez_core.agent_runner.subprocess.run",
                return_value=mock_result,
            ):
                self.runner._execute_parallel(step)

        # Each sub-task should have its own log file
        for sub_id in range(2):
            sub_dir = self.runner._step_dir(1, sub_id=sub_id)
            log_files = [
                f for f in os.listdir(sub_dir) if f.startswith("_log_")
            ]
            self.assertEqual(len(log_files), 1, f"sub_id={sub_id}")

    def test_write_log_contains_error_output(self):
        """Log file captures error output from failed subprocesses."""
        import os

        step = PlanStep(1, "failing step", command="/query What?")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "partial output"
        mock_result.stderr = "error details"

        with patch.dict(os.environ, {"DEBUG": "1"}):
            with patch(
                "composez_core.agent_runner.subprocess.run",
                return_value=mock_result,
            ):
                self.runner._execute_script(step)

        step_dir = self.runner._step_dir(1)
        log_files = [f for f in os.listdir(step_dir) if f.startswith("_log_")]
        self.assertEqual(len(log_files), 1)

        log_path = os.path.join(step_dir, log_files[0])
        with open(log_path) as f:
            content = f.read()

        self.assertIn("ERROR:", content)

    def test_run_subprocess_log_path_parameter(self):
        """_run_subprocess writes log when log_path is provided."""
        import os

        log_path = os.path.join(self._tmpdir, "test_log.txt")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output text"
        mock_result.stderr = ""

        with patch(
            "composez_core.agent_runner.subprocess.run",
            return_value=mock_result,
        ):
            self.runner._run_subprocess(
                ["/query Hello"], log_path=log_path,
            )

        self.assertTrue(os.path.isfile(log_path))
        with open(log_path) as f:
            content = f.read()
        self.assertIn("/query Hello", content)
        self.assertIn("output text", content)


# ---------------------------------------------------------------------------
# Save/load direct path tests (via novel_commands)
# ---------------------------------------------------------------------------

class TestSaveLoadDirectPath(unittest.TestCase):
    """Test /save ctx and /load ctx with direct file paths."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()

        self.coder = _make_stub_coder()
        self.coder.root = self._tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()

        # Create a real file to add to context
        import os
        self._test_file = os.path.join(self._tmpdir, "test.md")
        with open(self._test_file, "w") as f:
            f.write("test content")
        self.coder.abs_fnames.add(self._test_file)

        from composez_core.novel_commands import NovelCommands
        self.cmds = NovelCommands.__new__(NovelCommands)
        self.cmds.io = self.coder.io
        self.cmds.coder = self.coder
        self.cmds.root = self._tmpdir

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_save_ctx_with_direct_path(self):
        """'/save ctx agents/run_1/step_1/_after.yml' saves to that path."""
        import os
        self.cmds.cmd_save("ctx agents/run_1/step_1/_after.yml")
        path = os.path.join(self._tmpdir, "agents/run_1/step_1/_after.yml")
        self.assertTrue(os.path.isfile(path))

    def test_load_ctx_with_direct_path(self):
        """'/load ctx agents/run_1/step_1/_before.yml' loads from that path."""
        import os
        import yaml

        # First save a context file at a direct path
        path = os.path.join(self._tmpdir, "agents/run_1/step_1/_before.yml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {"editable": ["test.md"], "read_only": []}
        with open(path, "w") as f:
            yaml.dump(data, f)

        # Clear current context
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()

        self.cmds.cmd_load("ctx agents/run_1/step_1/_before.yml")

        self.assertIn(self._test_file, self.coder.abs_fnames)

    def test_load_ctx_fallback_to_cache(self):
        """If direct path doesn't exist, falls back to cache/context/."""
        import os
        import yaml

        # Create in cache dir
        cache_path = os.path.join(self._tmpdir, "cache/context/myname.yml")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        data = {"editable": ["test.md"], "read_only": []}
        with open(cache_path, "w") as f:
            yaml.dump(data, f)

        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()

        # Load by name — should fall back to cache
        self.cmds.cmd_load("ctx myname")
        self.assertIn(self._test_file, self.coder.abs_fnames)

    def test_save_ctx_without_path_uses_cache(self):
        """'/save ctx myname' still uses cache/context/myname.yml."""
        import os
        self.cmds.cmd_save("ctx myname")
        cache_path = os.path.join(
            self._tmpdir, "cache/context/myname.yml"
        )
        self.assertTrue(os.path.isfile(cache_path))


if __name__ == "__main__":
    unittest.main()
