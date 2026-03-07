"""Agent runner — parses and executes multi-step plans produced by AgentCoder.

A plan is a list of steps.  Each step has one of:
- ``commands``: a list of slash commands to run sequentially (a "script")
- ``parallel``: a list of scripts to run concurrently in subprocesses
- ``ask_user``: a question to ask the user (pauses execution)

After each step that produces meaningful results, the orchestrator LLM
reviews the output and decides whether to continue, revise the remaining
plan, ask the user a question, or declare the task done.

**Every** step (sequential or parallel) runs in its own aider subprocess
for isolation of ``/add``, ``/drop``, chat history, and git state.
Context flows automatically between sequential steps via files:

- Each agent execution gets a working directory ``agents/{run_id}/``.
- After each step, file context is saved to ``_after_context_*.yml``
  and text results are saved to ``_analysis_*.txt``.
- Each step automatically loads a merged before-context from all
  completed prior steps, including file contexts and analysis files
  (as read-only context, benefiting from prompt caching).
- The ``agents/`` directory is cleaned up after execution unless
  ``DEBUG`` is set in the environment.

Auto-commits are disabled for the duration of plan execution so the
caller can commit explicitly at the end.
"""

import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

# Maximum number of review loop iterations before forcing a continue.
MAX_REVIEW_ITERATIONS = 5

# Maximum number of re-prompts when ask_user returns empty.
MAX_ASK_USER_RETRIES = 2

# Maximum number of plan revisions before forcing plan completion.
MAX_PLAN_REVISIONS = 2

# Maximum total review calls across the entire plan execution.
MAX_TOTAL_REVIEWS = 15

# Hard ceiling on total steps executed (including revision-created steps).
MAX_TOTAL_STEPS = 20

# Maximum net-new steps a single revision can add beyond what it replaces.
MAX_NET_NEW_STEPS = 3


class PlanStep:
    """One step in an agent plan.

    Accepts ``command`` (single string) or ``commands`` (list) — both
    are normalised to ``self.commands``.  Parallel entries are normalised
    to a list of scripts (list of lists).
    """

    __slots__ = (
        "number",
        "description",
        "commands",
        "parallel",
        "ask_user",
        "result",
        "_script_commands",
    )

    def __init__(self, number, description, *, command=None, commands=None,
                 parallel=None, ask_user=None):
        self.number = number
        self.description = description or ""

        # Normalise: single command → commands list
        if command and not commands:
            self.commands = [command]
        else:
            self.commands = commands or []

        # Normalise: parallel entries → list of scripts (list of lists)
        if parallel:
            self.parallel = [
                [item] if isinstance(item, str) else item
                for item in parallel
            ]
        else:
            self.parallel = []

        self.ask_user = ask_user
        self.result = None

    def __repr__(self):
        if self.commands:
            if len(self.commands) == 1:
                return f"PlanStep({self.number}, command={self.commands[0]!r})"
            return f"PlanStep({self.number}, commands={self.commands!r})"
        if self.parallel:
            return f"PlanStep({self.number}, parallel={self.parallel!r})"
        if self.ask_user:
            return f"PlanStep({self.number}, ask_user={self.ask_user!r})"
        return f"PlanStep({self.number})"


