"""
Novel mode activation — applies prose-oriented prompts and narrative file
validation to any coder, regardless of edit format.

Instead of a dedicated NovelCoder subclass (which would lock you into one edit
format), novel mode is a lightweight overlay:

- **Prompts**: content-specific prompts (main_system, lazy_prompt, etc.) come
  from ``NovelPrompts``; format-specific prompts (system_reminder,
  example_messages) stay from the underlying coder so diff / udiff / whole /
  etc. all work correctly.
- **Validator**: ``edit_path_validator`` is set to
  ``NarrativeMap.check_narrative_file`` so that narrative level file
  constraints are enforced in ``prepare_to_edit()`` for every edit format.
  The validator automatically propagates across format switches via
  ``Coder.create(from_coder=...)``.
"""

import os
import re
import types

from pathlib import Path

from .config import NOVEL_DIR, get_auto_context, resolve_model_for_role
from .db import Db
from .narrative_map import NarrativeMap
from .novel_context_prompts import NovelContextPrompts
from .novel_prompts import (
    NovelAgentPrompts,
    NovelComposePrompts,
    NovelPrompts,
    NovelQueryPrompts,
)


class NovelPromptOverlay:
    """Wraps a coder's prompts, overlaying novel-specific content prompts.

    Format-specific prompts (``system_reminder``, ``example_messages``) come
    from the *original* coder so the edit format instructions stay correct.
    Everything else comes from ``NovelPrompts`` if it defines the attribute,
    or falls back to the original.

    Pass a *novel_prompts* instance to override which novel prompts are used
    (e.g. ``NovelComposePrompts()`` for the compose planning phase).
    """

    _FORMAT_SPECIFIC = frozenset({"system_reminder", "example_messages"})

    def __init__(self, original_prompts, novel_prompts=None):
        object.__setattr__(self, "_original", original_prompts)
        object.__setattr__(self, "_novel", novel_prompts or NovelPrompts())

    def __getattr__(self, name):
        # Format-specific prompts always come from the original coder
        if name in self._FORMAT_SPECIFIC:
            return getattr(self._original, name)
        # If the novel prompts class hierarchy explicitly defines this
        # attribute (up to but not including CoderPrompts), use it.
        from aider.coders.base_prompts import CoderPrompts

        for cls in type(self._novel).__mro__:
            if cls is CoderPrompts or cls is object:
                break
            if name in cls.__dict__:
                return getattr(self._novel, name)
        # Otherwise fall back to the original coder's prompts
        return getattr(self._original, name)


def collapse_paths(root, rel_paths):
    """Collapse file paths into parent directories when all entries are present.

    If every file and subdirectory inside a directory on disk is represented
    in the set, replace them with just the directory path.  Repeats
    recursively so that fully-covered subtrees bubble up to a single entry.

    Parameters
    ----------
    root : str
        Absolute path to the project root (used for ``os.listdir``).
    rel_paths : iterable of str
        Relative paths (from *root*) to consider.

    Returns
    -------
    list of str
        Sorted collapsed paths.  Directory entries have a trailing ``/``.
    """
    result = set(rel_paths)
    changed = True
    while changed:
        changed = False
        parents = {}
        for p in list(result):
            parent = os.path.dirname(p)
            if parent:
                parents.setdefault(parent, set()).add(p)

        for parent_rel, children_in_set in parents.items():
            full_parent = os.path.join(root, parent_rel)
            if not os.path.isdir(full_parent):
                continue
            try:
                entries = os.listdir(full_parent)
            except OSError:
                continue
            all_children = set()
            for name in entries:
                if name.startswith("."):
                    continue
                all_children.add(os.path.join(parent_rel, name))
            if all_children and all_children == children_in_set:
                result -= children_in_set
                result.add(parent_rel)
                changed = True
    return sorted(
        p + "/" if os.path.isdir(os.path.join(root, p)) else p
        for p in result
    )


def _novel_file_sort_key(abs_path, root):
    """Return a sort key that groups files: non-narrative → summaries → prose.

    Within each group files are sorted alphabetically by relative path.
    """
    basename = os.path.basename(abs_path)

    if basename == NarrativeMap.SUMMARY_FILE:
        return (1, os.path.relpath(abs_path, root))
    if basename == NarrativeMap.PROSE_FILE:
        return (2, os.path.relpath(abs_path, root))
    return (0, os.path.relpath(abs_path, root))