class AgentRunner:
    """Parse and execute agent plans with a dynamic review loop.

    Parameters
    ----------
    coder : Coder
        The agent-mode coder whose ``commands`` attribute provides access
        to the slash command infrastructure.
    """

    # Commands whose results warrant orchestrator review.
    _REVIEW_COMMANDS = frozenset(["/query", "/write", "/summarize", "/edit"])

    def __init__(self, coder):
        self.coder = coder
        self.io = coder.io
        self.commands = coder.commands
        self.user_answers = {}  # step_number -> answer string
        self._review_coder = None  # Persistent review coder (lazy)
        self._completed_steps = {}  # step_number -> PlanStep (with results)
        self._revision_count = 0   # Total plan revisions
        self._review_count = 0     # Total review calls across the plan
        # Agent working directory: agents/{run_id}/
        self._run_id = f"run_{int(time.time() * 1000)}"
        self._run_dir = None  # Created lazily by _get_run_dir()

    def _emit(self, event_type, data=None):
        """Emit a structured agent event if the IO supports it."""
        if hasattr(self.io, "agent_event"):
            self.io.agent_event(event_type, data)

    # ------------------------------------------------------------------
    # Agent working directory
    # ------------------------------------------------------------------

    def _get_run_dir(self):
        """Return the ``agents/{run_id}/`` directory, creating it if needed.

        Also ensures ``agents/`` is in ``.gitignore`` so transient working
        files never pollute the repo-map or get committed.
        """
        if self._run_dir is None:
            root = self._get_repo_root()
            self._run_dir = os.path.join(root, "agents", self._run_id)
            os.makedirs(self._run_dir, exist_ok=True)
            self._ensure_gitignore(root)
        return self._run_dir

    @staticmethod
    def _ensure_gitignore(root):
        """Add ``agents/`` to ``.gitignore`` if not already present."""
        gitignore = os.path.join(root, ".gitignore")
        marker = "agents/"
        try:
            if os.path.isfile(gitignore):
                with open(gitignore, encoding="utf-8") as f:
                    content = f.read()
                if marker in content.splitlines():
                    return  # already present
                # Append with a trailing newline
                if not content.endswith("\n"):
                    content += "\n"
                content += f"{marker}\n"
            else:
                content = f"{marker}\n"
            with open(gitignore, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            pass  # best-effort

    def _step_dir(self, step_number, sub_id=None):
        """Return ``agents/{run_id}/step_{N}/`` (or ``.../step_{N}/{sub_id}/``)."""
        parts = [self._get_run_dir(), f"step_{step_number}"]
        if sub_id is not None:
            parts.append(str(sub_id))
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _slugify(text, max_len=40):
        """Convert a description to a safe filename fragment."""
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return slug[:max_len] if slug else "step"

    def _after_context_path(self, step_number, description):
        """Path for the after-step context file."""
        slug = self._slugify(description)
        return os.path.join(
            self._step_dir(step_number),
            f"_after_context_{slug}.yml",
        )

    def _analysis_path(self, step_number, description):
        """Path for the step analysis/result text file."""
        slug = self._slugify(description)
        return os.path.join(
            self._step_dir(step_number),
            f"_analysis_{slug}.txt",
        )

    # ------------------------------------------------------------------
    # Plan parsing
    # ------------------------------------------------------------------

    def parse_plan(self, content):
        """Extract a YAML plan from the LLM response text.

        Returns a list of ``PlanStep`` or ``None`` if no valid plan found.
        """
        yaml_block = self._extract_yaml(content)
        if yaml_block is None:
            self.io.tool_error("No YAML plan found in the response.")
            return None

        try:
            data = yaml.safe_load(yaml_block)
        except yaml.YAMLError as exc:
            self.io.tool_error(f"Failed to parse plan YAML: {exc}")
            return None

        if not isinstance(data, dict) or "plan" not in data:
            self.io.tool_error("YAML does not contain a 'plan' key.")
            return None

        raw_steps = data["plan"]
        if not isinstance(raw_steps, list) or not raw_steps:
            self.io.tool_error("Plan is empty or not a list.")
            return None

        steps = []
        for entry in raw_steps:
            step = self._parse_step(entry)
            if step is None:
                return None
            steps.append(step)

        return steps

    def _extract_yaml(self, content):
        """Pull the first fenced YAML block from *content*.

        Falls back to unfenced ``plan:`` or ``action:`` blocks.
        """
        pattern = r"```ya?ml\s*\n(.*?)```"
        m = re.search(pattern, content, re.DOTALL)
        if m:
            return m.group(1)

        pattern2 = r"^((?:plan|action):\s*\n(?:[ \t].*\n?)*)"
        m2 = re.search(pattern2, content, re.MULTILINE)
        if m2:
            return m2.group(1)

        return None

    def _parse_step(self, entry):
        """Parse a single step dict from the YAML plan."""
        if not isinstance(entry, dict):
            self.io.tool_error(f"Plan step is not a dict: {entry!r}")
            return None

        number = entry.get("step")
        if number is None:
            self.io.tool_error(f"Plan step missing 'step' number: {entry!r}")
            return None

        description = entry.get("description", "")
        command = entry.get("command")       # single string (convenience)
        commands = entry.get("commands")     # list of strings (preferred)
        parallel = entry.get("parallel")
        ask_user = entry.get("ask_user")
        # depends_on is silently ignored — steps are always sequential.

        # Normalise command/commands into a single flag for counting
        has_commands = command is not None or commands is not None

        action_count = sum([
            has_commands,
            parallel is not None,
            ask_user is not None,
        ])
        if action_count == 0:
            self.io.tool_error(
                f"Step {number} has no action "
                f"(need command/commands, parallel, or ask_user)."
            )
            return None
        if action_count > 1:
            self.io.tool_error(
                f"Step {number} has multiple actions "
                f"(only one of command/commands/parallel/ask_user allowed)."
            )
            return None

        # Build commands list
        cmds = None
        if commands is not None:
            if not isinstance(commands, list):
                self.io.tool_error(
                    f"Step {number}: commands must be a list."
                )
                return None
            cmds = commands
        elif command is not None:
            cmds = [command]

        # Normalise parallel entries into scripts (list of lists)
        par_scripts = []
        if parallel is not None:
            if not isinstance(parallel, list):
                self.io.tool_error(f"Step {number}: parallel must be a list.")
                return None
            for item in parallel:
                if isinstance(item, str):
                    par_scripts.append([item])
                elif isinstance(item, dict):
                    if "commands" in item:
                        par_scripts.append(item["commands"])
                    elif "command" in item:
                        par_scripts.append([item["command"]])
                    else:
                        self.io.tool_error(
                            f"Step {number}: invalid parallel entry: {item!r}"
                        )
                        return None
                else:
                    self.io.tool_error(
                        f"Step {number}: invalid parallel entry: {item!r}"
                    )
                    return None

        return PlanStep(
            number=number,
            description=description,
            commands=cmds,
            parallel=par_scripts,
            ask_user=ask_user,
        )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    @property
    def _has_structured_ui(self):
        """True when the IO supports structured agent events (web UI)."""
        return hasattr(self.io, "agent_event")

    def show_plan(self, steps, include_completed=False):
        """Pretty-print the plan for user review.

        When *include_completed* is True (used after a revision), completed
        steps are prepended to the emitted list so the UI has the full picture.
        """
        # Emit structured plan for the UI
        plan_steps = []

        if include_completed and self._completed_steps:
            for num in sorted(self._completed_steps):
                s = self._completed_steps[num]
                step_info = {
                    "number": s.number,
                    "description": s.description,
                    "type": "commands",
                    "status": "error" if _step_failed(s) else "success",
                }
                plan_steps.append(step_info)

        for step in steps:
            step_info = {
                "number": step.number,
                "description": step.description,
            }
            if step.commands:
                step_info["type"] = "commands"
                step_info["commands"] = step.commands
            elif step.parallel:
                step_info["type"] = "parallel"
                step_info["subtaskCount"] = len(step.parallel)
                step_info["scripts"] = [
                    s[0] if len(s) == 1 else " -> ".join(s)
                    for s in step.parallel
                ]
            elif step.ask_user:
                step_info["type"] = "ask_user"
                step_info["question"] = step.ask_user
            plan_steps.append(step_info)
        self._emit("plan", {"steps": plan_steps})

        # Only emit text plan when structured UI is not available (terminal)
        if not self._has_structured_ui:
            self.io.tool_output("\n--- Agent Plan ---\n")
            for step in steps:
                self.io.tool_output(f"  Step {step.number}: {step.description}")
                if step.commands:
                    for cmd in step.commands:
                        self.io.tool_output(f"    {cmd}")
                elif step.parallel:
                    self.io.tool_output("    (parallel)")
                    for script in step.parallel:
                        if len(script) == 1:
                            self.io.tool_output(f"      {script[0]}")
                        else:
                            self.io.tool_output("      ---")
                            for cmd in script:
                                self.io.tool_output(f"        {cmd}")
                elif step.ask_user:
                    self.io.tool_output(f"    [ask user] {step.ask_user}")
            self.io.tool_output("")

    # ------------------------------------------------------------------
    # Execution — dynamic agent loop
    # ------------------------------------------------------------------

    def execute(self, steps):
        """Execute steps in a dynamic agent loop.

        Auto-commits are disabled for the duration so only an explicit
        ``/git commit`` step will create a commit.
        """
        # Disable auto-commits during plan execution
        original_auto_commits = getattr(self.coder, "auto_commits", True)
        self.coder.auto_commits = False

        try:
            self._execute_loop(steps)
        finally:
            self.coder.auto_commits = original_auto_commits
            self._cleanup_run_dir()

    def _execute_loop(self, steps):
        remaining = list(steps)
        self._completed_steps = {}
        steps_executed = 0

        while remaining:
            step = remaining.pop(0)

            # Hard ceiling on total steps to prevent revision-driven blowup.
            steps_executed += 1
            if steps_executed > MAX_TOTAL_STEPS:
                self.io.tool_warning(
                    f"  Hit step limit ({MAX_TOTAL_STEPS}), "
                    f"stopping plan execution."
                )
                self._emit("done", {
                    "summary": "Stopped: too many steps executed",
                })
                return

            if not self._has_structured_ui:
                self.io.tool_output(
                    f"\n--- Step {step.number}: {step.description} ---"
                )
            self._emit("step_start", {
                "step": step.number,
                "description": step.description,
            })

            try:
                if step.ask_user:
                    self._execute_ask_user(step)
                elif step.commands:
                    self._execute_script(step)
                elif step.parallel:
                    self._execute_parallel(step)
            except KeyboardInterrupt:
                self._emit("step_end", {
                    "step": step.number,
                    "status": "error",
                    "result": "Interrupted by user",
                })
                self.io.tool_warning("\nPlan execution interrupted by user.")
                self._emit("done", {"summary": "Interrupted by user"})
                return
            except Exception as exc:
                self.io.tool_error(f"Step {step.number} failed: {exc}")
                step.result = f"ERROR: {exc}"

            self._completed_steps[step.number] = step

            is_error = (
                step.result and isinstance(step.result, str)
                and step.result.startswith("ERROR:")
            )

            self._emit("step_end", {
                "step": step.number,
                "status": "error" if is_error else "success",
                "result": _truncate(str(step.result), 600) if step.result else None,
            })

            # Failed ask_user steps are fatal — the user didn't provide
            # required input, so proceeding with the plan makes no sense.
            if step.ask_user and is_error:
                self.io.tool_warning(
                    f"  Step {step.number}: user input required but not "
                    f"provided — stopping plan."
                )
                self._emit("done", {
                    "summary": "Stopped: required user input not provided",
                })
                return

            # Successful ask_user steps don't need review — the answer is
            # captured in user_answers and will be interpolated into later
            # steps via {answer:N}.  Reviewing them just risks the review
            # coder producing spurious ask_user/revise loops.
            if step.ask_user and not is_error:
                continue

            # Review results with the orchestrator if warranted
            handled = False
            if self._should_review(step):
                # Guard against runaway review loops
                self._review_count += 1
                if self._review_count > MAX_TOTAL_REVIEWS:
                    self.io.tool_warning(
                        f"  Hit review limit ({MAX_TOTAL_REVIEWS}), "
                        f"stopping plan execution."
                    )
                    self._emit("done", {
                        "summary": "Stopped: too many review cycles",
                    })
                    return

                self._emit("review_start", {"step": step.number})
                action = self._review_step(step, remaining)
                self._emit("review_end", {
                    "step": step.number,
                    "action": action["type"] if action else "continue",
                })
                if action is not None:
                    handled = True
                    if action["type"] == "done":
                        summary = action.get("summary", "Task complete")
                        if not self._has_structured_ui:
                            self.io.tool_output(
                                f"\n--- Agent: {summary} ---\n"
                            )
                        self._emit("done", {"summary": summary})
                        return
                    elif action["type"] == "revise":
                        self._revision_count += 1
                        if self._revision_count > MAX_PLAN_REVISIONS:
                            self.io.tool_warning(
                                f"  Hit revision limit "
                                f"({MAX_PLAN_REVISIONS}), continuing "
                                f"with current plan."
                            )
                        else:
                            new_steps = action["steps"]
                            # Cap net-new steps to prevent inflation.
                            replaced_count = len(remaining)
                            net_new = len(new_steps) - replaced_count
                            if net_new > MAX_NET_NEW_STEPS:
                                self.io.tool_warning(
                                    f"  Revision adds {net_new} net new "
                                    f"steps (limit {MAX_NET_NEW_STEPS}),"
                                    f" trimming."
                                )
                                new_steps = new_steps[
                                    :replaced_count + MAX_NET_NEW_STEPS
                                ]
                            remaining = new_steps
                            if not self._has_structured_ui:
                                self.io.tool_output(
                                    "\n--- Plan revised ---"
                                )
                            self.show_plan(
                                remaining, include_completed=True
                            )
                    # "continue" → proceed with next step

            # Fallback: if step errored and wasn't handled by the reviewer,
            # ask the user whether to continue.
            if is_error and not handled:
                if not self.io.confirm_ask("Continue with remaining steps?"):
                    self._emit("done", {"summary": "Stopped due to error"})
                    return

        if not self._has_structured_ui:
            self.io.tool_output("\n--- Plan complete ---\n")
        self._emit("done", {"summary": "Plan complete"})

    def _should_review(self, step):
        """Decide whether this step's results warrant orchestrator review.

        Note: ask_user steps are handled directly in _execute_loop (success
        skips review, failure stops the plan) so they never reach here.
        """
        if (step.result and isinstance(step.result, str)
                and step.result.startswith("ERROR:")):
            return True

        # Check if any command in the script is reviewable
        if step.commands:
            for cmd_text in step.commands:
                cmd = cmd_text.strip().split()[0] if cmd_text.strip() else ""
                if cmd in self._REVIEW_COMMANDS:
                    return True

        # Check all scripts in a parallel step
        if step.parallel:
            for script in step.parallel:
                for cmd_text in script:
                    cmd = (cmd_text.strip().split()[0]
                           if cmd_text.strip() else "")
                    if cmd in self._REVIEW_COMMANDS:
                        return True

        return False

    # ------------------------------------------------------------------
    # Review loop
    # ------------------------------------------------------------------

    def _get_review_coder(self):
        """Lazily create the persistent review coder.

        The review coder uses a minimal system prompt focused on reviewing
        step results and deciding next actions.  Auto-context is disabled
        because the review coder doesn't need manuscript files — it only
        needs step results (provided in the user message).
        """
        if self._review_coder is None:
            try:
                from aider.coders.base_coder import Coder

                self._review_coder = Coder.create(
                    io=self.io,
                    main_model=self.coder.main_model,
                    from_coder=self.coder,
                    edit_format="query",
                    autonomy="direct",
                    summarize_from_coder=False,
                )

                # Override the system prompt with a review-specific one.
                # Coder.create activates novel query mode (NovelQueryPrompts)
                # which gives the coder a "fiction editor" identity — wrong
                # for an orchestrator reviewer.
                self._review_coder.gpt_prompts.main_system = _REVIEW_SYSTEM_PROMPT

                # Don't let the review coder retry (reflect) on its own
                # output — we handle retries in _review_step's loop.
                self._review_coder.max_reflections = 1

                # Disable auto-context — the review coder doesn't need to
                # identify manuscript files.  It only reviews step results.
                self._review_coder._auto_context_enabled = False

            except Exception as exc:
                self.io.tool_warning(f"Could not create review coder: {exc}")
                return None
        return self._review_coder

    def _review_step(self, step, remaining):
        """Review step results with the orchestrator LLM.

        Returns an action dict (continue/revise/ask_user/done) or None.
        """
        review_coder = self._get_review_coder()
        if review_coder is None:
            return None

        # Clear conversation history between step reviews.  The review
        # prompt already includes all completed-step context, so stale
        # messages only confuse the model (it starts cycling between
        # done/revise/continue after too many turns).
        if hasattr(review_coder, "done_messages"):
            review_coder.done_messages = []
        if hasattr(review_coder, "cur_messages"):
            review_coder.cur_messages = []

        # Load analysis files as read-only context for the review coder.
        # This benefits from prompt caching across review iterations.
        review_coder.abs_read_only_fnames = set()
        for num in sorted(self._completed_steps):
            s = self._completed_steps[num]
            analysis = self._analysis_path(num, s.description)
            if os.path.isfile(analysis):
                review_coder.abs_read_only_fnames.add(
                    os.path.abspath(analysis)
                )

        prompt = self._build_review_prompt(step, remaining)
        asked_user = False

        for iteration in range(MAX_REVIEW_ITERATIONS):
            if not self._has_structured_ui:
                self.io.tool_output("\n  Reviewing results...")
            try:
                from aider.commands import SwitchCoder

                response = review_coder.run(with_message=prompt)
            except SwitchCoder:
                return {"type": "continue"}
            except KeyboardInterrupt:
                self.io.tool_warning("\nReview interrupted.")
                return None
            except Exception as exc:
                self.io.tool_error(f"Review failed: {exc}")
                return None

            action = self._parse_review_response(response)
            if action is None:
                # No parseable action — default to continue
                self.io.tool_warning(
                    "  Review produced no parseable action, continuing."
                )
                return {"type": "continue"}

            if action["type"] == "ask_user":
                # Only allow one ask_user per review cycle to prevent
                # the review coder from repeatedly questioning the user.
                if asked_user:
                    self.io.tool_output(
                        "  Review asked user twice, continuing."
                    )
                    return {"type": "continue"}
                asked_user = True
                answer = self._prompt_user(action["question"])
                if not answer or not answer.strip():
                    self.io.tool_output("  User provided no answer, continuing.")
                    return {"type": "continue"}
                self.io.tool_output(f"  User answered: {answer}")
                prompt = f"The user answered: {answer}"
                continue
            else:
                return action

        # Exhausted review iterations — force continue
        self.io.tool_warning(
            f"  Review loop hit {MAX_REVIEW_ITERATIONS} iterations, "
            f"forcing continue."
        )
        return {"type": "continue"}

    def _build_review_prompt(self, step, remaining):
        """Build the review prompt with step results and remaining plan.

        Full analysis text is loaded as read-only file context (set up
        in ``_review_step``).  The prompt includes step descriptions and
        status, plus a short result preview for the latest step.
        """
        parts = []

        if self._completed_steps:
            parts.append("## Completed Steps\n")
            parts.append(
                "(Full analysis for each step is attached as a read-only "
                "file.  See the _analysis_*.txt files.)\n"
            )
            for num in sorted(self._completed_steps):
                s = self._completed_steps[num]
                status = "FAILED" if _step_failed(s) else "OK"
                parts.append(f"Step {s.number}: {s.description} [{status}]\n")

        parts.append("\n## Latest Step Result\n")
        parts.append(f"Step {step.number}: {step.description}\n")
        if step.result:
            parts.append(f"Result:\n{_truncate(str(step.result), 2000)}\n")
        else:
            parts.append("Result: (no output)\n")

        if remaining:
            parts.append("\n## Remaining Plan\n")
            for s in remaining:
                if s.commands:
                    cmds = " -> ".join(s.commands)
                    parts.append(
                        f"Step {s.number}: {s.description} -> {cmds}\n"
                    )
                elif s.parallel:
                    scripts = []
                    for script in s.parallel:
                        scripts.append(" -> ".join(script))
                    parts.append(
                        f"Step {s.number}: {s.description} -> (parallel) "
                        f"{'; '.join(scripts)}\n"
                    )
                elif s.ask_user:
                    parts.append(
                        f"Step {s.number}: {s.description} -> [ask user] "
                        f"{s.ask_user}\n"
                    )
        else:
            parts.append("\n(No remaining steps)\n")

        parts.append("""
## Instructions

Review the latest step result. Decide what to do next.
Respond with a YAML block:

To proceed with the next step as planned (THIS IS THE DEFAULT):
```yaml
action: continue
```

To ask the user a question before proceeding:
```yaml
action: ask_user
question: "Your question here"
```

To revise the remaining plan (replacing all remaining steps):
```yaml
action: revise
plan:
  - step: N
    description: "..."
    commands:
      - "/add ..."
      - "/write ..."
```

To stop execution (task is complete or should be abandoned):
```yaml
action: done
summary: "Brief summary of what was accomplished"
```

**Default to `continue` in almost all cases.** Only use `revise` when
something went WRONG — the step produced an error, returned unexpected
results that invalidate the remaining plan, or a critical assumption
changed.  Do NOT revise just to add "nice to have" steps, polish, or
steps you think were missing.  A revision should have fewer or equal
steps compared to what it replaces.  Fewer steps is better.

Use `ask_user` only when you genuinely need user input that you cannot
infer from context.
""")

        return "\n".join(parts)

    def _parse_review_response(self, response):
        """Parse the orchestrator's YAML review response."""
        if not response:
            return None

        fenced = re.search(r"```ya?ml\s*\n(.*?)```", response, re.DOTALL)
        if fenced:
            try:
                data = yaml.safe_load(fenced.group(1))
                if isinstance(data, dict) and "action" in data:
                    return self._action_from_dict(data)
            except yaml.YAMLError:
                pass

        # Unfenced: extract from "action:" to the next blank line.
        # Without this boundary, yaml.safe_load would try to parse trailing
        # prose as YAML, causing a YAMLError that silently swallows the
        # action (e.g. "done" gets lost and defaults to "continue").
        block = re.search(
            r"^(action:.*?)(?=\n[ \t]*\n|\Z)",
            response, re.MULTILINE | re.DOTALL,
        )
        if block:
            try:
                data = yaml.safe_load(block.group(1))
                if isinstance(data, dict) and "action" in data:
                    return self._action_from_dict(data)
            except yaml.YAMLError:
                pass

        # No structured action found — return None (caller defaults to
        # continue).  Previously this checked for the word "continue"
        # anywhere in the response, but that matched false positives like
        # "before I can continue, I need..." causing premature progression.
        return None

    def _action_from_dict(self, data):
        """Convert a parsed YAML dict to an action."""
        action = data.get("action")
        if action == "continue":
            return {"type": "continue"}
        elif action == "done":
            return {
                "type": "done",
                "summary": data.get("summary", "Task complete"),
            }
        elif action == "ask_user":
            question = data.get("question")
            if not question:
                return {"type": "continue"}
            return {"type": "ask_user", "question": question}
        elif action == "revise":
            plan_data = data.get("plan")
            if not plan_data or not isinstance(plan_data, list):
                return {"type": "continue"}
            steps = []
            for entry in plan_data:
                step = self._parse_step(entry)
                if step is None:
                    return {"type": "continue"}
                steps.append(step)
            # Renumber revised steps sequentially from last completed.
            last_completed = (
                max(self._completed_steps.keys())
                if self._completed_steps else 0
            )
            for i, step in enumerate(steps):
                step.number = last_completed + i + 1
            return {"type": "revise", "steps": steps}
        return None

    # ------------------------------------------------------------------
    # Context file management
    # ------------------------------------------------------------------

    # Maximum characters to store in analysis files.
    _MAX_ANALYSIS_LEN = 8000

    # Regex pattern matching the bare prompt line that aider prints
    # before each command.  Everything before the first prompt is startup
    # chrome; everything after the last ``Tokens:`` line is trailing chrome.
    _PROMPT_RE = re.compile(r"^(?:\S+(?:\s+\S+)?\s*)?>\s*$")

    # Lines to strip from the body (between first prompt and end).
    _BODY_CHROME = re.compile(
        r"^("
        r"─{3,}"                         # horizontal rules
        r"|Tokens: "                      # token usage
        r"|Saved .+ files? to"            # /save ctx output
        r")",
    )

    @classmethod
    def _strip_chrome(cls, text, commands=None):
        """Remove aider UI chrome from subprocess output.

        Strategy: split the output on prompt lines (``>``, ``diff>``,
        etc.) into sections — one per command.  Discard everything
        before the first prompt (startup banner).  Within each section,
        strip horizontal rules, token counts, and /save ctx output.

        When *commands* is provided, each section is prefixed with
        ``> /command`` so the analysis file shows what was run.  The
        first and last commands (auto-added ``/load ctx`` and
        ``/save ctx``) are omitted from the display.
        """
        lines = text.splitlines()

        # Split output into sections delimited by prompt lines.
        # Section 0 = startup (discarded), section N = output after
        # the Nth prompt.
        sections = []
        current = []
        found_prompt = False
        for line in lines:
            if cls._PROMPT_RE.match(line.strip()):
                found_prompt = True
                sections.append(current)
                current = []
            else:
                current.append(line)
        sections.append(current)  # trailing content after last prompt

        # Section 0 is startup chrome — discard it, but only if a
        # prompt was found.  If no prompt exists, keep everything.
        if found_prompt:
            body_sections = sections[1:]
        else:
            body_sections = sections

        # Strip body chrome from each section.
        cleaned_sections = []
        for section_lines in body_sections:
            cleaned = []
            for line in section_lines:
                if cls._BODY_CHROME.match(line.strip()):
                    continue
                cleaned.append(line)
            cleaned_sections.append("\n".join(cleaned).strip())

        # Interleave commands with their output sections.
        if commands:
            # Auto-added /load ctx and /save ctx are infrastructure —
            # show user commands only but still include their output.
            _AUTO_CMD = re.compile(r"^/(load|save) ctx\b")
            result_parts = []
            for i, section_text in enumerate(cleaned_sections):
                if i < len(commands):
                    cmd = commands[i]
                    if not _AUTO_CMD.match(cmd):
                        result_parts.append(f"> {cmd}")
                if section_text:
                    result_parts.append(section_text)
            return "\n".join(result_parts).strip()

        return "\n\n".join(s for s in cleaned_sections if s).strip()

    def _save_analysis(self, step):
        """Write the step's text result to an analysis file on disk.

        Strips aider UI chrome (banners, context displays, token counts)
        and interleaves the slash commands with their output so the
        analysis file reads like a transcript.
        """
        if not step.result or _step_failed(step):
            return
        commands = getattr(step, "_script_commands", None)
        # For parallel steps, join sub-results
        if isinstance(step.result, list):
            parts = []
            for i, r in enumerate(step.result):
                if r and not (isinstance(r, str) and r.startswith("ERROR:")):
                    parts.append(f"--- Sub-task {i} ---\n{self._strip_chrome(r)}")
            result_text = "\n\n".join(parts)
        else:
            result_text = self._strip_chrome(str(step.result), commands=commands)
        if not result_text.strip():
            return
        result_text = _truncate(result_text, self._MAX_ANALYSIS_LEN)
        path = self._analysis_path(step.number, step.description)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(result_text)

    def _build_before_context(self, step):
        """Build a before-context file from the immediate prior step.

        Returns the absolute path to the before-context YAML file, or
        ``None`` if there is no completed prior step.

        Only the most recent successful step's context is loaded because
        ``/save ctx`` already captures cumulative file context — each
        step's after-context includes files from all prior steps.  The
        analysis file from the prior step is added as read-only context.
        """
        if not self._completed_steps:
            return None

        # Find the most recent successful step.
        prev_step = None
        for num in sorted(self._completed_steps, reverse=True):
            candidate = self._completed_steps[num]
            if not _step_failed(candidate):
                prev_step = candidate
                break

        if prev_step is None:
            return None

        editable = set()
        read_only = set()
        root = self._get_repo_root()

        # Load file context from the immediate prior step.
        after_ctx = self._after_context_path(
            prev_step.number, prev_step.description
        )
        if os.path.isfile(after_ctx):
            with open(after_ctx, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data:
                editable.update(data.get("editable", []))
                read_only.update(data.get("read_only", []))

        # Add the prior step's analysis as read-only context.
        analysis = self._analysis_path(
            prev_step.number, prev_step.description
        )
        if os.path.isfile(analysis):
            rel = os.path.relpath(analysis, root)
            read_only.add(rel)

        if not editable and not read_only:
            return None

        slug = self._slugify(step.description)
        before_path = os.path.join(
            self._step_dir(step.number),
            f"_before_context_{slug}.yml",
        )
        data = {"editable": sorted(editable), "read_only": sorted(read_only)}
        with open(before_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

        return before_path

    def _merge_parallel_contexts(self, step):
        """Merge ``_after_context`` files from parallel sub-steps into one."""
        editable = set()
        read_only = set()
        slug = self._slugify(step.description)

        for i in range(len(step.parallel)):
            sub_dir = self._step_dir(step.number, sub_id=i)
            ctx_path = os.path.join(sub_dir, f"_after_context_{slug}.yml")
            if os.path.isfile(ctx_path):
                with open(ctx_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data:
                    editable.update(data.get("editable", []))
                    read_only.update(data.get("read_only", []))

        merged_path = self._after_context_path(step.number, step.description)
        data = {"editable": sorted(editable), "read_only": sorted(read_only)}
        os.makedirs(os.path.dirname(merged_path), exist_ok=True)
        with open(merged_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def _cleanup_run_dir(self):
        """Remove the ``agents/{run_id}/`` directory unless DEBUG is set."""
        if os.environ.get("DEBUG"):
            self.io.tool_output(
                f"  DEBUG: keeping agent working directory: "
                f"agents/{self._run_id}/"
            )
            return
        if self._run_dir and os.path.isdir(self._run_dir):
            shutil.rmtree(self._run_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _prompt_user(self, question):
        """Get user input, supporting both web and terminal IO."""
        if hasattr(self.io, "prompt_ask"):
            return self.io.prompt_ask(question)
        # Fallback: use builtins input for terminal
        try:
            import builtins
            return builtins.input(question + " ")
        except (EOFError, KeyboardInterrupt):
            return ""

    def _execute_ask_user(self, step):
        """Pause and ask the user a question.

        Re-prompts up to ``MAX_ASK_USER_RETRIES`` times if the user gives
        an empty response.  If no answer is obtained, marks the step result
        as an error so dependent steps can detect the failure.
        """
        for attempt in range(1 + MAX_ASK_USER_RETRIES):
            answer = self._prompt_user(step.ask_user)
            if answer and answer.strip():
                self.user_answers[step.number] = answer.strip()
                step.result = answer.strip()
                self.io.tool_output(f"  User answered: {answer.strip()}")
                return

            if attempt < MAX_ASK_USER_RETRIES:
                self.io.tool_warning(
                    "  No answer provided.  Please answer the question "
                    "or the step will be skipped."
                )

        # Exhausted retries — mark as failed
        self.io.tool_warning(
            f"  Step {step.number}: no user answer after "
            f"{1 + MAX_ASK_USER_RETRIES} attempts, skipping."
        )
        step.result = "ERROR: no user answer provided"

    def _execute_script(self, step):
        """Execute a multi-command script in an isolated subprocess.

        Context flows automatically between sequential steps:
        - Before-context (from prior step) is loaded via ``/load ctx``.
        - After-context is saved via ``/save ctx`` at the end.
        - Analysis results are written to disk after the subprocess returns.
        """
        root = self._get_repo_root()
        script = []

        # Auto-load before-context from the immediate prior step.
        before_ctx = self._build_before_context(step)
        if before_ctx:
            rel_path = os.path.relpath(before_ctx, root)
            script.append(f"/load ctx {rel_path}")
            self.io.tool_output(f"  Loading context from prior step")

        # Count only user-provided commands for the display message.
        user_cmd_count = len(step.commands)

        for cmd in step.commands:
            cmd = self._interpolate(cmd.strip())
            if not cmd.startswith("/"):
                self.io.tool_warning(
                    f"  Step {step.number}: bare text wrapped as "
                    f"/query: {_truncate(cmd, 60)}"
                )
                cmd = "/query " + cmd
            script.append(cmd)

        # Auto-save context at the end so future steps can load it.
        after_path = self._after_context_path(step.number, step.description)
        rel_after = os.path.relpath(after_path, root)
        script.append(f"/save ctx {rel_after}")

        # When DEBUG is set, write a full I/O log for inspection.
        log_path = None
        if os.environ.get("DEBUG"):
            slug = self._slugify(step.description)
            log_path = os.path.join(
                self._step_dir(step.number), f"_log_{slug}.txt"
            )

        # Store the full script on the step so _save_analysis can
        # interleave commands with their output sections.
        step._script_commands = list(script)

        step.result = self._run_subprocess(
            script, log_path=log_path, user_cmd_count=user_cmd_count
        )

        # Save analysis (the text result) to disk.
        self._save_analysis(step)

        # Show a preview of the step result in the logs.
        if step.result and not _step_failed(step):
            cleaned = self._strip_chrome(str(step.result), commands=script)
            preview = _truncate(cleaned, 300)
            if preview.strip():
                self.io.tool_output(f"  Result: {preview}")

    def _execute_parallel(self, step):
        """Execute parallel scripts concurrently in subprocesses.

        Each script runs in its own aider subprocess for true isolation
        of ``/add``, ``/drop``, and git state.  Context flows via files:
        before-context is loaded from dependencies, after-context is saved
        per sub-task and merged at the end.
        """
        root = self._get_repo_root()
        slug = self._slugify(step.description)

        # Build before-context once (shared by all parallel scripts).
        before_ctx = self._build_before_context(step)

        scripts = []
        for i, raw_script in enumerate(step.parallel):
            sub_script = []

            # Load before-context from dependencies.
            if before_ctx:
                rel_path = os.path.relpath(before_ctx, root)
                sub_script.append(f"/load ctx {rel_path}")

            for cmd in raw_script:
                sub_script.append(self._interpolate(cmd))

            # Save after-context to sub-directory.
            sub_dir = self._step_dir(step.number, sub_id=i)
            after_path = os.path.join(sub_dir, f"_after_context_{slug}.yml")
            rel_after = os.path.relpath(after_path, root)
            sub_script.append(f"/save ctx {rel_after}")

            scripts.append(sub_script)

        # When DEBUG is set, compute a log path for each sub-task.
        debug = os.environ.get("DEBUG")
        log_paths = []
        for i in range(len(scripts)):
            if debug:
                sub_dir = self._step_dir(step.number, sub_id=i)
                log_paths.append(
                    os.path.join(sub_dir, f"_log_{slug}.txt")
                )
            else:
                log_paths.append(None)

        total = len(scripts)
        completed_count = 0
        self._emit("parallel_progress", {
            "step": step.number,
            "completed": 0,
            "total": total,
        })

        with ThreadPoolExecutor(max_workers=total) as executor:
            future_to_idx = {
                executor.submit(
                    self._run_subprocess, s, log_path=log_paths[i]
                ): i
                for i, s in enumerate(scripts)
            }
            results = [None] * total
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    results[idx] = f"ERROR: {exc}"
                completed_count += 1
                self._emit("parallel_progress", {
                    "step": step.number,
                    "completed": completed_count,
                    "total": total,
                })

        step.result = results

        # Merge sub-step contexts into a single after-context for this step.
        self._merge_parallel_contexts(step)

        # Save analysis (combined parallel results) to disk.
        self._save_analysis(step)

    # ------------------------------------------------------------------
    # Subprocess execution (used by both sequential and parallel steps)
    # ------------------------------------------------------------------

    def _build_subprocess_args(self):
        """Build the command-line arguments for an aider subprocess.

        Subagents always use:
        - Direct autonomy (L1) — the default, no flag needed.
        - Auto-context enabled — picked up from ``.composez`` config.
        - No auto-lint — ``--no-auto-lint``.
        - No auto-commits — ``--no-auto-commits``.
        """
        args = [
            sys.executable, "-m", "aider",
            "--yes-always",
            "--no-auto-commits",
            "--no-auto-lint",
            "--auto-context",
            "--no-fancy-input",
            "--no-show-fnames",
            "--no-stream",
        ]

        # Pass through the model so the subprocess uses the same LLM
        model = getattr(self.coder, "main_model", None)
        if model:
            model_name = getattr(model, "name", None) or str(model)
            args.extend(["--model", model_name])

        return args

    def _get_repo_root(self):
        """Return the working directory for subprocesses."""
        root = getattr(self.coder, "root", None)
        if root:
            return str(root)
        return os.getcwd()

    def _run_subprocess(self, script, timeout=600, log_path=None,
                        user_cmd_count=None):
        """Run a script of slash commands in an isolated aider subprocess.

        Parameters
        ----------
        script : list[str]
            Slash commands to run, one per line.
        timeout : int
            Maximum seconds to wait (default 600 = 10 min).
        log_path : str or None
            When set, write the full I/O transcript (input commands and
            subprocess output) to this file.  Typically used when
            ``DEBUG=1`` is set.
        user_cmd_count : int or None
            Number of user-specified commands (excludes auto-added
            ``/load ctx`` and ``/save ctx``).  Falls back to
            ``len(script)`` if not provided.

        Returns
        -------
        str or None
            The subprocess stdout (combined with stderr), or an error
            string starting with ``ERROR:``.
        """
        args = self._build_subprocess_args()
        commands_input = "\n".join(script) + "\n"

        display_count = user_cmd_count if user_cmd_count is not None else len(script)
        self.io.tool_output(
            f"  Launching subprocess: {display_count} command(s)"
        )

        try:
            proc = subprocess.run(
                args,
                input=commands_input,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self._get_repo_root(),
            )

            output = proc.stdout or ""
            if proc.stderr:
                output = (output + "\n" + proc.stderr).strip()

            if proc.returncode != 0:
                result = f"ERROR: subprocess exited with code {proc.returncode}\n{output}"
            else:
                result = output.strip() if output else None

        except subprocess.TimeoutExpired:
            result = f"ERROR: subprocess timed out after {timeout}s"
        except Exception as exc:
            result = f"ERROR: subprocess failed: {exc}"

        # Write full I/O transcript when a log path is provided.
        if log_path:
            self._write_log(log_path, commands_input, result)

        return result

    def _write_log(self, log_path, commands_input, result):
        """Write a debug log file with the full subprocess I/O."""
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("=== INPUT (commands piped to subprocess) ===\n")
                f.write(commands_input)
                f.write("\n=== OUTPUT ===\n")
                f.write(str(result) if result else "(no output)")
                f.write("\n")
        except Exception:
            pass  # Don't let logging failures break execution

    def _interpolate(self, text):
        """Replace ``{answer:N}`` placeholders with user answers."""
        def _replace(m):
            step_num = int(m.group(1))
            return self.user_answers.get(
                step_num, f"(no answer for step {step_num})"
            )

        return re.sub(r"\{answer:(\d+)\}", _replace, text)


def _step_failed(step):
    """Return True if a step's result indicates failure."""
    if step.result is None:
        return False
    if isinstance(step.result, str) and step.result.startswith("ERROR:"):
        return True
    # Parallel steps: check if any sub-result failed
    if isinstance(step.result, list):
        return any(
            isinstance(r, str) and r.startswith("ERROR:")
            for r in step.result
        )
    return False


def _truncate(text, max_len):
    """Truncate *text* to *max_len* characters."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# Review coder system prompt
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """\
You are a plan review agent.  Your ONLY job is to review step results from
an executing plan and decide what to do next.

You will receive:
- A summary of completed steps and their results
- The latest step's full result
- The remaining plan steps

You must respond with EXACTLY ONE YAML code block containing an action.
Do NOT ask questions, do NOT explain your reasoning at length, do NOT
request files or context.  Just output the YAML action block.

Valid actions:

```yaml
action: continue
```

```yaml
action: done
summary: "Brief summary of what was accomplished"
```

```yaml
action: ask_user
question: "A specific, answerable question"
```

```yaml
action: revise
plan:
  - step: N
    description: "..."
    commands:
      - "/add ..."
      - "/write ..."
```

Rules:
- **Default to ``continue``** — this should be your answer in the vast
  majority of cases.  If the step succeeded, continue.
- Use ``revise`` ONLY when something went wrong — an error occurred, results
  are clearly incorrect, or a core assumption changed.  Do NOT revise to add
  polish steps, "nice to have" improvements, or steps you think were missing.
  A revision should have fewer or equal steps compared to what it replaces.
- Use ``ask_user`` ONLY when you genuinely need user input you cannot infer.
- Use ``done`` when the task is complete or should be abandoned.
- Your response must contain a fenced ```yaml block.  Nothing else matters.
"""