def _install_file_sorting(coder):
    """Monkey-patch file iteration so the LLM sees files in narrative order.

    Order: non-narrative files (db entries, etc.) → summaries → prose.
    This ensures the LLM has the full structural outline before prose content.
    """
    root = coder.root

    def _sorted_get_abs_fnames_content(self):
        sorted_fnames = sorted(
            list(self.abs_fnames),
            key=lambda f: _novel_file_sort_key(f, root),
        )
        for fname in sorted_fnames:
            content = self.io.read_text(fname)
            if content is None:
                relative_fname = self.get_rel_fname(fname)
                self.io.tool_warning(f"Dropping {relative_fname} from the chat.")
                self.abs_fnames.remove(fname)
            else:
                yield fname, content

    def _sorted_get_read_only_files_content(self):
        from aider.utils import is_image_file

        prompt = ""
        sorted_fnames = sorted(
            self.abs_read_only_fnames,
            key=lambda f: _novel_file_sort_key(f, root),
        )
        for fname in sorted_fnames:
            content = self.io.read_text(fname)
            if content is not None and not is_image_file(fname):
                relative_fname = self.get_rel_fname(fname)
                prompt += "\n"
                prompt += relative_fname
                prompt += f"\n{self.fence[0]}\n"
                prompt += content
                prompt += f"{self.fence[1]}\n"
        return prompt

    coder.get_abs_fnames_content = types.MethodType(
        _sorted_get_abs_fnames_content, coder
    )
    coder.get_read_only_files_content = types.MethodType(
        _sorted_get_read_only_files_content, coder
    )


def _ensure_metadata(coder):
    """Create ``db/core/metadata.yml`` if it does not already exist.

    Uses the repository directory name as the title and the git user name
    (from ``git config user.name``) as the author.
    """
    import yaml

    root = getattr(coder, "root", None)
    if not root:
        return

    metadata_path = os.path.join(root, "db", "core", "metadata.yml")
    if os.path.isfile(metadata_path):
        return

    # Title: basename of the project root
    title = os.path.basename(os.path.abspath(root)) or "Untitled"

    # Author: git user.name, or "Unknown"
    author = "Unknown"
    repo = getattr(coder, "repo", None)
    if repo:
        git_repo = getattr(repo, "repo", None)
        if git_repo:
            try:
                author = git_repo.git.config("--get", "user.name") or "Unknown"
            except Exception:
                pass

    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
    data = {"title": title, "author": author}
    Path(metadata_path).write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def _override_main_model(coder, root):
    """Override ``coder.main_model`` with the edit_model from ``.composez``.

    When ``.composez`` specifies an ``edit_model``, it becomes the coder's
    main model so that all editing tasks use the configured model rather
    than whatever was passed on the command line.

    If the model's provider requires API keys that aren't available in the
    environment, the override is skipped with a warning.
    """
    edit_model_name = resolve_model_for_role(root, "edit_model")
    if not edit_model_name:
        return

    from aider import models as _models

    model = _models.Model(edit_model_name)

    if model.missing_keys:
        io = getattr(coder, "io", None)
        if io:
            missing = ", ".join(model.missing_keys)
            io.tool_warning(
                f"Skipping edit_model override ({edit_model_name}): "
                f"missing API key(s): {missing}"
            )
        return

    coder.main_model = model


def _install_admin_model_for_commits(coder, root):
    """Override git commit message models with the admin_model from ``.composez``.

    If ``admin_model`` is configured, the repo's model list (used for
    generating commit messages) is replaced with just that model, keeping
    commit-message generation fast and cheap.
    """
    admin_model_name = resolve_model_for_role(root, "admin_model")
    if not admin_model_name:
        return

    repo = getattr(coder, "repo", None)
    if not repo:
        return

    from aider import models as _models

    admin = _models.Model(admin_model_name)
    repo.models = [admin]


def activate_novel_mode(coder):
    """Apply novel editing behaviour to any coder instance.

    - Overlays prose-oriented prompts while preserving the coder's native
      edit-format instructions (system_reminder, example_messages).
    - Sets narrative file validation so that only allowed files can be
      edited inside the narrative tree.
    - Auto-creates SUMMARY.md and PROSE.md without prompting the user.
    - Installs a display formatter so the prompt file listing uses
      collapsed directory paths instead of individual files.
    - Sorts files in the LLM prompt: non-narrative → summaries → prose.
    - Works with every edit format: whole, diff, diff-fenced, udiff, etc.
    """
    # Use compose-specific planning prompts when the coder is in compose mode.
    strategy_name = getattr(
        getattr(coder, "autonomy_strategy", None), "name", "direct"
    )
    if strategy_name == "compose":
        novel_prompts = NovelComposePrompts()
    else:
        novel_prompts = None  # default NovelPrompts
    coder.gpt_prompts = NovelPromptOverlay(coder.gpt_prompts, novel_prompts)
    coder.auto_create_fnames = {NarrativeMap.SUMMARY_FILE, NarrativeMap.PROSE_FILE}

    # Disable the code-oriented repo map — it uses PageRank on code
    # symbols which is meaningless for prose.  Novel mode relies on
    # auto-context and the narrative map instead.
    coder.repo_map = None

    # Ensure db/core/metadata.yml exists (first-run init).
    root = getattr(coder, "root", None)

    # Create a NarrativeMap instance for file validation (needs levels config)
    novel_root = os.path.join(root, NOVEL_DIR) if root else "."
    nmap = NarrativeMap(novel_root)
    coder.edit_path_validator = nmap.check_narrative_file

    if root:
        _ensure_metadata(coder)

    # Always load db/core/ entries as read-only context.
    if root and os.path.isdir(os.path.join(root, Db.DB_DIR)):
        load_core_context(coder)

    # Sort files in narrative order for the LLM prompt.
    if root:
        _install_file_sorting(coder)

    # Collapse fully-covered directories in the prompt file listing.
    if root:
        _install_display_formatter(coder, root)

    # Override main_model with edit_model from .composez if configured.
    if root:
        _override_main_model(coder, root)

    # Override git commit message model with admin_model if configured.
    if root:
        _install_admin_model_for_commits(coder, root)

    # Apply .composez auto_lint setting (unless overridden by CLI).
    if root:
        _apply_auto_lint_config(coder, root)

    # Register Vale as the markdown linter so /lint still works.
    _register_vale_linter(coder)

    # Install novel auto-lint: runs before commit, filters to changed lines,
    # auto-reflects without prompting.
    _install_novel_auto_lint(coder)

    # Auto-run `git mv` commands suggested by the LLM.
    _install_auto_git_mv(coder)

    # Install auto-context pre-LLM hook (unless this is already a context coder).
    if getattr(coder, "edit_format", None) != "context":
        _install_auto_context(coder)


def load_core_context(coder):
    """Add all ``db/core/`` entries to the coder's read-only context."""
    root = getattr(coder, "root", None)
    if not root:
        return
    db = Db(root)
    for path in db.get_core_context_paths():
        abs_path = os.path.abspath(path)
        if os.path.isfile(abs_path):
            coder.abs_read_only_fnames.add(abs_path)


def _install_display_formatter(coder, root):
    """Set ``display_fnames_formatter`` on the coder's io so the prompt file
    listing collapses fully-covered directories."""

    def _formatter(rel_fnames, rel_read_only_fnames):
        ro_set = set(rel_read_only_fnames or [])
        editable = [f for f in rel_fnames if f not in ro_set]
        read_only = [f for f in rel_fnames if f in ro_set]
        collapsed_editable = collapse_paths(root, editable)
        collapsed_read_only = collapse_paths(root, read_only)
        new_all = sorted(set(collapsed_editable + collapsed_read_only))
        return new_all, collapsed_read_only

    coder.io.display_fnames_formatter = _formatter


def _is_git_mv_only(cmd_block):
    """Return True if every non-blank, non-comment line is a ``git mv`` command."""
    lines = [
        line.strip()
        for line in cmd_block.strip().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return bool(lines) and all(line.startswith("git mv ") for line in lines)


def _apply_auto_lint_config(coder, root):
    """Apply the ``.composez`` ``auto_lint`` setting to the coder.

    Reads ``.composez`` and sets ``coder._novel_auto_lint`` accordingly.
    The novel auto-lint system uses this flag instead of the base
    ``auto_lint`` attribute (which is set to ``False`` to prevent
    the base linting flow from running).
    """
    from .config import get_auto_lint

    coder._novel_auto_lint = get_auto_lint(root)


def _register_vale_linter(coder):
    """Register Vale as the markdown linter for auto-lint.

    After this call, aider's built-in ``auto_lint`` will run Vale on
    ``.md`` files whenever the LLM edits them, showing warnings and
    offering to fix them automatically.
    """
    if not hasattr(coder, "linter"):
        return

    from composez_core.vale_linter import make_markdown_linter, vale_available

    if not vale_available():
        if not getattr(_register_vale_linter, "_warned", False):
            _register_vale_linter._warned = True
            io = getattr(coder, "io", None)
            if io and getattr(coder, "auto_lint", False):
                io.tool_warning(
                    "Vale is not installed — prose linting is disabled. "
                    "Install it with: pip install vale"
                )
        return

    if not hasattr(coder, "_novel_lint_level"):
        coder._novel_lint_level = "warning"

    markdown_lint = make_markdown_linter(coder.root, coder)
    coder.linter.set_linter("markdown", markdown_lint)


def _get_changed_lines(repo, fnames):
    """Return a dict mapping absolute file paths to sets of changed line numbers.

    Uses ``git diff`` (unstaged changes vs HEAD) to determine which lines
    were added or modified.  Only files in *fnames* are included.
    """
    if not repo:
        return {}

    import re as _re

    try:
        diff_output = repo.repo.git.diff(
            "HEAD", "--unified=0", "--", *fnames,
            stdout_as_string=False,
        ).decode("utf-8", "replace")
    except Exception:
        return {}

    changed = {}
    current_file = None
    hunk_re = _re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    for line in diff_output.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file:
            m = hunk_re.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                if current_file not in changed:
                    changed[current_file] = set()
                changed[current_file].update(range(start, start + count))

    return changed


def _install_novel_auto_lint(coder):
    """Install novel-specific auto-lint that runs before commit.

    Disables the base ``auto_lint`` flow and instead wraps
    ``auto_commit`` to:

    1. Get the git diff to find changed lines per file.
    2. Run Vale on all edited prose files in a single invocation.
    3. Filter results to only changed lines.
    4. If issues are found, skip the commit and auto-reflect so the
       LLM fixes them — no confirmation prompt.
    5. On the next round (after the fix), commit everything together.
    """
    from .vale_linter import ValeLinter, vale_available

    if not vale_available():
        return

    original_auto_commit = type(coder).auto_commit  # unbound

    _LINT_LEVELS = {
        "error": {"error"},
        "warning": {"error", "warning"},
        "suggestion": {"error", "warning", "suggestion"},
    }

    def _auto_commit_with_lint(self, edited, context=None):
        # Only intercept the first commit (the LLM edit), not the
        # "Ran the linter" follow-up or other contexts.
        if context is not None:
            return original_auto_commit(self, edited, context=context)

        if not getattr(self, "_novel_auto_lint", True):
            return original_auto_commit(self, edited)

        if self.edit_format in ("query", "selection"):
            return original_auto_commit(self, edited)

        # Find prose files among the edited set.
        root = getattr(self, "root", None) or ""
        prose_fnames = []
        for f in edited:
            if f and f.endswith(".md"):
                abs_f = self.abs_root_path(f) if hasattr(self, "abs_root_path") else f
                prose_fnames.append(abs_f)

        if not prose_fnames:
            return original_auto_commit(self, edited)

        # 1. Get changed lines from git diff (before committing).
        changed_lines = _get_changed_lines(self.repo, prose_fnames)
        if not changed_lines:
            # New files or no repo — lint everything, don't filter.
            changed_lines = None

        # 2. Run Vale on all prose files in one invocation.
        vale = ValeLinter(root=root)
        batch = vale.lint_files(prose_fnames)

        if not batch:
            self.io.tool_output("Lint clean.")
            return original_auto_commit(self, edited)

        # 3. Filter to changed lines and current severity level.
        level = getattr(self, "_novel_lint_level", "warning")
        severities = _LINT_LEVELS.get(level, {"error", "warning"})

        all_errors = []
        for fname, result in batch.items():
            filtered_warnings = [
                w for w in result.warnings
                if w["severity"] in severities
            ]
            if changed_lines is not None:
                rel = os.path.relpath(fname, root) if root else fname
                lines_set = changed_lines.get(rel, set())
                filtered_warnings = [
                    w for w in filtered_warnings
                    if w["line"] in lines_set
                ]
            if filtered_warnings:
                rel = vale._rel(fname)
                text = vale._format_warnings(rel, filtered_warnings)
                all_errors.append(text)

        if not all_errors:
            self.io.tool_output("Lint clean.")
            return original_auto_commit(self, edited)

        # 4. Issues found — show them, skip the commit, auto-reflect.
        n = len(prose_fnames)
        label = "file" if n == 1 else "files"
        self.io.tool_output(f"Running Vale on {n} prose {label}...")

        error_text = "\n".join(all_errors)
        self.io.tool_warning(error_text)

        self.reflected_message = (
            "# Fix any errors below, if possible.\n\n" + error_text
        )
        self.lint_outcome = False

        # Return the saved_message the base flow expects so that
        # move_back_cur_messages still works.
        if hasattr(self.gpt_prompts, "files_content_gpt_edits_no_repo"):
            return self.gpt_prompts.files_content_gpt_edits_no_repo
        return None

    coder.auto_commit = types.MethodType(_auto_commit_with_lint, coder)
    # Track the novel auto-lint state separately and disable the base
    # auto_lint so lines 1794-1802 in send_message don't double-run.
    coder._novel_auto_lint = True
    coder.auto_lint = False


def _install_auto_git_mv(coder):
    """Wrap ``run_shell_commands`` so that ``git mv`` commands auto-run.

    Any shell command block consisting entirely of ``git mv`` lines is
    executed immediately via the ``/git`` command infrastructure and removed
    from the list before the base method runs, which avoids the interactive
    confirmation prompt for these safe rename operations.
    """
    original = type(coder).run_shell_commands  # unbound class method

    def _run_shell_commands(self):
        auto = []
        remaining = []
        for cmd_block in self.shell_commands:
            if _is_git_mv_only(cmd_block):
                auto.append(cmd_block)
            else:
                remaining.append(cmd_block)

        for cmd_block in auto:
            for line in cmd_block.strip().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip "git " prefix — cmd_git prepends it back.
                git_args = line[len("git "):]
                self.commands.cmd_git(git_args)

        self.shell_commands = remaining
        return original(self)

    coder.run_shell_commands = types.MethodType(_run_shell_commands, coder)


# ------------------------------------------------------------------
# Auto-context: automatically identify relevant files before each LLM call
# ------------------------------------------------------------------

MAX_PREVIEW_WORDS = 50


def build_db_listing(root):
    """Build a text listing of every db entry with a preview of its contents.

    Each entry is shown as ``db/<category>/<name>.md — <first 50 words>``.
    """
    db = Db(root)
    entries = db.get_entries()
    if not entries:
        return "(no db entries)"

    lines = []
    for entry in entries:
        rel_path = os.path.relpath(entry.path, root)
        content = entry.content.strip()
        words = content.split()
        preview = " ".join(words[:MAX_PREVIEW_WORDS])
        if len(words) > MAX_PREVIEW_WORDS:
            preview += " …"
        lines.append(f"- ``{rel_path}`` — {preview}")

    return "\n".join(lines)


def _extract_backtick_paths(content, root):
    """Extract file paths from backtick-delimited strings in *content*.

    ``get_file_mentions`` splits on whitespace, which breaks paths that
    contain spaces (e.g. ``novel/Act 1 - Title/…``).  This helper pulls
    out backtick-wrapped strings and resolves them against *root* to find
    real files.  Returns a set of absolute paths.
    """
    found = set()
    for match in re.findall(r"`([^`]+)`", content):
        candidate = match.strip()
        if not candidate:
            continue
        abs_path = os.path.normpath(os.path.join(root, candidate))
        if os.path.isfile(abs_path):
            found.add(abs_path)
    return found


def _space_aware_get_file_mentions(self, content, ignore_current=False):
    """Extended ``get_file_mentions`` that also handles paths with spaces.

    The upstream ``get_file_mentions`` splits on whitespace, so a path like
    ``novel/Act 1 - Title/Chapter 1/PROSE.md`` is shattered into fragments
    that never match.  This override calls the original method (which
    handles simple paths and basenames) then does substring matching for
    any repo file whose relative path contains a space.
    """
    from aider.coders.base_coder import Coder

    # Original method handles paths without spaces and unique basenames
    result = Coder.get_file_mentions(self, content, ignore_current)

    # Additionally, do substring matching for paths that contain spaces
    if ignore_current:
        addable = self.get_all_relative_files()
    else:
        addable = self.get_addable_relative_files()

    normalized_content = content.replace("\\", "/")
    for rel_fname in addable:
        if " " not in rel_fname:
            continue  # Original method handles these fine
        normalized = rel_fname.replace("\\", "/")
        if normalized in normalized_content:
            result.add(rel_fname)

    return result


def _reply_completed_no_reflect(self):
    """Replacement for ``ContextCoder.reply_completed`` — adds files, no reflection.

    The original ``reply_completed`` ties adding files to ``abs_fnames``
    with starting a reflection round.  When ``max_reflections`` is low
    (e.g. 1), it skips both — the guard ``num_reflections >= max_reflections - 1``
    evaluates to ``0 >= 0`` on the very first call and returns early, so no
    files are ever collected.

    This override decouples the two: files mentioned in the LLM response
    are added to ``abs_fnames``, but ``reflected_message`` is never set
    so no reflection round is triggered.
    """
    content = self.partial_response_content
    if not content or not content.strip():
        return True

    mentioned = set(self.get_file_mentions(content, ignore_current=True))
    self.abs_fnames = set()
    for fname in mentioned:
        self.add_rel_fname(fname)

    return True


def run_auto_context(coder, user_message):
    """Run context analysis and temporarily add identified files.

    Spins up a disposable ``ContextCoder`` using the **weak model**,
    with novel-aware prompts that list all db entries.  Returns the set
    of absolute paths that were identified so they can be removed after
    the main LLM call completes.
    """
    from aider.coders.base_coder import Coder

    root = getattr(coder, "root", None)
    if not root:
        return set()

    # Build the db listing for the prompt.
    # Detect query/agent mode so we request "files to examine" instead of
    # "files to modify" — otherwise the LLM correctly says nothing needs
    # modification and auto-context adds no files.
    autonomy_name = getattr(getattr(coder, "autonomy_strategy", None), "name", "direct")
    query_mode = getattr(coder, "edit_format", None) == "query" or autonomy_name == "agent"
    db_listing = build_db_listing(root)
    prompts = NovelContextPrompts(db_listing=db_listing, query_mode=query_mode)

    # Use the admin model for context analysis (cheaper, faster).
    # Prefer the .composez admin_model; fall back to weak_model.
    admin_model_name = resolve_model_for_role(root, "admin_model")
    if admin_model_name:
        from aider import models as _models

        context_model = _models.Model(admin_model_name)
    else:
        context_model = coder.main_model.weak_model

    # Create a minimal context coder — no chat history, no summarization.
    # Uses the real IO (not a wrapper) because Coder.__init__ relies on
    # IO internals for repo and file tracking initialisation.
    ctx_coder = Coder.create(
        main_model=context_model,
        edit_format="context",
        io=coder.io,
        from_coder=coder,
        summarize_from_coder=False,
        stream=False,
    )

    # Override prompts with our novel-aware version
    ctx_coder.gpt_prompts = prompts

    # Clear chat history — context mode is stateless
    ctx_coder.done_messages = []
    ctx_coder.cur_messages = []

    # Coder.create() calls activate_novel_mode() which installs
    # auto-context on EVERY coder — including this ctx_coder.
    # Disable it to prevent infinite recursion (ctx_coder trying to
    # run auto-context on itself, creating another ctx_coder, etc.).
    ctx_coder._auto_context_enabled = False

    # One round is enough — the LLM lists relevant files on the first
    # try; the default 3-reflection loop wastes tokens.
    ctx_coder.max_reflections = 1

    # Suppress the "Tokens: …" cost report from the ctx_coder
    ctx_coder.show_usage_report = lambda: None

    # Monkey-patch get_file_mentions so the ContextCoder's own reflection
    # loop can find paths with spaces (e.g. novel/Act 1 - Title/…).
    ctx_coder.get_file_mentions = types.MethodType(
        _space_aware_get_file_mentions, ctx_coder
    )

    # Override reply_completed to add files without triggering reflection.
    # The default ContextCoder.reply_completed() ties file addition to the
    # reflection mechanism — with max_reflections=1 it skips both, leaving
    # abs_fnames empty.  This override decouples the two.
    ctx_coder.reply_completed = types.MethodType(
        _reply_completed_no_reflect, ctx_coder
    )

    # Temporarily suppress the ctx_coder's LLM response display.
    # We mute assistant_output on the shared IO for the duration of
    # the run, then restore it.  This is safe because the run is
    # synchronous — nothing else uses the IO concurrently.
    _orig_assistant_output = coder.io.assistant_output
    coder.io.assistant_output = lambda *a, **kw: None
    try:
        ctx_coder.run(with_message=user_message, preproc=False)
    finally:
        coder.io.assistant_output = _orig_assistant_output

    # Collect the files the context coder identified
    identified = set(ctx_coder.abs_fnames) | set(ctx_coder.abs_read_only_fnames)

    # Only add files that aren't already in the main coder's context
    already_present = coder.abs_fnames | coder.abs_read_only_fnames
    new_files = identified - already_present

    if new_files:
        rel_names = sorted(os.path.relpath(f, root) for f in new_files)
        coder.io.tool_output(
            "Auto-context added: " + ", ".join(rel_names)
        )
        # In edit mode, add as editable so the LLM can modify them.
        # In query/agent mode, add as read-only reference context.
        target_set = coder.abs_read_only_fnames if query_mode else coder.abs_fnames
        for f in new_files:
            target_set.add(f)

    return new_files


def _maybe_run_auto_context(self, user_message):
    """Run auto-context if enabled, returning the set of added files.

    Shared helper used by both the ``run_one`` and ``run_stream`` wrappers
    installed by :func:`_install_auto_context`.
    """
    if not getattr(self, "_auto_context_enabled", False):
        return set()
    if not user_message or self.commands.is_command(user_message):
        return set()
    try:
        return run_auto_context(self, user_message)
    except Exception as e:
        self.io.tool_warning(f"Auto-context failed: {e}")
        return set()


def _install_auto_context(coder):
    """Monkey-patch ``run_one`` and ``run_stream`` to run auto-context before
    the main LLM call.

    In edit mode, identified files are added as editable (``abs_fnames``) so
    the LLM can modify them.  In query/agent mode they are added as read-only
    context.  Files are removed after the call so they don't accumulate.

    Both entry points are patched because the CLI uses ``run_one`` (via
    ``run``) while the web UI uses ``run_stream``.
    """
    root = getattr(coder, "root", None)
    if not root:
        io = getattr(coder, "io", None)
        if io and hasattr(io, "tool_warning"):
            io.tool_warning("Auto-context: skipped (no root directory)")
        return

    # CLI --auto-context / --no-auto-context overrides .composez.
    cli_override = getattr(coder, "auto_context", None)
    if cli_override is not None:
        coder._auto_context_enabled = cli_override
    else:
        coder._auto_context_enabled = get_auto_context(root)

    original_run_one = type(coder).run_one  # unbound

    def _run_one_with_auto_context(self, user_message, preproc):
        added_files = _maybe_run_auto_context(self, user_message)

        try:
            return original_run_one(self, user_message, preproc)
        finally:
            # Remove temporarily added files (from whichever set they were added to)
            if added_files:
                self.abs_fnames -= added_files
                self.abs_read_only_fnames -= added_files

    coder.run_one = types.MethodType(_run_one_with_auto_context, coder)

    original_run_stream = type(coder).run_stream  # unbound

    def _run_stream_with_auto_context(self, user_message):
        added_files = _maybe_run_auto_context(self, user_message)

        try:
            yield from original_run_stream(self, user_message)
        finally:
            if added_files:
                self.abs_fnames -= added_files
                self.abs_read_only_fnames -= added_files

    coder.run_stream = types.MethodType(_run_stream_with_auto_context, coder)


def activate_novel_query_mode(coder):
    """Apply novel query/feedback behaviour to any coder instance.

    Replaces prompts entirely with editorial/analysis prompts.  Since query
    mode produces no edits, there are no format-specific prompts to preserve.
    """
    coder.gpt_prompts = NovelQueryPrompts()
    coder.repo_map = None
    _install_auto_context(coder)


def activate_novel_agent_mode(coder):
    """Apply novel agent behaviour to any coder instance.

    Replaces prompts with agent-specific prompts that know about the novel
    structure and available commands.  Like query mode, agent mode produces no
    direct edits — it orchestrates other coders via slash commands.

    Auto-context is **disabled** for the orchestrator — it only produces YAML
    plans and doesn't need file context.  Auto-context runs inside the
    subprocesses where it's actually useful.
    """
    coder.gpt_prompts = NovelAgentPrompts()
    coder.repo_map = None
    coder._auto_context_enabled = False
