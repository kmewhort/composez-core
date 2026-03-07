"""
Novel-specific commands for the aider chat interface.

Adds commands for navigating narrative structure, managing the reference db,
viewing word counts, prose linting, and more.
"""

import os
import re
import shutil
from pathlib import Path

import pyperclip
import yaml

from .db import Db, DEFAULT_CATEGORIES
from .config import NOVEL_DIR, get_levels
from .narrative_map import (
    NarrativeMap,
    _NUM_TITLE_RE,
    _build_level_re,
    make_titled_dir,
    natural_sort_key,
    parse_level_dir,
)
from .vale_linter import ValeLinter


# ------------------------------------------------------------------
# Public helpers — used by NovelCommands and the novel_ui server
# ------------------------------------------------------------------

def _extract_text(content):
    """Extract plain text from a message content field (string or multipart list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content) if content else ""


def _format_message(msg):
    """Format a single message dict as a labelled transcript block."""
    role = msg.get("role", "unknown").upper()
    text = _extract_text(msg.get("content", ""))
    if not text.strip():
        return ""
    return f"## {role}\n\n{text}"


def build_copy_context_markdown(coder, continue_only=False, extra=""):
    """Build the full context transcript that mirrors what the LLM actually sees.

    Parameters
    ----------
    coder : Coder
        The active coder instance.
    continue_only : bool
        If True, only include chunks after ``done`` (i.e. the current
        conversation turn: chat_files, cur, reminder).  This is useful for
        continuing a conversation in a web UI without re-sending the cached
        history.
    extra : str
        Optional extra instruction text appended at the end.

    Returns
    -------
    str
        A markdown-formatted transcript ready to paste into a web UI.
    """
    chunks = coder.format_chat_chunks()

    if continue_only:
        # Only the "live" portion: chat_files, cur, reminder
        message_groups = [
            chunks.chat_files,
            chunks.cur,
            chunks.reminder,
        ]
    else:
        # Full context in the order the LLM sees it
        message_groups = [
            chunks.system,
            chunks.examples,
            chunks.readonly_files,
            chunks.repo,
            chunks.done,
            chunks.chat_files,
            chunks.cur,
            chunks.reminder,
        ]

    blocks = []
    for group in message_groups:
        for msg in group:
            block = _format_message(msg)
            if block:
                blocks.append(block)

    markdown = "\n\n".join(blocks)

    if extra:
        markdown += f"\n\n{extra}"

    return markdown


def build_copy_context_human(coder, extra=""):
    """Build lightweight context matching the original upstream /copy-context.

    Only includes *user* messages from ``repo``, ``readonly_files`` and
    ``chat_files`` — no system prompts, examples, or conversation history.
    This is the minimal "here are my files, tell me what to change" payload.

    Parameters
    ----------
    coder : Coder
        The active coder instance.
    extra : str
        Optional extra instruction text appended at the end.

    Returns
    -------
    str
        A markdown-formatted context string.
    """
    chunks = coder.format_chat_chunks()

    markdown = ""
    for group in [chunks.repo, chunks.readonly_files, chunks.chat_files]:
        for msg in group:
            if msg["role"] != "user":
                continue
            text = _extract_text(msg.get("content", ""))
            if text.strip():
                markdown += text + "\n\n"

    markdown += (
        "Just tell me how to edit the files to make the changes.\n"
        "Don't give me back entire files.\n"
        "Just show me the edits I need to make.\n"
    )

    if extra:
        markdown += f"\n{extra}\n"

    return markdown


def apply_pasted_response(coder, response_text):
    """Apply a pasted LLM response as if the model had returned it.

    This feeds the text through the coder's normal edit-application pipeline:
    ``partial_response_content`` → ``apply_updates`` → auto-commit / lint.
    """
    # Record the response as if the LLM produced it
    coder.partial_response_content = response_text
    coder.partial_response_function_call = []

    # Add it to cur_messages so the conversation history stays consistent
    coder.add_assistant_reply_to_cur_messages()

    # Show the response to the user
    if hasattr(coder, "io"):
        coder.io.ai_output(response_text)

    # Apply edits (the same path as send_message's post-response handling)
    try:
        edited = coder.apply_updates()
    except Exception as err:
        coder.io.tool_error(f"Error applying edits: {err}")
        return

    if edited:
        coder.aider_edited_files.update(edited)
        saved_message = coder.auto_commit(edited)
        if not saved_message and hasattr(coder.gpt_prompts, "files_content_gpt_edits_no_repo"):
            saved_message = coder.gpt_prompts.files_content_gpt_edits_no_repo
        coder.move_back_cur_messages(saved_message)

        # Run linter if configured (only in edit mode, not query/selection)
        if coder.auto_lint and getattr(coder, "edit_format", None) not in ("query", "selection"):
            lint_errors = coder.lint_edited(edited)
            coder.auto_commit(edited, context="Ran the linter")
            if lint_errors:
                coder.io.tool_warning("Lint errors found in pasted response edits.")
                coder.io.tool_output(lint_errors)
    else:
        coder.io.tool_output("No edits found in the pasted response.")


class NovelCommands:
    """Novel-specific commands that extend aider's command system.

    These are designed to be mixed into or called from the main Commands class.
    Each ``cmd_*`` method follows aider's command convention:
    - Takes a single ``args`` string parameter
    - Uses ``self.io`` for output
    - Uses ``self.coder`` for access to the coder state
    """

    def __init__(self, io, coder, root=None, parent_commands=None):
        self.io = io
        self.coder = coder
        self.root = root or (coder.root if coder else ".")
        self._parent_commands = parent_commands

        self._narrative_map = None
        self._db = None
        self._vale_linter = None
        self._lint_level = "warning"  # default: show errors + warnings

    @property
    def novel_root(self):
        """The directory containing narrative content (``<root>/novel/``)."""
        return os.path.join(self.root, NOVEL_DIR)

    @property
    def levels(self):
        return get_levels(self.root)

    @property
    def narrative_map(self):
        if self._narrative_map is None:
            self._narrative_map = NarrativeMap(
                self.novel_root, levels=self.levels, io=self.io,
            )
        return self._narrative_map

    @property
    def db(self):
        if self._db is None:
            self._db = Db(self.root, io=self.io)
        return self._db

    @property
    def vale_linter(self):
        if self._vale_linter is None:
            self._vale_linter = ValeLinter(root=self.root)
        return self._vale_linter

    # ------------------------------------------------------------------
    # Narrative structure commands
    # ------------------------------------------------------------------

    def cmd_wordcount(self, args):
        """Show word counts for each narrative level"""
        counts = self.narrative_map.get_word_counts()
        if counts["total"] == 0:
            self.io.tool_output("No content found. Total word count: 0")
            return

        levels = counts.get("levels", self.levels)
        lines = [f"Total word count: {counts['total']:,}\n"]

        def _render_level(items, depth):
            if depth >= len(levels):
                return
            level_name = levels[depth]
            level_key = level_name.lower()
            child_key = (
                levels[depth + 1].lower() + "s" if depth + 1 < len(levels) else None
            )
            indent = "  " * depth
            is_leaf = depth == len(levels) - 1
            for item in items:
                label = f"{indent}{level_name} {item[level_key]}"
                if item.get("title"):
                    label += f": {item['title']}"
                if is_leaf:
                    label += f": {item.get('words', 0):,} words"
                else:
                    label += f": {item.get('total', 0):,} words"
                lines.append(label)
                if child_key and child_key in item:
                    _render_level(item[child_key], depth + 1)

        _render_level(counts.get("acts", []), 0)
        self.io.tool_output("\n".join(lines))

    # ------------------------------------------------------------------
    # /add override — handle "db" prefix for read-only reference entries
    # ------------------------------------------------------------------

    def cmd_add(self, args):
        """Add scenes, chapters, or db entries to the chat context.

        Usage:
            /add db                          — add all db entries
            /add db sarah                    — add entry 'sarah'
            /add db characters               — add all character entries
            /add summaries                   — add all SUMMARY.md files
            /add summaries act 1 chapter 2   — add SUMMARY.md files under chapter 2
            /add prose                       — add all PROSE.md files
            /add prose act 1 chapter 2       — add PROSE.md files under chapter 2
            /add act 1 chapter 2             — add all files for chapter 2
            /add <file> ...                  — add files (same as base /add)
        """
        stripped = args.strip()
        if not stripped:
            if self._parent_commands:
                return self._parent_commands.cmd_add(args)
            self.io.tool_error("No parent commands available to handle /add.")
            return

        tokens = stripped.split()
        keyword = tokens[0].lower()

        if keyword == "db":
            rest = " ".join(tokens[1:]).strip()
            return self._add_db(rest)

        if keyword == "summaries":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                return self._add_all_summaries()
            parsed = self._parse_location_args(rest)
            if parsed is not None:
                return self._add_narrative(parsed, "summary")
            self.io.tool_error(f"Could not parse location: {rest}")
            return

        if keyword == "prose":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                return self._add_all_prose()
            parsed = self._parse_location_args(rest)
            if parsed is not None:
                return self._add_narrative(parsed, "prose")
            self.io.tool_error(f"Could not parse location: {rest}")
            return

        # Try as a bare narrative location (no summary/prose prefix)
        parsed = self._parse_location_args(stripped)
        if parsed is not None:
            return self._add_narrative_all(parsed)

        # Delegate to the base /add command
        if self._parent_commands:
            return self._parent_commands.cmd_add(args)

        self.io.tool_error("No parent commands available to handle /add.")

    def _add_db(self, args):
        """Add db entries to the chat."""
        if not args:
            # /add db — add everything
            paths = self.db.get_all_context_paths()
            if not paths:
                self.io.tool_warning("No db entries found.")
                return
            added = set()
            for path in paths:
                abs_path = os.path.abspath(path)
                if self.coder:
                    self.coder.abs_fnames.add(abs_path)
                added.add(os.path.relpath(path, self.root) if self.root else path)
            self._report_added(added)
            return

        # Try as a specific entry name first
        entry = self.db.get_entry(args)
        if entry:
            abs_path = os.path.abspath(entry.path)
            if self.coder:
                self.coder.abs_fnames.add(abs_path)
            rel = os.path.relpath(entry.path, self.root) if self.root else entry.path
            self._report_added({rel})
            return

        # Try as a category
        entries = self.db.get_entries_by_category(args)
        if entries:
            added = set()
            for entry in entries:
                abs_path = os.path.abspath(entry.path)
                if self.coder:
                    self.coder.abs_fnames.add(abs_path)
                added.add(os.path.relpath(entry.path, self.root) if self.root else entry.path)
            self._report_added(added)
            return

        self.io.tool_error(f"No db entry or category matching '{args}' found.")

    def _add_all_prose(self):
        """Add all PROSE.md files from the entire novel."""
        tree = self.narrative_map.get_tree()
        if not tree:
            self.io.tool_warning("No narrative structure found.")
            return

        paths = []
        for node in tree:
            paths.extend(self._collect_prose_files(node))

        if not paths:
            self.io.tool_warning("No PROSE.md files found.")
            return

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        added = set()
        for path in paths:
            abs_path = os.path.abspath(path)
            self.coder.abs_fnames.add(abs_path)
            added.add(os.path.relpath(path, self.root) if self.root else path)
        self._report_added(added)

    def _add_narrative(self, nums, add_type):
        """Add narrative files to the chat.

        For 'summary': for leaf nodes adds SUMMARY.md; for non-leaf nodes
            adds any .md files in the directory.
        For 'prose': adds all PROSE.md files from leaf nodes under the specified node.
        """
        node = self._resolve_node(nums)
        if node is None:
            return

        paths = []

        label = self._node_label(node)

        if add_type == "summary":
            if node.is_leaf:
                summary_path = os.path.join(node.path, NarrativeMap.SUMMARY_FILE)
                if os.path.isfile(summary_path):
                    paths.append(summary_path)
                else:
                    self.io.tool_error(f"No SUMMARY.md found for {label}.")
                    return
            else:
                # Non-leaf: collect SUMMARY.md from all descendant leaf nodes
                paths = self._collect_summary_files(node)
                if not paths:
                    self.io.tool_error(f"No SUMMARY.md files found under {label}.")
                    return

        elif add_type == "prose":
            # Collect all PROSE.md files from scenes under this node
            paths = self._collect_prose_files(node)
            if not paths:
                self.io.tool_error(f"No PROSE.md files found under {label}.")
                return

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        added = set()
        for path in paths:
            abs_path = os.path.abspath(path)
            self.coder.abs_fnames.add(abs_path)
            added.add(os.path.relpath(path, self.root) if self.root else path)
        self._report_added(added)

    def _collect_prose_files(self, node):
        """Collect all PROSE.md paths from leaf nodes under a node."""
        paths = []
        if node.is_leaf:
            prose_path = os.path.join(node.path, NarrativeMap.PROSE_FILE)
            if os.path.isfile(prose_path):
                paths.append(prose_path)
        else:
            for child in node.children:
                paths.extend(self._collect_prose_files(child))
        return paths

    def _collect_summary_files(self, node):
        """Collect all SUMMARY.md paths from leaf nodes under a node."""
        paths = []
        if node.is_leaf:
            summary_path = os.path.join(node.path, NarrativeMap.SUMMARY_FILE)
            if os.path.isfile(summary_path):
                paths.append(summary_path)
        else:
            for child in node.children:
                paths.extend(self._collect_summary_files(child))
        return paths

    def _add_all_summaries(self):
        """Add all SUMMARY.md files from the entire novel."""
        tree = self.narrative_map.get_tree()
        if not tree:
            self.io.tool_warning("No narrative structure found.")
            return

        paths = []
        for node in tree:
            paths.extend(self._collect_summary_files(node))

        if not paths:
            self.io.tool_warning("No SUMMARY.md files found.")
            return

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        added = set()
        for path in paths:
            abs_path = os.path.abspath(path)
            self.coder.abs_fnames.add(abs_path)
            added.add(os.path.relpath(path, self.root) if self.root else path)
        self._report_added(added)

    def _add_narrative_all(self, nums):
        """Add all narrative files (summaries + prose) for a location."""
        node = self._resolve_node(nums)
        if node is None:
            return

        paths = self._collect_all_narrative_files(node)
        if not paths:
            label = self._node_label(node)
            self.io.tool_error(f"No narrative files found for {label}.")
            return

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        added = set()
        for path in paths:
            abs_path = os.path.abspath(path)
            self.coder.abs_fnames.add(abs_path)
            added.add(os.path.relpath(path, self.root) if self.root else path)
        self._report_added(added)

    def _collect_all_narrative_files(self, node):
        """Collect narrative files from a node and its descendants.

        For leaf nodes: SUMMARY.md + PROSE.md.
        For non-leaf nodes: any .md files in the directory + all descendant files.
        """
        paths = []
        if node.is_leaf:
            summary_path = os.path.join(node.path, NarrativeMap.SUMMARY_FILE)
            if os.path.isfile(summary_path):
                paths.append(summary_path)
            prose_path = os.path.join(node.path, NarrativeMap.PROSE_FILE)
            if os.path.isfile(prose_path):
                paths.append(prose_path)
        else:
            # Non-leaf: include any .md files the user has placed here
            if os.path.isdir(node.path):
                for name in sorted(os.listdir(node.path)):
                    if name.endswith(".md"):
                        fpath = os.path.join(node.path, name)
                        if os.path.isfile(fpath):
                            paths.append(fpath)
        for child in node.children:
            paths.extend(self._collect_all_narrative_files(child))
        return paths

    def completions_add(self):
        """Return completions for /add command."""
        return ["db", "summaries", "prose"] + [l.lower() for l in self.levels]

    # ------------------------------------------------------------------
    # New command
    # ------------------------------------------------------------------

    def cmd_new(self, args):
        """Create new narrative nodes, db entries or instructions.

        Usage:
            /new <level_name> [title]                — next number, auto-scaffold
            /new act N chapter N scene N [title]      — specific target
            /new N N N [title]                        — shorthand for specific target
            /new instruction <name>
            /new db <category> <name>
            /new db/<category>/<name>.md              — path-style db entry

        Shorthand (use numbers for location, then the level to create):
            /new 1 chapter [title]          — new chapter in act 1
            /new 1 2 scene [title]          — new scene in act 1, chapter 2

        Specific targeting (all levels have numbers):
            /new act 1 chapter 1 scene 3 My Title
            /new 1 1 3 My Title
            /new act 1 chapter 2 "My Title"   — creates chapter, no auto-scene
        """
        args = args.strip()
        if not args:
            levels = self.levels
            lines = ["Usage: /new <level> [title]"]
            for i, level in enumerate(levels):
                if i == 0:
                    lines.append(f"  /new {level.lower()} [title]")
                else:
                    parents = " ".join(
                        f"{levels[j].lower()} N" for j in range(i)
                    )
                    lines.append(f"  /new {parents} {level.lower()} N [title]")
            lines.append("  /new instruction <name>")
            lines.append("  /new db <category> <name>")
            lines.append("  /new db/<category>/<name>.md")
            self.io.tool_error("\n".join(lines))
            return

        tokens = args.split()

        # Handle "instruction <name>" before the general parser
        if tokens[0].lower() == "instruction":
            name = " ".join(tokens[1:]).strip()
            if not name:
                self.io.tool_error("Usage: /new instruction <name>")
                return
            return self._create_instruction(name)

        # Handle "db <category> <name>" or "db/<category>/<name>"
        if tokens[0].lower() == "db":
            if len(tokens) < 3:
                self.io.tool_error("Usage: /new db <category> <name>")
                return
            category = tokens[1]
            name = " ".join(tokens[2:])
            return self._create_db_entry(category, name)

        # Handle path-style db entry: "db/characters/tom.md"
        if tokens[0].lower().startswith("db/"):
            parts = tokens[0].split("/", 2)
            if len(parts) < 3 or not parts[1] or not parts[2]:
                self.io.tool_error(
                    "Usage: /new db/<category>/<name>.md"
                )
                return
            category = parts[1]
            name = parts[2]
            return self._create_db_entry(category, name)

        parsed = self._parse_new_args(args)
        if parsed is None:
            return  # error already reported

        depth, parent_nums, target_num, title = parsed
        return self._create_node(depth, parent_nums, title, target_num=target_num)

    @staticmethod
    def _strip_quotes(title):
        """Strip surrounding quotes from a title string."""
        if title and len(title) >= 2:
            if (title[0] == '"' and title[-1] == '"') or (
                title[0] == "'" and title[-1] == "'"
            ):
                title = title[1:-1]
        return title

    @staticmethod
    def _strip_level_prefix(title, level_name):
        """Remove a leading ``Level N -`` prefix from *title*.

        Users sometimes type ``/new 1 1 3 "Scene 3 - It Begins"`` which would
        produce ``Scene 3 - Scene 3 - It Begins``.  This strips the redundant
        prefix so only ``It Begins`` survives.
        """
        if not title:
            return title
        # Match "Level N - ...", "Level N ...", case-insensitive
        pattern = re.compile(
            rf"^{re.escape(level_name)}\s+\d+\s*(?:-\s*)?",
            re.IGNORECASE,
        )
        stripped = pattern.sub("", title).strip()
        return stripped or title  # fall back to original if nothing left

    def _parse_new_args(self, args):
        """Parse /new arguments.

        Returns ``(depth, parent_nums, target_num, title)`` or ``None`` on
        error.

        * ``depth`` — level index to create at.
        * ``parent_nums`` — ordinal numbers for ancestor levels.
        * ``target_num`` — explicit ordinal for the target level, or ``None``
          to auto-increment.
        * ``title`` — human-readable title or ``None``.

        When every level keyword in the input has a number, the deepest
        numbered level is the *target* (specific creation).  When the last
        keyword has no number, it is created at the next available number
        (auto-increment).  Pure-number shorthand (``1 2 3 Title``) always
        targets a specific node.
        """
        tokens = args.split()
        levels = self.levels
        level_names = [l.lower() for l in levels]
        pos = 0
        parsed_levels = []  # list of (level_idx, number_or_None)

        # --- Try keyword syntax: consume "[level [N]]..." ---
        if tokens[0].lower() in level_names:
            while pos < len(tokens) and tokens[pos].lower() in level_names:
                level_idx = level_names.index(tokens[pos].lower())
                pos += 1
                # Is the next token a number? (location specifier)
                if pos < len(tokens) and tokens[pos].isdigit():
                    parsed_levels.append((level_idx, int(tokens[pos])))
                    pos += 1
                else:
                    # Level name with no number = create at this depth
                    parsed_levels.append((level_idx, None))
                    break

            last_idx, last_num = parsed_levels[-1]
            if last_num is None:
                # Last keyword has no number → auto-increment (old behaviour)
                parent_nums = [n for _, n in parsed_levels[:-1]]
                title = " ".join(tokens[pos:]) or None
                title = self._strip_quotes(title) if title else None
                return (last_idx, parent_nums, None, title)
            else:
                # All keywords had numbers → deepest is the target
                parent_nums = [n for _, n in parsed_levels[:-1]]
                title = " ".join(tokens[pos:]) or None
                title = self._strip_quotes(title) if title else None
                return (last_idx, parent_nums, last_num, title)

        # --- Shorthand: leading numbers then optionally a keyword ---
        if tokens[0].isdigit():
            nums = []
            while pos < len(tokens) and tokens[pos].isdigit():
                nums.append(int(tokens[pos]))
                pos += 1

            if pos < len(tokens) and tokens[pos].lower() in level_names:
                target_level = tokens[pos].lower()
                target_depth = level_names.index(target_level)
                pos += 1

                # Check if the keyword is followed by a number (specific target)
                if pos < len(tokens) and tokens[pos].isdigit():
                    target_num = int(tokens[pos])
                    pos += 1
                    title = " ".join(tokens[pos:]) or None
                    title = self._strip_quotes(title) if title else None
                    # nums are parents, keyword+number is the target
                    if len(nums) != target_depth:
                        self.io.tool_error(
                            f"Expected {target_depth} parent number(s) for "
                            f"a {levels[target_depth].lower()}, got {len(nums)}."
                        )
                        return None
                    return (target_depth, nums, target_num, title)

                # Keyword with no number → auto-increment (old behaviour)
                title = " ".join(tokens[pos:]) or None
                title = self._strip_quotes(title) if title else None

                if target_depth == 0 and nums:
                    self.io.tool_error(
                        f"Cannot specify location numbers when creating "
                        f"a {levels[0].lower()}."
                    )
                    return None
                if len(nums) > target_depth:
                    self.io.tool_error(
                        f"Too many location numbers for creating a "
                        f"{levels[target_depth].lower()}."
                    )
                    return None
                return (target_depth, nums, None, title)

            # Only numbers (+ optional title) — specific target.
            if len(nums) > len(levels):
                self.io.tool_error("Too many location numbers.")
                return None
            title = " ".join(tokens[pos:]) or None
            title = self._strip_quotes(title) if title else None
            if len(nums) == 0:
                self.io.tool_error("No location numbers provided.")
                return None
            target_depth = len(nums) - 1
            parent_nums = nums[:-1]
            target_num = nums[-1]
            return (target_depth, parent_nums, target_num, title)

        self.io.tool_error(
            "Usage: /new <level> [title] or /new <nums...> [title]"
        )
        return None

    def completions_new(self):
        """Return completions for /new command."""
        return [l.lower() for l in self.levels] + ["instruction", "db"]

    def completions_raw_new(self, document, complete_event):
        """Context-aware completions for /new.

        After ``/new db``, complete with db category names.
        """
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        words = text.split()

        # "/new db <partial>" — offer category names
        if len(words) >= 3 and words[1].lower() == "db" and not text[-1].isspace():
            partial = words[-1].lower()
            categories = self._db_categories()
            for cat in sorted(categories):
                if cat.lower().startswith(partial):
                    yield Completion(cat, start_position=-len(words[-1]))
            return

        # "/new db " (trailing space) — offer all category names
        if len(words) == 2 and words[1].lower() == "db" and text[-1].isspace():
            for cat in sorted(self._db_categories()):
                yield Completion(cat)
            return

        # First argument — offer the standard keywords
        if len(words) <= 2 and not (len(words) == 2 and text[-1].isspace()):
            partial = words[-1].lower() if len(words) > 1 else ""
            kws = [l.lower() for l in self.levels] + ["instruction", "db"]
            for kw in kws:
                if kw.startswith(partial):
                    yield Completion(kw, start_position=-len(partial))

    def _db_categories(self):
        """Return existing db category directory names + defaults."""
        categories = set(DEFAULT_CATEGORIES)
        db_path = os.path.join(self.root, "db")
        if os.path.isdir(db_path):
            for item in sorted(os.listdir(db_path)):
                if os.path.isdir(os.path.join(db_path, item)):
                    categories.add(item)
        return sorted(categories)

    def _create_node(self, depth, parent_nums, title, target_num=None):
        """Create a new node at the given depth under the specified parents.

        Parameters
        ----------
        depth : int
            Index into ``self.levels`` (0 = top level).
        parent_nums : list[int]
            Ordinal numbers for each ancestor level.  May be shorter than
            *depth*; missing ancestors default to the last existing node.
        title : str or None
            Human-readable title.
        target_num : int or None
            When given, create the node at this specific ordinal number
            (instead of auto-incrementing).  A warning is emitted if a
            node with that number already exists.  When a specific target
            is given, child nodes are **not** auto-scaffolded.
        """
        levels = self.levels
        level_name = levels[depth]
        is_leaf = depth == len(levels) - 1
        specific = target_num is not None

        # Navigate to the parent directory, creating missing ancestors when
        # a specific target was requested.
        parent_dir = self.novel_root
        for i in range(depth):
            ancestor_level = levels[i]
            if i < len(parent_nums):
                num = parent_nums[i]
            else:
                # Default to the last (highest-numbered) ancestor
                num = self._find_last_level_number(parent_dir, ancestor_level)
                if num is None:
                    self.io.tool_error(
                        f"No {ancestor_level.lower()}s found. "
                        f"Create one first with /new {ancestor_level.lower()}"
                    )
                    return
            ancestor_dir = self._find_level_dir(parent_dir, ancestor_level, num)
            if not ancestor_dir:
                if specific:
                    # Auto-create the missing ancestor directory
                    anc_dir_name = make_titled_dir(ancestor_level, num)
                    ancestor_dir = os.path.join(parent_dir, anc_dir_name)
                    os.makedirs(ancestor_dir, exist_ok=True)
                    anc_rel = os.path.relpath(ancestor_dir, self.root)
                    self.io.tool_output(f"Created {anc_rel}/")
                else:
                    self.io.tool_error(f"{ancestor_level} {num} not found.")
                    return
            parent_dir = ancestor_dir

        # Determine the ordinal number for the new node
        if specific:
            # Check if it already exists
            existing = self._find_level_dir(parent_dir, level_name, target_num)
            if existing:
                rel = os.path.relpath(existing, self.root)
                self.io.tool_warning(f"{level_name} {target_num} already exists: {rel}/")
                return
            node_num = target_num
        else:
            node_num = self._next_level_number(parent_dir, level_name)

        title = self._strip_level_prefix(title, level_name)
        dir_name = make_titled_dir(level_name, node_num, title)
        node_dir = os.path.join(parent_dir, dir_name)
        os.makedirs(node_dir, exist_ok=True)

        if is_leaf:
            summary_path = os.path.join(node_dir, "SUMMARY.md")
            Path(summary_path).write_text(
                (title or "Untitled") + "\n", encoding="utf-8"
            )
            prose_path = os.path.join(node_dir, "PROSE.md")
            self._touch(prose_path)

        rel = os.path.relpath(node_dir, self.root)
        self.io.tool_output(f"Created {rel}/")

        if self.coder and is_leaf:
            self.coder.abs_fnames.add(os.path.abspath(summary_path))
            self.coder.abs_fnames.add(os.path.abspath(prose_path))

        # Auto-create an initial leaf node inside non-leaf containers,
        # but only when auto-incrementing (not for specific targets).
        if not is_leaf and not specific:
            # Build the path down to the leaf level, creating intermediate
            # directories as needed.
            cursor = node_dir
            for mid in range(depth + 1, len(levels) - 1):
                mid_level = levels[mid]
                mid_dir_name = make_titled_dir(mid_level, 1)
                cursor = os.path.join(cursor, mid_dir_name)
                os.makedirs(cursor, exist_ok=True)
                mid_rel = os.path.relpath(cursor, self.root)
                self.io.tool_output(f"Created {mid_rel}/")

            leaf_level = levels[-1]
            leaf_dir_name = make_titled_dir(leaf_level, 1)
            leaf_dir = os.path.join(cursor, leaf_dir_name)
            os.makedirs(leaf_dir, exist_ok=True)
            leaf_rel = os.path.relpath(leaf_dir, self.root)
            self.io.tool_output(f"Created {leaf_rel}/")

            summary_path = os.path.join(leaf_dir, "SUMMARY.md")
            Path(summary_path).write_text("Untitled\n", encoding="utf-8")
            prose_path = os.path.join(leaf_dir, "PROSE.md")
            self._touch(prose_path)

            if self.coder:
                self.coder.abs_fnames.add(os.path.abspath(summary_path))
                self.coder.abs_fnames.add(os.path.abspath(prose_path))

        self._narrative_map = None

    def _create_db_entry(self, category, name):
        """Create a db entry."""
        from .db import CORE_CATEGORY

        entry = self.db.create_entry(category, name)
        self.io.tool_output(f"Created db entry: {entry.path}")

        abs_path = os.path.abspath(entry.path)
        if self.coder:
            if category.lower() == CORE_CATEGORY:
                # Core entries are always read-only context
                self.coder.abs_read_only_fnames.add(abs_path)
                self.io.tool_output(
                    f"Added to context as read-only: {entry.category}/{entry.name}"
                )
            else:
                # Other entries are editable so the user/LLM can fill them in
                self.coder.abs_fnames.add(abs_path)
                self.io.tool_output(
                    f"Added to chat for editing: {entry.category}/{entry.name}"
                )

    def _create_instruction(self, name):
        """Create a new instruction file in the instructions/ directory."""
        instructions_dir = os.path.join(self.root, "instructions")
        os.makedirs(instructions_dir, exist_ok=True)

        # Ensure .md extension
        if not name.endswith(".md") and not name.endswith(".txt"):
            name = name + ".md"

        path = os.path.join(instructions_dir, name)
        if os.path.exists(path):
            self.io.tool_error(f"Instruction already exists: {path}")
            return

        Path(path).write_text("", encoding="utf-8")
        rel = os.path.relpath(path, self.root) if self.root else path
        self.io.tool_output(f"Created instruction: {rel}")

        abs_path = os.path.abspath(path)
        if self.coder:
            self.coder.abs_fnames.add(abs_path)
        self.io.tool_output(f"Added to chat for editing: {Path(name).stem}")

    # ------------------------------------------------------------------
    # Delete command
    # ------------------------------------------------------------------

    def cmd_delete(self, args):
        """Delete prose, summaries, db entries, or instructions.

        Usage:
            /delete prose <level> N [<level> M ...]
            /delete prose N [M [P]]
            /delete summaries <level> N [<level> M ...]
            /delete summaries N [M [P]]
            /delete db <name>
            /delete db <category> <name>
            /delete instruction <name>
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /delete prose <location> | "
                "summaries <location> | "
                "db <name> | instruction <name>"
            )
            return

        tokens = args.split()
        keyword = tokens[0].lower()

        if keyword == "db":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                self.io.tool_error(
                    "Usage: /delete db <name> or /delete db <category> <name>"
                )
                return
            return self._delete_db(rest)
        elif keyword == "instruction":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                self.io.tool_error("Usage: /delete instruction <name>")
                return
            return self._delete_instruction(rest)
        elif keyword == "prose":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                self.io.tool_error("Usage: /delete prose <location>")
                return
            return self._delete_files_by_name(rest, "PROSE.md")
        elif keyword == "summaries":
            rest = " ".join(tokens[1:]).strip()
            if not rest:
                self.io.tool_error("Usage: /delete summaries <location>")
                return
            return self._delete_files_by_name(rest, "SUMMARY.md")

        self.io.tool_error(
            "Usage: /delete prose <location> | "
            "summaries <location> | "
            "db <name> | instruction <name>"
        )

    def _delete_db(self, args):
        """Delete a db entry, optionally scoped by category."""
        tokens = args.split()

        category = None
        name = args

        # If two tokens, try first as category
        if len(tokens) == 2:
            candidate_cat = tokens[0]
            candidate_name = tokens[1]
            # Check if first token is a valid category
            entry = self.db.get_entry(candidate_name, category=candidate_cat)
            if entry:
                category = candidate_cat
                name = candidate_name

        entry = self.db.delete_entry(name, category=category)
        if not entry:
            self.io.tool_error(f"No db entry matching '{args}' found.")
            return

        self.io.tool_output(f"Deleted db entry: {entry.category}/{entry.name}")

        # Remove from coder's file sets if present
        if self.coder:
            abs_path = os.path.abspath(entry.path)
            self.coder.abs_fnames.discard(abs_path)
            self.coder.abs_read_only_fnames.discard(abs_path)

    def _delete_instruction(self, name):
        """Delete an instruction file."""
        instructions_dir = os.path.join(self.root, "instructions")
        if not os.path.isdir(instructions_dir):
            self.io.tool_error("No instructions/ directory found.")
            return

        path = self._find_instruction_file(instructions_dir, name)
        if not path:
            available = self._list_instruction_names(instructions_dir)
            if available:
                self.io.tool_error(
                    f"Instruction '{name}' not found. "
                    f"Available: {', '.join(available)}"
                )
            else:
                self.io.tool_error(
                    f"Instruction '{name}' not found. No instructions exist."
                )
            return

        try:
            os.remove(path)
        except OSError as e:
            self.io.tool_error(f"Error deleting instruction: {e}")
            return

        rel = os.path.relpath(path, self.root) if self.root else path
        self.io.tool_output(f"Deleted instruction: {rel}")

        # Remove from coder's file sets if present
        if self.coder:
            abs_path = os.path.abspath(path)
            self.coder.abs_fnames.discard(abs_path)
            self.coder.abs_read_only_fnames.discard(abs_path)

    def _delete_files_by_name(self, args, filename):
        """Delete all files matching *filename* under a narrative location."""
        parsed = self._parse_location_args(args)
        if parsed is None:
            return

        levels = self.levels
        nums = parsed

        # Navigate to the target directory
        target = self.novel_root
        for i, num in enumerate(nums):
            level_name = levels[i]
            found = self._find_level_dir(target, level_name, num)
            if not found:
                self.io.tool_error(f"{level_name} {num} not found.")
                return
            target = found

        # Collect all matching files under target
        to_delete = []
        for dirpath, _dirs, files in os.walk(target):
            if filename in files:
                to_delete.append(os.path.join(dirpath, filename))

        if not to_delete:
            label = ", ".join(
                f"{levels[i].lower()} {nums[i]}" for i in range(len(nums))
            )
            self.io.tool_error(
                f"No {filename} files found under {label}."
            )
            return

        rel_paths = [os.path.relpath(p, self.root) for p in to_delete]
        if not self.io.confirm_ask(
            f"Delete {len(to_delete)} {filename} file(s)?\n"
            + "\n".join(f"  {r}" for r in rel_paths),
            default="n",
        ):
            self.io.tool_output("Cancelled.")
            return

        deleted = 0
        for path in to_delete:
            try:
                os.remove(path)
                deleted += 1
                # Remove from coder's file sets
                if self.coder:
                    abs_path = os.path.abspath(path)
                    self.coder.abs_fnames.discard(abs_path)
                    self.coder.abs_read_only_fnames.discard(abs_path)
            except OSError as e:
                self.io.tool_error(f"Error deleting {path}: {e}")

        self.io.tool_output(f"Deleted {deleted} {filename} file(s).")
        self._narrative_map = None

    def _shift_dirs_down(self, parent_dir, level_name, from_num):
        """Rename all ``Level N - Title`` directories with N >= from_num to N-1.

        Renames in ascending order (lowest first) to avoid conflicts.
        Returns the number of directories shifted.
        """
        if not os.path.isdir(parent_dir):
            return 0

        level_re = _build_level_re([level_name])
        to_shift = []
        for name in os.listdir(parent_dir):
            full = os.path.join(parent_dir, name)
            if not os.path.isdir(full):
                continue
            m = level_re.match(name)
            if m and int(m.group(2)) >= from_num:
                to_shift.append((int(m.group(2)), m.group(3), full))

        # Sort ascending so we rename lowest first
        to_shift.sort(key=lambda x: x[0])

        for num, old_title, old_path in to_shift:
            new_name = make_titled_dir(level_name, num - 1, old_title)
            new_path = os.path.join(parent_dir, new_name)
            os.rename(old_path, new_path)

        return len(to_shift)

    def completions_delete(self):
        """Return completions for /delete command."""
        return ["prose", "summaries", "db", "instruction"]

    # ------------------------------------------------------------------
    # Insert-after / insert-before commands
    # ------------------------------------------------------------------

    def cmd_insert_after(self, args):
        """Insert a new node after a position, renumbering subsequent elements.

        Usage:
            /insert-after act N [title]
            /insert-after [act N] chapter M [title]
            /insert-after [act N] [chapter M] scene P [title]
            /insert-after N [title]            (shorthand: insert act)
            /insert-after N M [title]          (shorthand: insert chapter)
            /insert-after N M P [title]        (shorthand: insert scene)
        """
        self._do_insert(args, before=False)

    def cmd_insert_before(self, args):
        """Insert a new node before a position, renumbering subsequent elements.

        Usage:
            /insert-before act N [title]
            /insert-before [act N] chapter M [title]
            /insert-before [act N] [chapter M] scene P [title]
            /insert-before N [title]            (shorthand: insert act)
            /insert-before N M [title]          (shorthand: insert chapter)
            /insert-before N M P [title]        (shorthand: insert scene)
        """
        self._do_insert(args, before=True)

    def completions_insert_after(self):
        """Return completions for /insert-after command."""
        return [l.lower() for l in self.levels]

    def completions_insert_before(self):
        """Return completions for /insert-before command."""
        return [l.lower() for l in self.levels]

    def _do_insert(self, args, before):
        """Shared logic for insert-after and insert-before."""
        args = args.strip()
        label = "insert-before" if before else "insert-after"
        if not args:
            self.io.tool_error(
                f"Usage: /{label} <level> N [title] | N [M [P]] [title]"
            )
            return

        parsed = self._parse_insert_args(args, label)
        if parsed is None:
            return

        depth, parent_nums, ref_num, title = parsed
        return self._insert_node(depth, parent_nums, ref_num, title, before)

    def _parse_insert_args(self, args, label="insert-after"):
        """Parse /insert-after or /insert-before arguments.

        Returns (depth, parent_nums, ref_num, title) or None on error.

        Supports keyword and shorthand syntax:
            <level> N [title]                     →  insert at that level
            [<level> N] <level> M [title]         →  insert with parent context
            N [title]                             →  insert at depth 0 (shorthand)
            N M [title]                           →  insert at depth 1 (shorthand)
            N M P [title]                         →  insert at depth 2 (shorthand)
        """
        tokens = args.split()
        levels = self.levels
        level_names = [l.lower() for l in levels]
        usage = (
            f"Usage: /{label} <level> N [title] | N [M [P]] [title]"
        )

        # --- Shorthand: leading numbers determine the level ---
        if tokens[0].isdigit() and (
            len(tokens) < 2 or tokens[1].isdigit()
            or tokens[1].lower() not in level_names
        ):
            nums = []
            title_start = 0
            for i, tok in enumerate(tokens):
                if tok.isdigit():
                    nums.append(int(tok))
                    title_start = i + 1
                else:
                    break
            title = " ".join(tokens[title_start:]) or None

            if len(nums) < 1 or len(nums) > len(levels):
                self.io.tool_error(usage)
                return None

            depth = len(nums) - 1
            parent_nums = nums[:-1]
            ref_num = nums[-1]
            return (depth, parent_nums, ref_num, title)

        # --- Keyword syntax ---
        pos = 0
        parent_nums = []
        current_level_idx = -1

        while pos < len(tokens) and tokens[pos].lower() in level_names:
            current_level_idx = level_names.index(tokens[pos].lower())
            pos += 1
            if pos >= len(tokens) or not tokens[pos].isdigit():
                self.io.tool_error(
                    f"Expected a number after '{tokens[pos-1]}'. {usage}"
                )
                return None
            ref_num = int(tokens[pos])
            pos += 1

            # Is the next token another level keyword?
            if pos < len(tokens) and tokens[pos].lower() in level_names:
                parent_nums.append(ref_num)
            else:
                # This is the target level — depth from the level keyword
                depth = current_level_idx
                title = " ".join(tokens[pos:]) or None
                return (depth, parent_nums, ref_num, title)

        self.io.tool_error(usage)
        return None

    def _shift_dirs_up(self, parent_dir, level_name, from_num):
        """Rename all ``Level N - Title`` directories with N >= from_num to N+1.

        Renames in descending order (highest first) to avoid conflicts.
        Returns the number of directories shifted.
        """
        if not os.path.isdir(parent_dir):
            return 0

        level_re = _build_level_re([level_name])
        to_shift = []
        for name in os.listdir(parent_dir):
            full = os.path.join(parent_dir, name)
            if not os.path.isdir(full):
                continue
            m = level_re.match(name)
            if m and int(m.group(2)) >= from_num:
                to_shift.append((int(m.group(2)), m.group(3), full))

        # Sort descending so we rename highest first
        to_shift.sort(key=lambda x: x[0], reverse=True)

        for num, old_title, old_path in to_shift:
            new_name = make_titled_dir(level_name, num + 1, old_title)
            new_path = os.path.join(parent_dir, new_name)
            os.rename(old_path, new_path)

        return len(to_shift)

    def _insert_node(self, depth, parent_nums, ref_num, title, before):
        """Insert a new node before or after *ref_num* at the given depth."""
        levels = self.levels
        level_name = levels[depth]
        is_leaf = depth == len(levels) - 1

        # Navigate to the parent directory — default missing ancestors to last
        parent_dir = self.novel_root
        for i in range(depth):
            ancestor_level = levels[i]
            if i < len(parent_nums):
                num = parent_nums[i]
            else:
                num = self._find_last_level_number(parent_dir, ancestor_level)
                if num is None:
                    self.io.tool_error(
                        f"No {ancestor_level.lower()}s found. "
                        f"Create one first with /new {ancestor_level.lower()}"
                    )
                    return
            found = self._find_level_dir(parent_dir, ancestor_level, num)
            if not found:
                self.io.tool_error(f"{ancestor_level} {num} not found.")
                return
            parent_dir = found

        # Validate reference exists
        ref_dir = self._find_level_dir(parent_dir, level_name, ref_num)
        if not ref_dir:
            self.io.tool_error(f"{level_name} {ref_num} not found.")
            return

        new_num = ref_num if before else ref_num + 1
        shifted = self._shift_dirs_up(parent_dir, level_name, new_num)

        dir_name = make_titled_dir(level_name, new_num, title)
        node_dir = os.path.join(parent_dir, dir_name)
        os.makedirs(node_dir, exist_ok=True)

        summary_path = os.path.join(node_dir, "SUMMARY.md")
        Path(summary_path).write_text(
            (title or "Untitled") + "\n", encoding="utf-8"
        )

        if is_leaf:
            prose_path = os.path.join(node_dir, "PROSE.md")
            self._touch(prose_path)

        rel = os.path.relpath(node_dir, self.root)
        pos_label = "before" if before else "after"
        self.io.tool_output(
            f"Inserted {rel}/ {pos_label} {level_name.lower()} {ref_num}"
            + (f" (renumbered {shifted})" if shifted else "")
        )

        if self.coder:
            self.coder.abs_fnames.add(os.path.abspath(summary_path))
            if is_leaf:
                self.coder.abs_fnames.add(os.path.abspath(prose_path))

        self._narrative_map = None

    # ------------------------------------------------------------------
    # /move — move a narrative node or file to a new position
    # ------------------------------------------------------------------

    def cmd_move(self, args):
        """Move a narrative node or file to a new position.

        For narrative nodes, moves the source to the target position and
        renumbers siblings in both the old and new locations.

        Usage:
            /move <source> to <target>

        Narrative examples:
            /move 2 1 to 2 2          — move act 2 ch 1 → act 2 ch 2 position
            /move 1 1 1 to 1 2 1      — move act1/ch1/sc1 → act1/ch2/sc1
            /move 2 1 to 2             — move ch 1 to end of act 2
            /move act 2 chapter 1 to act 2 chapter 3

        File/directory examples:
            /move db/characters/Sarah.md db/locations/
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /move <source> to <target>\n"
                "       /move <source_path> <target_path>"
            )
            return

        # Split on " to " for narrative moves
        if " to " in args:
            parts = args.split(" to ", 1)
            src_str = parts[0].strip()
            tgt_str = parts[1].strip()
            return self._move_narrative(src_str, tgt_str)

        # Fall back to file/directory move
        tokens = args.split()
        if len(tokens) == 2:
            return self._move_path(tokens[0], tokens[1])

        self.io.tool_error(
            "Usage: /move <source> to <target>\n"
            "       /move <source_path> <target_path>"
        )

    def completions_move(self):
        """Return completions for /move command."""
        return [l.lower() for l in self.levels]

    def _move_narrative(self, src_str, tgt_str):
        """Move a narrative node to a new position."""
        src_nums = self._parse_location_args(src_str)
        if src_nums is None:
            return

        tgt_nums = self._parse_location_args(tgt_str)
        if tgt_nums is None:
            return

        src_depth = len(src_nums)
        tgt_depth = len(tgt_nums)
        levels = self.levels

        if src_depth > len(levels) or tgt_depth > len(levels):
            self.io.tool_error("Location exceeds configured level depth.")
            return

        # The source level (what we're moving)
        src_level_idx = src_depth - 1
        src_level_name = levels[src_level_idx]

        # Target must be same depth (exact position) or one less (append to parent)
        if tgt_depth == src_depth:
            # Moving to an exact position within a (possibly different) parent
            tgt_parent_nums = tgt_nums[:-1]
            tgt_position = tgt_nums[-1]
        elif tgt_depth == src_depth - 1:
            # Moving to end of a parent
            tgt_parent_nums = tgt_nums
            tgt_position = None  # means "append"
        else:
            self.io.tool_error(
                f"Target depth ({tgt_depth}) must match source depth "
                f"({src_depth}) or be one level up ({src_depth - 1})."
            )
            return

        # Resolve the source node and its parent directory
        self.narrative_map.refresh()
        src_node = self.narrative_map.find_node(*src_nums)
        if src_node is None:
            parts = [f"{levels[i].lower()} {src_nums[i]}" for i in range(src_depth)]
            self.io.tool_error(f"Source not found: {', '.join(parts)}.")
            return

        # Find source parent directory
        src_parent_dir = self.novel_root
        for i in range(src_depth - 1):
            src_parent_dir = self._find_level_dir(
                src_parent_dir, levels[i], src_nums[i]
            )
            if not src_parent_dir:
                self.io.tool_error(f"{levels[i]} {src_nums[i]} not found.")
                return

        # Find target parent directory
        tgt_parent_dir = self.novel_root
        for i in range(len(tgt_parent_nums)):
            tgt_parent_dir = self._find_level_dir(
                tgt_parent_dir, levels[i], tgt_parent_nums[i]
            )
            if not tgt_parent_dir:
                self.io.tool_error(f"{levels[i]} {tgt_parent_nums[i]} not found.")
                return

        src_num = src_nums[-1]
        src_dir = src_node.path
        same_parent = os.path.normpath(src_parent_dir) == os.path.normpath(tgt_parent_dir)

        if tgt_position is None:
            # Append to end of target parent
            tgt_position = self._next_level_number(tgt_parent_dir, src_level_name)
            if same_parent:
                # If appending within the same parent, the "next" slot after
                # removal is one less (since we're removing the source first)
                tgt_position -= 1

        if same_parent and tgt_position == src_num:
            self.io.tool_output("Source and target are the same; nothing to do.")
            return

        # Collect abs paths under source for coder updates
        old_abs_paths = set()
        if self.coder:
            for abs_path in list(self.coder.abs_fnames) + list(self.coder.abs_read_only_fnames):
                if abs_path.startswith(os.path.abspath(src_dir) + os.sep):
                    old_abs_paths.add(abs_path)

        # Remove old coder references (will re-add with new paths)
        if self.coder:
            for p in old_abs_paths:
                self.coder.abs_fnames.discard(p)
                self.coder.abs_read_only_fnames.discard(p)

        # Step 1: Move source to a temp name to avoid collisions
        temp_name = f".moving_{os.path.basename(src_dir)}"
        temp_path = os.path.join(src_parent_dir, temp_name)
        os.rename(src_dir, temp_path)

        # Step 2: Close the gap in the source parent (shift down from src_num+1)
        self._shift_dirs_down(src_parent_dir, src_level_name, src_num + 1)

        # Step 3: Compute effective target in the gap-closed numbering.
        # For cross-parent moves, tgt_position is already correct.
        # For same-parent: after removing source, everything above src_num
        # shifted down by 1.  If target > src, the slot we want in the
        # *closed* sequence is (tgt - 1).  But _shift_dirs_up will push
        # items from that slot onward up by 1, so the final slot the moved
        # item lands in is the same number.  We actually need the item to
        # end up at tgt_position in the FINAL numbering, so we must NOT
        # adjust — the shift-up reopens the slot at the right place.
        effective_tgt = tgt_position

        # Step 4: Open a gap at the target position (shift up from effective_tgt)
        self._shift_dirs_up(tgt_parent_dir, src_level_name, effective_tgt)

        # Step 5: Move source into target position
        # Preserve the original title
        parsed = parse_level_dir(os.path.basename(src_dir), levels)
        old_title = parsed[2] if parsed else None
        new_name = make_titled_dir(src_level_name, effective_tgt, old_title)
        new_path = os.path.join(tgt_parent_dir, new_name)
        os.rename(temp_path, new_path)

        # Step 6: Update coder references with new paths
        if self.coder and old_abs_paths:
            old_base = os.path.abspath(src_dir)
            new_base = os.path.abspath(new_path)
            for old_p in old_abs_paths:
                new_p = old_p.replace(old_base, new_base, 1)
                if os.path.isfile(new_p):
                    # Preserve editable vs read-only status
                    if old_p in self.coder.abs_read_only_fnames:
                        self.coder.abs_read_only_fnames.add(new_p)
                    else:
                        self.coder.abs_fnames.add(new_p)

        # Build labels
        src_label = ", ".join(
            f"{levels[i].lower()} {src_nums[i]}" for i in range(src_depth)
        )
        if tgt_depth == src_depth:
            tgt_label = ", ".join(
                f"{levels[i].lower()} {tgt_nums[i]}" for i in range(tgt_depth)
            )
        else:
            parent_label = ", ".join(
                f"{levels[i].lower()} {tgt_parent_nums[i]}"
                for i in range(len(tgt_parent_nums))
            )
            tgt_label = f"end of {parent_label}" if parent_label else "end"

        self.io.tool_output(f"Moved {src_label} → {tgt_label}")
        self._narrative_map = None

    def _move_path(self, src, tgt):
        """Move a file or directory to a new location (for db entries etc.)."""
        src_path = os.path.join(self.root, src) if not os.path.isabs(src) else src
        tgt_path = os.path.join(self.root, tgt) if not os.path.isabs(tgt) else tgt

        if not os.path.exists(src_path):
            self.io.tool_error(f"Source not found: {src}")
            return

        # If target is a directory, move source into it
        if os.path.isdir(tgt_path):
            tgt_path = os.path.join(tgt_path, os.path.basename(src_path))

        if os.path.exists(tgt_path):
            self.io.tool_error(f"Target already exists: {tgt}")
            return

        # Update coder references
        old_abs = os.path.abspath(src_path)
        new_abs = os.path.abspath(tgt_path)

        os.rename(src_path, tgt_path)

        if self.coder:
            if os.path.isfile(tgt_path):
                if old_abs in self.coder.abs_fnames:
                    self.coder.abs_fnames.discard(old_abs)
                    self.coder.abs_fnames.add(new_abs)
                if old_abs in self.coder.abs_read_only_fnames:
                    self.coder.abs_read_only_fnames.discard(old_abs)
                    self.coder.abs_read_only_fnames.add(new_abs)

        src_rel = os.path.relpath(src_path, self.root)
        tgt_rel = os.path.relpath(tgt_path, self.root)
        self.io.tool_output(f"Moved {src_rel} → {tgt_rel}")

    # ------------------------------------------------------------------
    # /instruct — inject instruction text into the chat
    # ------------------------------------------------------------------

    def cmd_instruct(self, args):
        """Load an instruction file and inject its content into the current chat.

        Usage:
            /instruct <instruction_name>
        """
        args = args.strip()
        if not args:
            self.io.tool_error("Usage: /instruct <instruction_name>")
            return

        instructions_dir = os.path.join(self.root, "instructions")
        if not os.path.isdir(instructions_dir):
            self.io.tool_error("No instructions/ directory found.")
            return

        # Find the instruction file (try exact match, then with extensions)
        path = self._find_instruction_file(instructions_dir, args)
        if not path:
            available = self._list_instruction_names(instructions_dir)
            if available:
                self.io.tool_error(
                    f"Instruction '{args}' not found. "
                    f"Available: {', '.join(available)}"
                )
            else:
                self.io.tool_error(
                    f"Instruction '{args}' not found. "
                    "No instructions exist yet — create one with /new instruction <name>"
                )
            return

        try:
            content = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            self.io.tool_error(f"Error reading instruction: {e}")
            return

        if not content.strip():
            self.io.tool_warning(f"Instruction '{args}' is empty.")
            return

        rel = os.path.relpath(path, self.root) if self.root else path
        self.io.tool_output(f"Loading instruction: {rel}")
        return content

    def completions_instruct(self):
        """Return completions for /instruct command."""
        instructions_dir = os.path.join(self.root, "instructions")
        return self._list_instruction_names(instructions_dir)

    def _find_instruction_file(self, instructions_dir, name):
        """Find an instruction file by name (with or without extension)."""
        # Exact match
        exact = os.path.join(instructions_dir, name)
        if os.path.isfile(exact):
            return exact

        # Try common extensions
        for ext in (".txt", ".md"):
            candidate = os.path.join(instructions_dir, name + ext)
            if os.path.isfile(candidate):
                return candidate

        # Case-insensitive search
        name_lower = name.lower()
        for fname in os.listdir(instructions_dir):
            fpath = os.path.join(instructions_dir, fname)
            if not os.path.isfile(fpath):
                continue
            stem = Path(fname).stem
            if stem.lower() == name_lower or fname.lower() == name_lower:
                return fpath

        return None

    def _list_instruction_names(self, instructions_dir):
        """Return sorted list of instruction names (stems) from the directory."""
        if not os.path.isdir(instructions_dir):
            return []
        names = []
        for fname in sorted(os.listdir(instructions_dir)):
            fpath = os.path.join(instructions_dir, fname)
            if os.path.isfile(fpath):
                names.append(Path(fname).stem)
        return names

    # ------------------------------------------------------------------
    # /lint override — use Vale for prose linting
    # ------------------------------------------------------------------

    def cmd_lint(self, args=""):
        """Lint prose files using Vale.

        Usage:
            /lint                   — lint files in the chat (or all dirty files)
            /lint act 1 chapter 2   — lint all prose under act 1, chapter 2
            /lint 1 2 3             — lint act 1, chapter 2, scene 3
            /lint <file> ...        — lint specific files
        """
        fnames = self._get_target_files(args)

        # Fall back to dirty files if nothing in chat
        if not fnames and self.coder and self.coder.repo:
            dirty = self.coder.repo.get_dirty_files()
            if dirty:
                fnames = [
                    self.coder.abs_root_path(f) if hasattr(self.coder, "abs_root_path") else f
                    for f in dirty
                ]

        if not fnames:
            self.io.tool_warning("No files to lint. Add files with /add first.")
            return

        # Filter to prose files only (.txt, .md)
        prose_fnames = [f for f in fnames if f.endswith((".txt", ".md"))]
        if not prose_fnames:
            # No prose files — delegate to base /lint for code files
            if self._parent_commands:
                return self._parent_commands.cmd_lint(args)
            self.io.tool_warning("No prose files (.txt, .md) found to lint.")
            return

        from aider.io import ConfirmGroup

        severities = self._lint_severities()

        # Run Vale on all files in a single invocation.
        batch_results = self.vale_linter.lint_files(prose_fnames)

        # Collect all issues across files, then offer to fix in one batch.
        file_issues = []  # [(fname, filtered_text), ...]
        for fname in prose_fnames:
            result = batch_results.get(fname)
            if result is None:
                rel = os.path.relpath(fname, self.root) if self.root else fname
                self.io.tool_output(f"{rel}: no prose issues found.")
                continue

            # Filter to the current lint level
            filtered_text = self._filter_warnings(result, severities)
            if not filtered_text:
                rel = os.path.relpath(fname, self.root) if self.root else fname
                self.io.tool_output(f"{rel}: no prose issues found.")
                continue

            file_issues.append((fname, filtered_text))
            self.io.tool_output(filtered_text)

        if not file_issues:
            self.io.tool_output("All files clean.")
            return

        if self.coder and self.io.confirm_ask(
            "Fix lint issues?", default="y"
        ):
            all_errors = "\n".join(text for _, text in file_issues)
            all_fnames = [fname for fname, _ in file_issues]
            self._fix_lint_errors_batch(all_fnames, all_errors)

    _LINT_LEVELS = {
        "error": {"error"},
        "warning": {"error", "warning"},
        "suggestion": {"error", "warning", "suggestion"},
    }

    def _lint_severities(self):
        """Return the set of severities for the current lint level."""
        return self._LINT_LEVELS.get(self._lint_level, {"error", "warning"})

    def _filter_warnings(self, result, severities):
        """Filter a ValeLintResult to only include the given severities.

        Returns formatted text for the LLM, or empty string if nothing
        matches.
        """
        filtered = [w for w in result.warnings if w["severity"] in severities]
        if not filtered:
            return ""

        rel_fname = result.text.split("\n")[0].replace("## Vale lint: ", "")
        return self.vale_linter._format_warnings(rel_fname, filtered)

    def _fix_lint_errors(self, fname, errors):
        """Use the LLM to fix lint errors in a file."""
        from aider.coders.base_coder import Coder

        if not self.coder:
            return

        # Commit before fixing if dirty
        if (
            self._parent_commands
            and self.coder.repo
            and self.coder.repo.is_dirty()
            and self.coder.dirty_commits
        ):
            self._parent_commands.cmd_commit("")

        lint_coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            summarize_from_coder=False,
        )

        # Only the file being fixed should be editable
        lint_coder.abs_fnames = {os.path.abspath(fname)}
        lint_coder.abs_read_only_fnames = set()
        lint_coder.cur_messages = []
        lint_coder.done_messages = []

        lint_coder.run("# Fix any errors below, if possible.\n\n" + errors)
        lint_coder.abs_fnames = set()

        if (
            self._parent_commands
            and self.coder.repo
            and self.coder.repo.is_dirty()
            and self.coder.auto_commits
        ):
            self._parent_commands.cmd_commit("")

    def _fix_lint_errors_batch(self, fnames, errors):
        """Use the LLM to fix lint errors across multiple files in one call."""
        from aider.coders.base_coder import Coder

        if not self.coder:
            return

        # Commit before fixing if dirty
        if (
            self._parent_commands
            and self.coder.repo
            and self.coder.repo.is_dirty()
            and self.coder.dirty_commits
        ):
            self._parent_commands.cmd_commit("")

        lint_coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            summarize_from_coder=False,
        )

        # All affected files should be editable
        lint_coder.abs_fnames = {os.path.abspath(f) for f in fnames}
        lint_coder.abs_read_only_fnames = set()
        lint_coder.cur_messages = []
        lint_coder.done_messages = []

        lint_coder.run("# Fix any errors below, if possible.\n\n" + errors)
        lint_coder.abs_fnames = set()

        if (
            self._parent_commands
            and self.coder.repo
            and self.coder.repo.is_dirty()
            and self.coder.auto_commits
        ):
            self._parent_commands.cmd_commit("")

    def completions_lint(self):
        return [l.lower() for l in self.levels]

    # ------------------------------------------------------------------
    # /lint-level — set which severity levels /lint reports
    # ------------------------------------------------------------------

    def cmd_lint_level(self, args=""):
        """Set which severity levels /lint reports and fixes.

        Usage:
            /lint-level              — show current level
            /lint-level error        — errors only
            /lint-level warning      — errors + warnings (default)
            /lint-level suggestion   — everything
        """
        args = args.strip().lower()

        if not args:
            self.io.tool_output(f"Lint level: {self._lint_level}")
            return

        if args in self._LINT_LEVELS:
            self._lint_level = args
            # Sync to coder so auto-lint uses the same level
            if self.coder and hasattr(self.coder, "_novel_lint_level"):
                self.coder._novel_lint_level = args
            severities = " + ".join(sorted(self._lint_severities()))
            self.io.tool_output(f"Lint level set to: {self._lint_level} ({severities})")
        else:
            self.io.tool_error(
                f"Unknown level '{args}'. Choose: error, warning, suggestion"
            )

    def completions_lint_level(self):
        return ["error", "warning", "suggestion"]

    # ------------------------------------------------------------------
    # Import command
    # ------------------------------------------------------------------

    def cmd_import(self, args):
        """Import content into this project from an external source.

        Subcommands:
            /import novelcrafter <path>  — import a Novelcrafter markdown export
                                           (zip file or directory)
            /import markdown <file.md>   — import a plain markdown file using
                                           H1=act, H2=chapter, H3=scene

        Both modes ensure act/ and db/ have no uncommitted changes, clear act/
        (and db/ for novelcrafter), run the import, then commit the result.
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /import novelcrafter <path-to-zip-or-directory>\n"
                "       /import markdown <file.md>"
            )
            return

        parts = args.split(None, 1)
        subcmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "novelcrafter":
            self._import_novelcrafter(rest)
        elif subcmd == "markdown":
            self._import_markdown(rest)
        else:
            self.io.tool_error(
                f"Unknown import format: {subcmd}\n"
                "Usage: /import novelcrafter <path>\n"
                "       /import markdown <file.md>"
            )

    def _check_dirty_act_db(self):
        """Return True (and print errors) if narrative dirs or db/ have uncommitted changes."""
        repo = self.coder.repo if self.coder else None
        if not repo:
            return False
        dirty_files = repo.get_dirty_files()

        # Build prefixes for the top-level narrative dirs
        top_level = self.levels[0]
        level_re = _build_level_re([top_level])

        blocking = []
        for f in dirty_files:
            parts = f.split("/", 1)
            if parts[0] == "db" or parts[0] == NOVEL_DIR or level_re.match(parts[0]):
                blocking.append(f)

        if blocking:
            self.io.tool_error(
                "Uncommitted changes in narrative or db/ directories "
                "— commit or stash them first:"
            )
            for f in sorted(blocking):
                self.io.tool_error(f"  {f}")
            return True
        return False

    def _commit_import(self, dirs, commit_msg):
        """Stage *dirs* and commit with *commit_msg*."""
        repo = self.coder.repo if self.coder else None
        if not repo:
            return
        try:
            repo.repo.git.add("-A", "--", *dirs)
            if repo.is_dirty():
                repo.repo.git.commit("-m", commit_msg)
                commit_hash = repo.get_head_commit_sha(short=True)
                self.io.tool_output(f"Committed {commit_hash}: {commit_msg}")
        except Exception as e:
            self.io.tool_error(f"Git commit failed: {e}")

    # --- /import novelcrafter ---

    def _import_novelcrafter(self, args):
        if not args:
            self.io.tool_error("Usage: /import novelcrafter <path-to-zip-or-directory>")
            return

        source = args
        if not os.path.isabs(source):
            source = os.path.join(self.root, source)
        if not os.path.exists(source):
            self.io.tool_error(f"Source not found: {source}")
            return

        if self._check_dirty_act_db():
            return

        # Delete novel/ (narrative dirs) and db/
        novel_dir = self.novel_root
        if os.path.isdir(novel_dir):
            shutil.rmtree(novel_dir)
        db_dir = os.path.join(self.root, "db")
        if os.path.isdir(db_dir):
            shutil.rmtree(db_dir)

        from .importer import NovelcrafterImporter

        importer = NovelcrafterImporter(source, self.root, io=self.io)
        try:
            summary = importer.run()
        except Exception as e:
            self.io.tool_error(f"Import failed: {e}")
            return

        # (Re)create placeholder cover if missing
        from . import seed_cover_image

        seed_cover_image(self.root, self.io)

        parts = []
        for key in ("acts", "chapters", "scenes"):
            if summary.get(key):
                parts.append(f"{summary[key]} {key}")
        skip = {"acts", "chapters", "scenes", "snippets"}
        for key in sorted(summary):
            if key in skip:
                continue
            if summary[key]:
                parts.append(f"{summary[key]} {key}")
        if summary.get("snippets"):
            parts.append(f"{summary['snippets']} snippets")

        if parts:
            self.io.tool_output(f"Import complete: {', '.join(parts)}.")
        else:
            self.io.tool_warning("Import completed but nothing was found to import.")

        commit_msg = "Import from Novelcrafter"
        if parts:
            commit_msg += f": {', '.join(parts)}"
        self._commit_import([".", "db"], commit_msg)

    # --- /import markdown ---

    def _import_markdown(self, args):
        if not args:
            self.io.tool_error("Usage: /import markdown <file.md>")
            return

        source = args
        if not os.path.isabs(source):
            source = os.path.join(self.root, source)
        if not os.path.isfile(source):
            self.io.tool_error(f"File not found: {source}")
            return

        if self._check_dirty_act_db():
            return

        # Delete novel/ (narrative dirs only — markdown import has no db)
        novel_dir = self.novel_root
        if os.path.isdir(novel_dir):
            shutil.rmtree(novel_dir)

        from .importer import MarkdownImporter

        importer = MarkdownImporter(source, self.root, io=self.io)
        try:
            summary = importer.run()
        except Exception as e:
            self.io.tool_error(f"Import failed: {e}")
            return

        # (Re)create placeholder cover if missing
        from . import seed_cover_image

        seed_cover_image(self.root, self.io)

        parts = []
        for key in ("acts", "chapters", "scenes"):
            if summary.get(key):
                parts.append(f"{summary[key]} {key}")

        if parts:
            self.io.tool_output(f"Import complete: {', '.join(parts)}.")
        else:
            self.io.tool_warning("Import completed but nothing was found to import.")

        commit_msg = "Import from markdown"
        if parts:
            commit_msg += f": {', '.join(parts)}"
        self._commit_import(["."], commit_msg)

    # ------------------------------------------------------------------
    # /export — export the narrative to markdown, docx, or epub
    # ------------------------------------------------------------------

    def cmd_export(self, args):
        """Export the narrative to a file.

        Subcommands:
            /export markdown [file]  — export as a single Markdown file
            /export docx [file]      — export as a styled Word document
            /export epub [file]      — export as an EPUB ebook

        If no filename is given, a default is chosen based on the format.
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /export markdown [file]\n"
                "       /export docx [file]\n"
                "       /export epub [file]"
            )
            return

        parts = args.split(None, 1)
        fmt = parts[0].lower()
        filename = parts[1].strip() if len(parts) > 1 else None

        valid_fmts = {"markdown", "docx", "epub"}
        if fmt not in valid_fmts:
            self.io.tool_error(
                f"Unknown format: {fmt}\n"
                "Supported formats: markdown, docx, epub"
            )
            return

        tree = self.narrative_map.get_tree()
        if not tree:
            self.io.tool_error("Nothing to export — no narrative structure found.")
            return

        # Read title/author from db/core/metadata.yml if present
        title, author = self._read_book_metadata()

        # Default filenames
        defaults = {
            "markdown": "export.md",
            "docx": "export.docx",
            "epub": "export.epub",
        }
        if not filename:
            filename = defaults[fmt]
        if not os.path.isabs(filename):
            filename = os.path.join(self.root, filename)

        from .exporter import export_docx, export_epub, export_markdown

        try:
            if fmt == "markdown":
                export_markdown(tree, filename)
            elif fmt == "docx":
                export_docx(tree, filename)
            elif fmt == "epub":
                export_epub(tree, filename, title=title, author=author)
        except ImportError as e:
            pkg = "python-docx" if fmt == "docx" else "ebooklib"
            self.io.tool_error(
                f"Missing dependency for {fmt} export: {e}\n"
                f"Install it with: pip install {pkg}"
            )
            return
        except Exception as e:
            self.io.tool_error(f"Export failed: {e}")
            return

        rel = os.path.relpath(filename, self.root)
        self.io.tool_output(f"Exported to {rel}")

    def _read_book_metadata(self):
        """Read title and author from db/core/metadata.yml if it exists."""
        meta_path = os.path.join(self.root, "db", "core", "metadata.yml")
        title = "Untitled"
        author = "Unknown"
        if os.path.isfile(meta_path):
            text = Path(meta_path).read_text(encoding="utf-8")
            data = yaml.safe_load(text) or {}
            title = data.get("title", title)
            author = data.get("author", author)
        return title, author

    def cmd_copy_context(self, args):
        """Copy the full chat context as a transcript, suitable for a web UI.

        Use "/copy-context continue" to copy only from the current conversation
        (after done/summary), omitting cached history.
        """
        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        args = (args or "").strip()
        continue_mode = args.lower() == "continue"
        extra_instruction = args if not continue_mode else ""

        markdown = build_copy_context_markdown(
            self.coder, continue_only=continue_mode, extra=extra_instruction
        )

        try:
            pyperclip.copy(markdown)
            label = "continuation context" if continue_mode else "full context"
            self.io.tool_output(f"Copied {label} to clipboard.")
        except Exception as e:
            self.io.tool_error(f"Failed to copy to clipboard: {e}")
            self.io.tool_output(
                "You may need to install xclip or xsel on Linux, or pbcopy on macOS."
            )

    # ------------------------------------------------------------------
    # /paste-response — apply a pasted LLM response as if it came from the model
    # ------------------------------------------------------------------

    def cmd_paste_response(self, args):
        """Paste an LLM response from the clipboard and apply it as if the model returned it.

        This lets you copy context to a web UI, get a response, and paste
        the response back to have aider apply the edits.
        """
        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        text = None
        if args and args.strip():
            text = args.strip()
        else:
            try:
                text = pyperclip.paste()
            except Exception as e:
                self.io.tool_error(f"Failed to read clipboard: {e}")
                return

        if not text or not text.strip():
            self.io.tool_error("No response text found in clipboard.")
            return

        apply_pasted_response(self.coder, text.strip())

    # ------------------------------------------------------------------
    # Standard command overrides
    # ------------------------------------------------------------------

    def hidden_commands(self):
        """Return commands that should be removed from the interface."""
        return {"/test", "/code", "/architect", "/context", "/map", "/map-refresh"}

    def cmd_drop(self, args):
        """Remove files from the chat session (core db entries are re-loaded automatically)."""
        if self._parent_commands:
            self._parent_commands.cmd_drop(args)
            # Re-load core context after any drop operation
            self._reload_core_context()

    def _reload_core_context(self):
        """Ensure all db/core/ entries are in the read-only context."""
        from .novel_coder import load_core_context

        if self.coder:
            load_core_context(self.coder)

    def cmd_query(self, args):
        """Query the manuscript without editing any files."""
        if self._parent_commands:
            return self._parent_commands.cmd_query(args)

    def cmd_feedback(self, args):
        """Critique a section and provide prioritized suggestions.

        Loads the specified narrative location or files as read-only context
        and asks the LLM to provide a structured critique with prioritized
        suggestions (2-3 high, 2-3 medium, 2-3 low).

        Usage:
            /feedback act 1 chapter 2 scene 3
            /feedback 1 2 3        (shorthand: act chapter scene)
            /feedback act 1 chapter 2
            /feedback 1 2          (shorthand: act chapter)
            /feedback act 1
            /feedback path/to/file.md
        """
        from aider.coders.base_coder import Coder
        from aider.commands import SwitchCoder

        files = self._get_target_files(args)
        if not files:
            self.io.tool_error(
                "No files found. Specify a narrative location or file path.\n"
                "Usage: /feedback [act N] [chapter N] [scene N]\n"
                "       /feedback N [N] [N]  (shorthand)\n"
                "       /feedback path/to/file.md"
            )
            return

        # Report what we're critiquing
        rel_names = sorted(
            os.path.relpath(f, self.root) if self.root else f for f in files
        )
        self.io.tool_output(
            f"Requesting feedback on {len(rel_names)} file(s):\n"
            + "\n".join(f"  {n}" for n in rel_names)
        )

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        # Spin up a temporary query-mode coder with these files as read-only
        coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            edit_format="query",
            summarize_from_coder=False,
        )

        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()

        # Clear chat history — feedback is a stateless one-shot call.
        # Keeping the main coder's done_messages would bloat the context
        # and reference files that are no longer present.
        coder.done_messages = []
        coder.cur_messages = []

        for path in files:
            coder.abs_read_only_fnames.add(os.path.abspath(path))

        # Re-add core context (style guide, etc.)
        from .novel_coder import load_core_context

        load_core_context(coder)

        prompt = (
            "You are a skilled fiction editor. Critique the provided manuscript "
            "section(s) and provide a prioritized list of actionable suggestions "
            "for improvement.\n\n"
            "Structure your response as follows:\n\n"
            "## High Priority (2-3 items)\n"
            "Critical issues that significantly impact the quality of the writing "
            "(e.g. plot holes, character inconsistencies, pacing problems, unclear "
            "prose, structural issues).\n\n"
            "## Medium Priority (2-3 items)\n"
            "Important improvements that would noticeably strengthen the work "
            "(e.g. dialogue refinement, better scene transitions, deepening "
            "characterization, tightening prose).\n\n"
            "## Low Priority (2-3 items)\n"
            "Polish-level suggestions for fine-tuning "
            "(e.g. word choice tweaks, minor rhythm improvements, small "
            "atmospheric details, subtle foreshadowing opportunities).\n\n"
            "For each suggestion, be specific: quote the relevant passage, "
            "explain the issue, and suggest a concrete improvement.\n"
            "Reference the project's style guide if provided in context.\n"
        )

        coder.run(prompt)

        raise SwitchCoder(
            edit_format=self.coder.edit_format,
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=False,
        )

    def completions_feedback(self):
        return [l.lower() for l in self.levels]

    def cmd_auto_context(self, args):
        """Toggle automatic context identification before each LLM call.

        Usage:
            /auto-context          Toggle on/off
            /auto-context on       Enable
            /auto-context off      Disable
        """
        from .config import load_config, save_config

        coder = self.coder
        arg = args.strip().lower()

        if arg in ("on", "true", "1", "yes"):
            new_val = True
        elif arg in ("off", "false", "0", "no"):
            new_val = False
        else:
            # Toggle
            new_val = not getattr(coder, "_auto_context_enabled", True)

        coder._auto_context_enabled = new_val

        # Persist to .composez
        root = getattr(coder, "root", None) or os.getcwd()
        config = load_config(root)
        config["auto_context"] = new_val
        save_config(root, config)

        status = "enabled" if new_val else "disabled"
        self.io.tool_output(f"Auto-context {status}.")

    def cmd_auto_lint(self, args):
        """Toggle automatic prose linting after edits.

        When enabled, Vale runs on edited markdown files and automatically
        fixes any issues it finds.  Changes the session state only — the
        default for new sessions is controlled by ``auto_lint`` in
        ``.composez``.

        Usage:
            /auto-lint          Toggle on/off
            /auto-lint on       Enable
            /auto-lint off      Disable
        """
        coder = self.coder
        arg = args.strip().lower()

        if arg in ("on", "true", "1", "yes"):
            new_val = True
        elif arg in ("off", "false", "0", "no"):
            new_val = False
        else:
            # Toggle
            new_val = not getattr(coder, "_novel_auto_lint", True)

        coder._novel_auto_lint = new_val

        status = "enabled" if new_val else "disabled"
        self.io.tool_output(f"Auto-lint {status}.")

    def cmd_compose(self, args):
        """Enter compose mode using 2 different models."""
        if self._parent_commands:
            return self._parent_commands.cmd_architect(args)

    def cmd_agent(self, args):
        """Plan and execute multi-step tasks using an orchestrating agent."""
        from aider.commands import SwitchCoder

        if not args.strip():
            # Keep the current edit format — if the user is in selection
            # mode, the agent will see the selection context.
            raise SwitchCoder(
                autonomy="agent",
                summarize_from_coder=False,
            )

        # One-shot: spin up an agent coder, run the prompt, return
        from aider.coders.base_coder import Coder

        coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            autonomy="agent",
            summarize_from_coder=False,
        )

        coder.run(args)

        raise SwitchCoder(
            edit_format=self.coder.edit_format,
            autonomy="direct",
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=False,
        )

    def completions_agent(self):
        from aider.io import CommandCompletionException

        raise CommandCompletionException()

    def cmd_select(self, args):
        """Set a text selection and switch to selection mode."""
        from aider.commands import SwitchCoder

        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /select filename:start_line:start_col-end_line:end_col"
            )
            return

        parsed = self._parse_selection_arg(args)
        if parsed is None:
            return

        fname, sel_range, sel_text = parsed

        # Ensure the file is in the chat
        abs_path = os.path.join(self.root, fname)
        if self.coder and abs_path not in self.coder.abs_fnames:
            self.coder.abs_fnames.add(abs_path)
            self.io.tool_output(f"Added {fname} to the chat.")

        # Display what was selected
        range_desc = (
            f"{sel_range['start']['line']+1}:{sel_range['start']['character']+1}"
            f"-{sel_range['end']['line']+1}:{sel_range['end']['character']+1}"
        )
        preview = sel_text if len(sel_text) <= 80 else sel_text[:77] + "..."
        self.io.tool_output(f"Selected {fname} [{range_desc}]: {preview}")

        raise SwitchCoder(
            edit_format="selection",
            summarize_from_coder=False,
            selection_filename=fname,
            selection_range=sel_range,
            selection_text=sel_text,
        )

    def cmd_selection(self, args):
        """Enter selection editing mode."""
        if self._parent_commands:
            return self._parent_commands._generic_chat_command(args, "selection")

    def _parse_selection_arg(self, arg):
        """Parse ``filename:start_line:start_col-end_line:end_col`` and return
        ``(rel_filename, lsp_range_dict, selected_text)`` or ``None`` on error.

        Line and column numbers in the argument are **1-based** (user-facing).
        The returned LSP Range uses 0-based indices.
        """
        # Expected: path/to/file.md:10:1-12:45
        m = re.match(
            r'^(.+?):(\d+):(\d+)-(\d+):(\d+)$', arg
        )
        if not m:
            self.io.tool_error(
                "Invalid selection format. "
                "Expected: filename:start_line:start_col-end_line:end_col "
                "(1-based line and column numbers)"
            )
            return None

        fname = m.group(1)
        start_line_1 = int(m.group(2))
        start_col_1 = int(m.group(3))
        end_line_1 = int(m.group(4))
        end_col_1 = int(m.group(5))

        # Convert to 0-based
        start_line = start_line_1 - 1
        start_char = start_col_1 - 1
        end_line = end_line_1 - 1
        end_char = end_col_1 - 1

        # Resolve the file path
        abs_path = os.path.join(self.root, fname)
        if not os.path.isfile(abs_path):
            self.io.tool_error(f"File not found: {fname}")
            return None

        # Read and extract the selected text
        try:
            content = Path(abs_path).read_text(encoding="utf-8")
        except OSError as e:
            self.io.tool_error(f"Cannot read {fname}: {e}")
            return None

        lines = content.splitlines(keepends=True)

        if start_line < 0 or end_line >= len(lines):
            self.io.tool_error(
                f"Line range {start_line_1}-{end_line_1} is out of bounds "
                f"(file has {len(lines)} lines)."
            )
            return None

        # Extract the selected text
        if start_line == end_line:
            sel_text = lines[start_line][start_char:end_char]
        else:
            parts = [lines[start_line][start_char:]]
            parts.extend(lines[start_line + 1 : end_line])
            if end_line < len(lines):
                parts.append(lines[end_line][:end_char])
            sel_text = "".join(parts)

        sel_range = {
            "start": {"line": start_line, "character": start_char},
            "end": {"line": end_line, "character": end_char},
        }

        return fname, sel_range, sel_text

    def cmd_ls(self, args):
        """List narrative structure, db entries, and which files are in the chat."""
        # Show narrative structure
        self.narrative_map.refresh()
        outline = self.narrative_map.get_outline(include_summaries=False)
        if outline:
            self.io.tool_output("Narrative structure:\n")
            self.io.tool_output(outline)
        else:
            self.io.tool_output("No narrative structure found.\n")

        # Show db entries
        categories = self.db.get_categories()
        if categories:
            self.io.tool_output("\nDb entries:\n")
            for cat in categories:
                entries = self.db.get_entries_by_category(cat)
                for entry in entries:
                    self.io.tool_output(f"  {cat}/{entry.name}")

        # Show what's in the chat
        if self.coder:
            chat_files = set()
            read_only_files = set()
            for abs_path in self.coder.abs_fnames:
                chat_files.add(os.path.relpath(abs_path, self.root))
            for abs_path in self.coder.abs_read_only_fnames:
                read_only_files.add(os.path.relpath(abs_path, self.root))

            if chat_files:
                self.io.tool_output("\nFiles in chat:\n")
                for f in self._collapse_paths(chat_files):
                    wc = self._word_count_for_path(f)
                    self.io.tool_output(f"  {f} ({wc:,} words)")
            if read_only_files:
                self.io.tool_output("\nRead-only files:\n")
                for f in self._collapse_paths(read_only_files):
                    wc = self._word_count_for_path(f)
                    self.io.tool_output(f"  {f} ({wc:,} words)")

    # ------------------------------------------------------------------
    # /grep — search text files for a pattern
    # ------------------------------------------------------------------

    def cmd_grep(self, args):
        """Search text files for a pattern.

        Usage:
            /grep <pattern> [location]
            /grep -i <pattern> [location]

        Flags: -i (case-insensitive), -c (count only), -l (files only).
        Location can be narrative notation (act 1 chapter 2), shorthand
        (1 2), or a file/directory path. Defaults to novel/.
        """
        if not args.strip():
            self.io.tool_error(
                "Usage: /grep [-i] [-c] [-l] <pattern> [location]"
            )
            return

        pattern, flags, location_args = self._parse_grep_args(args)
        if pattern is None:
            return

        try:
            regex = re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
        except re.error as e:
            self.io.tool_error(f"Invalid regex: {e}")
            return

        files = self._grep_resolve_files(location_args)
        if not files:
            self.io.tool_warning("No files to search.")
            return

        count_only = "c" in flags
        files_only = "l" in flags
        total_matches = 0
        output_lines = []

        for fpath in sorted(files):
            try:
                text = Path(fpath).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            lines = text.splitlines()
            file_matches = []
            for lineno, line in enumerate(lines, 1):
                if regex.search(line):
                    file_matches.append((lineno, line))

            if not file_matches:
                continue

            rel = os.path.relpath(fpath, self.root)
            total_matches += len(file_matches)

            if files_only:
                output_lines.append(rel)
            elif count_only:
                output_lines.append(f"{rel}: {len(file_matches)}")
            else:
                for lineno, line in file_matches:
                    output_lines.append(f"{rel}:{lineno}: {line}")

        if output_lines:
            self.io.tool_output("\n".join(output_lines))
        else:
            self.io.tool_output("No matches found.")

        if not files_only and not count_only:
            self.io.tool_output(f"\n{total_matches} match(es) in {len(files)} file(s).")

    def _parse_grep_args(self, args):
        """Parse grep arguments into (pattern, flags, location_args).

        Returns ``(pattern, flags, location_args)`` or ``(None, None, None)``
        on error.  *flags* is a string of single-char flags (e.g. ``"ic"``).
        """
        import shlex

        try:
            tokens = shlex.split(args)
        except ValueError:
            tokens = args.split()

        flags = ""
        rest = []
        for token in tokens:
            if token.startswith("-") and len(token) > 1 and not token[1:].isdigit():
                flags += token[1:]
            else:
                rest.append(token)

        if not rest:
            self.io.tool_error("No search pattern provided.")
            return None, None, None

        pattern = rest[0]
        location_args = " ".join(rest[1:])
        return pattern, flags, location_args

    def _grep_resolve_files(self, location_args):
        """Resolve location args to a list of text files to search.

        Falls back to the entire novel directory when no location is given.
        """
        if location_args.strip():
            # Try narrative location first
            parsed = self._parse_location_args(location_args)
            if parsed is not None:
                node = self._resolve_node(parsed)
                if node is not None:
                    return self._collect_text_files(node.path)
                return []

            # Try as a literal file or directory path
            path = (
                os.path.join(self.root, location_args)
                if not os.path.isabs(location_args)
                else location_args
            )
            if os.path.isfile(path):
                return [path]
            if os.path.isdir(path):
                return self._collect_text_files(path)

            self.io.tool_warning(f"Location not found: {location_args}")
            return []

        # Default: search the novel directory
        if os.path.isdir(self.novel_root):
            return self._collect_text_files(self.novel_root)
        return []

    def _collect_text_files(self, directory):
        """Collect all .md files under *directory*, recursively."""
        files = []
        for dirpath, _dirnames, filenames in os.walk(directory):
            for fname in sorted(filenames):
                if fname.endswith(".md") and not fname.startswith("."):
                    files.append(os.path.join(dirpath, fname))
        return files

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _report_added(self, rel_paths):
        """Print a collapsed, word-counted summary of files just added to the chat."""
        for entry in self._collapse_paths(rel_paths):
            wc = self._word_count_for_path(entry)
            self.io.tool_output(f"Added {entry} to the chat ({wc:,} words)")

    def _collapse_paths(self, rel_paths):
        """Collapse file paths into parent directories when all entries are present.

        Delegates to the standalone :func:`collapse_paths` in ``novel_coder``.
        """
        from .novel_coder import collapse_paths

        return collapse_paths(self.root, rel_paths)

    def _word_count_for_path(self, rel_path):
        """Return the word count for a file or directory (recursive)."""
        full = os.path.join(self.root, rel_path)
        if os.path.isfile(full):
            try:
                text = Path(full).read_text(encoding="utf-8")
                return len(text.split())
            except OSError:
                return 0
        if os.path.isdir(full):
            total = 0
            for dirpath, _dirnames, filenames in os.walk(full):
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    try:
                        text = Path(os.path.join(dirpath, fname)).read_text(encoding="utf-8")
                        total += len(text.split())
                    except (OSError, UnicodeDecodeError):
                        continue
            return total
        return 0

    def _get_target_files(self, args):
        """Get files to operate on — from location args, paths, or the chat.

        Resolution order:
        1. Narrative location (``act 1 chapter 2``, ``1 2 3``, etc.)
        2. Literal file paths
        3. Files currently in the chat
        """
        args = args.strip()
        if args:
            # Try narrative location first
            files = self._location_to_files(args)
            if files is not None:
                return files

            # Fall back to treating args as file paths
            fnames = []
            for word in args.split():
                path = os.path.join(self.root, word) if not os.path.isabs(word) else word
                if os.path.isfile(path):
                    fnames.append(path)
                else:
                    self.io.tool_warning(f"File not found: {word}")
            return fnames

        # Default to files currently in the chat
        if self.coder and self.coder.abs_fnames:
            return sorted(self.coder.abs_fnames)

        return []

    def _touch(self, path):
        """Create an empty file if it doesn't exist."""
        if not os.path.exists(path):
            Path(path).touch()

    def _find_last_level_number(self, search_dir, level_name):
        """Find the highest numbered ``Level N`` directory in *search_dir*."""
        if not os.path.isdir(search_dir):
            return None
        level_re = _build_level_re([level_name])
        max_num = None
        for name in os.listdir(search_dir):
            m = level_re.match(name)
            if m and os.path.isdir(os.path.join(search_dir, name)):
                num = int(m.group(2))
                if max_num is None or num > max_num:
                    max_num = num
        return max_num

    def _find_level_dir(self, search_dir, level_name, number):
        """Find the ``Level <number>`` directory in *search_dir*."""
        if not os.path.isdir(search_dir):
            return None
        level_re = _build_level_re([level_name])
        for name in os.listdir(search_dir):
            m = level_re.match(name)
            if m and int(m.group(2)) == number:
                full = os.path.join(search_dir, name)
                if os.path.isdir(full):
                    return full
        return None

    def _next_level_number(self, search_dir, level_name):
        """Get the next available number for *level_name* in a directory."""
        last = self._find_last_level_number(search_dir, level_name)
        return (last or 0) + 1

    def get_commands(self):
        """Return a dict of command_name -> method for integration."""
        commands = {}
        for attr in dir(self):
            if attr.startswith("cmd_"):
                name = attr[4:].replace("_", "-")
                commands[name] = getattr(self, attr)
        return commands

    # ------------------------------------------------------------------
    # /summarize — generate or update SUMMARY.md from prose
    # ------------------------------------------------------------------

    def cmd_summarize(self, args):
        """Generate summaries for leaf scenes from their prose.

        For scenes: summarizes the PROSE.md into SUMMARY.md.
        For chapters/acts: drills down to all descendant leaf scenes
            and generates a new SUMMARY.md for each.

        Usage:
            /summarize act 1 chapter 2 scene 3
            /summarize 1 2 3        (shorthand: act chapter scene)
            /summarize act 1 chapter 2
            /summarize 1 2          (shorthand: act chapter)
            /summarize act 1
            /summarize 1            (shorthand: act)
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /summarize [act N] [chapter N] [scene N]\n"
                "       /summarize N [N] [N]  (shorthand)"
            )
            return

        parsed = self._parse_location_args(args)
        if parsed is None:
            return

        node = self._resolve_node(parsed)
        if node is None:
            return

        if node.is_leaf:
            self.io.tool_output(f"Summarizing {self._node_label(node)}...")
            return self._summarize_scene(node)

        # Non-leaf: collect all descendant leaf scenes and summarize each
        leaves = self._collect_leaf_nodes(node)
        if not leaves:
            self.io.tool_error(
                f"No leaf scenes found under {self._node_label(node)}. "
                "Create scenes first with /new scene."
            )
            return

        # Check that at least one has prose
        scenes_with_prose = [
            leaf for leaf in leaves
            if self._has_content(os.path.join(leaf.path, NarrativeMap.PROSE_FILE))
        ]
        if not scenes_with_prose:
            self.io.tool_error(
                "No scenes have prose to summarize. "
                "Write scenes first with /write."
            )
            return

        self.io.tool_output(
            f"Summarizing {len(scenes_with_prose)} scene(s) "
            f"under {self._node_label(node)}..."
        )

        # Batch: collect all edit and read-only paths for all scenes
        edit_paths = []
        read_only_paths = []
        for leaf in scenes_with_prose:
            prose_path = os.path.join(leaf.path, NarrativeMap.PROSE_FILE)
            summary_path = os.path.join(leaf.path, NarrativeMap.SUMMARY_FILE)
            self._touch(summary_path)
            edit_paths.append(summary_path)
            read_only_paths.append(prose_path)

        prompt = (
            "Read the prose for each scene and write a concise summary "
            "for each scene's SUMMARY.md.\n\n"
            "Each summary should:\n"
            "- Start directly with the summary, no title required (titles are captured in the directory names)\n"
            "- Start with a short title for the scene on the first line\n"
            "- Capture the key events, character dynamics, and emotional beats\n"
            "- Note any important revelations, decisions, or turning points\n"
        )

        self._run_with_files(
            prompt=prompt,
            edit_paths=edit_paths,
            read_only_paths=read_only_paths,
        )

    def _summarize_scene(self, node):
        """Summarize a scene's prose into its SUMMARY.md."""
        prose_path = os.path.join(node.path, NarrativeMap.PROSE_FILE)
        if not self._has_content(prose_path):
            self.io.tool_error("Scene has no prose to summarize.")
            return

        summary_path = os.path.join(node.path, NarrativeMap.SUMMARY_FILE)
        self._touch(summary_path)

        prompt = (
            "Read the scene prose and write a concise summary for its SUMMARY.md.\n\n"
            "The summary should:\n"
            "- Start directly with the summary, no title required (titles are captured in the directory names)\n"
            "- Capture the key events, character dynamics, and emotional beats\n"
            "- Note any important revelations, decisions, or turning points\n"
        )

        self._run_with_files(
            prompt=prompt,
            edit_paths=[summary_path],
            read_only_paths=[prose_path],
        )

    def _collect_leaf_nodes(self, node):
        """Recursively collect all leaf nodes under a given node."""
        if node.is_leaf:
            return [node]
        leaves = []
        for child in node.children:
            leaves.extend(self._collect_leaf_nodes(child))
        return leaves

    def completions_summarize(self):
        return [l.lower() for l in self.levels]

    # ------------------------------------------------------------------
    # /write — generate PROSE.md from summaries
    # ------------------------------------------------------------------

    def cmd_write(self, args):
        """Write prose for a scene, chapter, or act from its summaries.

        For scenes: writes PROSE.md from the scene's SUMMARY.md.
        For chapters: writes all scene PROSE.md files, using the chapter
            and scene summaries as context.
        For acts: writes all scene PROSE.md files across all chapters.

        Usage:
            /write act 1 chapter 2 scene 3
            /write 1 2 3        (shorthand: act chapter scene)
            /write act 1 chapter 2
            /write 1 2          (shorthand: act chapter)
            /write act 1
            /write 1            (shorthand: act)
        """
        args = args.strip()
        if not args:
            self.io.tool_error(
                "Usage: /write [act N] [chapter N] [scene N]\n"
                "       /write N [N] [N]  (shorthand)"
            )
            return

        parsed = self._parse_location_args(args)
        if parsed is None:
            return

        node = self._resolve_node(parsed)
        if node is None:
            return

        # Delete existing PROSE.md files via git before writing fresh ones
        self._git_delete_prose(node)

        self.io.tool_output(f"Writing {self._node_label(node)}...")

        if node.is_leaf:
            return self._write_scene(node)
        elif len(parsed) == len(self.levels) - 1:
            return self._write_chapter(node)
        else:
            return self._write_act(node)

    def _git_delete_prose(self, node):
        """Delete and stage all PROSE.md files under *node*."""
        prose_files = self._collect_prose_files(node)
        if not prose_files:
            return

        repo = self.coder.repo if self.coder else None
        if not repo:
            return

        # Remove the files from disk
        rel_paths = []
        for path in prose_files:
            abs_path = os.path.abspath(path)
            rel = os.path.relpath(path, self.root) if self.root else path
            rel_paths.append(rel)
            try:
                os.remove(abs_path)
            except OSError:
                pass
            # Remove from coder's file sets
            if self.coder:
                self.coder.abs_fnames.discard(abs_path)
                self.coder.abs_read_only_fnames.discard(abs_path)

        # Stage the deletions (commit happens later via auto-commit)
        try:
            repo.repo.git.add("-A", "--", *[os.path.abspath(p) for p in prose_files])
            self.io.tool_output(
                f"Deleted {len(rel_paths)} PROSE.md file(s)"
            )
        except Exception as e:
            self.io.tool_error(f"Git staging of prose deletions failed: {e}")

        self._narrative_map = None

    def _write_scene(self, node):
        """Write prose for a single scene."""
        summary_path = os.path.join(node.path, NarrativeMap.SUMMARY_FILE)
        if not self._has_content(summary_path):
            self.io.tool_error(
                "Scene has no summary. "
                "Create one first with /summarize or by writing SUMMARY.md."
            )
            return

        target_path = os.path.join(node.path, NarrativeMap.PROSE_FILE)
        self._touch(target_path)

        read_only_paths = [summary_path]
        self._add_sibling_summaries(node, read_only_paths)

        prompt = (
            "Based on the scene summary and surrounding context, "
            "write the full prose for this scene's PROSE.md.\n\n"
            "Follow the project's style guide (provided as read-only context) "
            "and bring the events from the summary to life as a fully realized scene.\n"
        )

        self._run_with_files(
            prompt=prompt,
            edit_paths=[target_path],
            read_only_paths=read_only_paths,
        )

    def _write_chapter(self, node):
        """Write prose for all scenes in a chapter."""
        edit_paths = []
        read_only_paths = []

        # Each scene: PROSE.md is editable, SUMMARY.md is read-only context
        for scene in node.children:
            summary_path = os.path.join(scene.path, NarrativeMap.SUMMARY_FILE)
            if self._has_content(summary_path):
                read_only_paths.append(summary_path)

            prose_path = os.path.join(scene.path, NarrativeMap.PROSE_FILE)
            self._touch(prose_path)
            edit_paths.append(prose_path)

        if not edit_paths:
            self.io.tool_error(
                "No scenes found in this chapter. "
                "Create scenes first with /new scene."
            )
            return

        if not any(self._has_content(p) for p in read_only_paths):
            self.io.tool_error(
                "No scene summaries found. "
                "Summarize scenes first with /summarize."
            )
            return

        # Also include any .md files from the parent node directory as context
        self._add_dir_md_files(node, read_only_paths)

        prompt = (
            "Write the full prose for every scene in this chapter.\n\n"
            "The individual scene summaries are provided as read-only context. "
            "Write each scene's PROSE.md as a complete, fully realized "
            "narrative passage.\n\n"
            "Follow the project's style guide (provided as read-only context). "
            "Ensure smooth transitions between scenes — "
            "each scene should flow naturally from the previous one.\n"
        )

        self._run_with_files(
            prompt=prompt,
            edit_paths=edit_paths,
            read_only_paths=read_only_paths,
        )

    def _write_act(self, node):
        """Write prose for all scenes across all chapters in an act."""
        edit_paths = []
        read_only_paths = []

        # Include any .md files in the act directory as context
        self._add_dir_md_files(node, read_only_paths)

        for chapter in node.children:
            # Include any .md files in chapter directories as context
            self._add_dir_md_files(chapter, read_only_paths)

            for scene in chapter.children:
                summary_path = os.path.join(scene.path, NarrativeMap.SUMMARY_FILE)
                if self._has_content(summary_path):
                    read_only_paths.append(summary_path)

                prose_path = os.path.join(scene.path, NarrativeMap.PROSE_FILE)
                self._touch(prose_path)
                edit_paths.append(prose_path)

        if not edit_paths:
            self.io.tool_error(
                "No scenes found in this act. "
                "Create chapters and scenes first."
            )
            return

        if not any(self._has_content(p) for p in read_only_paths):
            self.io.tool_error(
                "No scene summaries found. "
                "Summarize scenes first with /summarize."
            )
            return

        prompt = (
            "Write the full prose for every scene in this act.\n\n"
            "The scene summaries are provided as read-only context. "
            "Write each scene's PROSE.md as a complete, fully realized "
            "narrative passage.\n\n"
            "Follow the project's style guide (provided as read-only context). "
            "Ensure smooth transitions between scenes and chapters — "
            "each scene should flow naturally from the previous one.\n"
        )

        self._run_with_files(
            prompt=prompt,
            edit_paths=edit_paths,
            read_only_paths=read_only_paths,
        )

    def completions_write(self):
        return [l.lower() for l in self.levels]

    # ------------------------------------------------------------------
    # Shared helpers for /summarize and /write
    # ------------------------------------------------------------------

    def _has_content(self, path):
        """Check if a file exists and has non-empty content."""
        return (
            os.path.isfile(path)
            and Path(path).read_text(encoding="utf-8").strip()
        )

    def _location_to_files(self, args):
        """Resolve narrative location args to a list of absolute file paths.

        Accepts keyword (``act 1 chapter 2``) or shorthand (``1 2``) syntax.
        Returns a list of absolute paths to every SUMMARY.md and PROSE.md
        under the resolved node, or ``None`` if *args* don't look like a
        valid narrative location.
        """
        parsed = self._parse_location_args(args)
        if parsed is None:
            return None
        node = self._resolve_node(parsed)
        if node is None:
            # Location syntax was valid but node doesn't exist — return
            # empty list so callers don't fall through to file-path parsing.
            return []
        paths = self._collect_all_narrative_files(node)
        return [os.path.abspath(p) for p in paths] if paths else []

    def _parse_location_args(self, args):
        """Parse location arguments into a list of level numbers.

        Supports both keyword and shorthand syntax:
            act 1 chapter 2 scene 3  →  [1, 2, 3]
            1 2 3                    →  [1, 2, 3]
            act 1 chapter 2          →  [1, 2]
            1 2                      →  [1, 2]
            act 1                    →  [1]
            1                        →  [1]

        Returns a list of ints, or None on error.
        """
        tokens = args.split()
        levels = self.levels
        level_names = [l.lower() for l in levels]
        pos = 0
        nums = []

        # Try keyword syntax first
        if tokens[0].lower() in level_names:
            while pos < len(tokens) and tokens[pos].lower() in level_names:
                level_word = tokens[pos].lower()
                pos += 1
                if pos >= len(tokens) or not tokens[pos].isdigit():
                    self.io.tool_error(
                        f"Expected a number after '{level_word}'."
                    )
                    return None
                nums.append(int(tokens[pos]))
                pos += 1

            if not nums:
                self.io.tool_error("At minimum, one level number is required.")
                return None
            return nums

        # Shorthand: just numbers
        for token in tokens:
            if token.isdigit():
                nums.append(int(token))
            else:
                level_list = " ".join(
                    f"[{l.lower()} N]" for l in levels
                )
                self.io.tool_error(
                    f"Unexpected '{token}'. Use: {level_list} "
                    "or just numbers."
                )
                return None

        if not nums:
            self.io.tool_error("At minimum, one level number is required.")
            return None

        if len(nums) > len(levels):
            self.io.tool_error(
                f"Too many numbers. Max {len(levels)} levels: "
                + ", ".join(l.lower() for l in levels) + "."
            )
            return None

        return nums

    def _resolve_node(self, nums):
        """Find the narrative node for the given level numbers, with error messages."""
        self.narrative_map.refresh()
        node = self.narrative_map.find_node(*nums)
        if node is None:
            levels = self.levels
            parts = [
                f"{levels[i].lower()} {nums[i]}"
                for i in range(len(nums))
            ]
            self.io.tool_error(f"Not found: {', '.join(parts)}.")
            return None
        return node

    def _node_label(self, node):
        """Return a human-readable label for a narrative node.

        Walks from the node up to the root to build a full location string.
        Examples:
            "Act 1 (The Rising Action)"
            "Act 2, Chapter 1 (The Storm)"
            "Act 1, Chapter 2, Scene 3 (The Encounter)"
        """
        # Walk path segments to build label from collapsed dir names
        rel = os.path.relpath(node.path, self.novel_root)
        segments = rel.replace(os.sep, "/").split("/")
        levels = self.levels
        parts = []
        for segment in segments:
            parsed = parse_level_dir(segment, levels)
            if parsed is None:
                continue
            level_name, number, title = parsed
            if segment == os.path.basename(node.path):
                # Target node — include title
                if title:
                    parts.append(f"{level_name} {number} ({title})")
                else:
                    parts.append(f"{level_name} {number}")
            else:
                # Ancestor — just number
                parts.append(f"{level_name} {number}")

        return ", ".join(parts)

    def _add_dir_md_files(self, node, paths):
        """Add any .md files from a non-leaf node's directory to paths for context."""
        if not os.path.isdir(node.path):
            return
        for name in sorted(os.listdir(node.path)):
            if name.endswith(".md"):
                fpath = os.path.join(node.path, name)
                if os.path.isfile(fpath) and fpath not in paths:
                    if self._has_content(fpath):
                        paths.append(fpath)

    def _add_sibling_summaries(self, node, paths):
        """Add adjacent sibling summaries for continuity context."""
        parent_dir = os.path.dirname(node.path)
        if not os.path.isdir(parent_dir):
            return

        # Find all siblings at the same level
        level_re = _build_level_re([node.kind])
        siblings = []
        for name in sorted(os.listdir(parent_dir), key=natural_sort_key):
            m = level_re.match(name)
            if m:
                sibling_path = os.path.join(parent_dir, name)
                if os.path.isdir(sibling_path) and sibling_path != node.path:
                    siblings.append((int(m.group(2)), sibling_path))

        # Add previous and next scene summaries
        for num, sib_path in siblings:
            if abs(num - node.number) <= 1:
                summary = os.path.join(sib_path, NarrativeMap.SUMMARY_FILE)
                if os.path.isfile(summary) and Path(summary).read_text(
                    encoding="utf-8"
                ).strip():
                    if summary not in paths:
                        paths.append(summary)

    def _run_with_files(self, prompt, edit_paths, read_only_paths):
        """Run the LLM with specified editable and read-only files."""
        from aider.coders.base_coder import Coder
        from aider.commands import SwitchCoder

        if not self.coder:
            self.io.tool_error("No coder available.")
            return

        # Use the parent coder's edit format — novel mode is activated
        # automatically by Coder.create()
        coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            summarize_from_coder=False,
        )

        # Clear file sets and set up exactly what we need
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()

        # Auto-create SUMMARY.md and PROSE.md without prompting
        coder.auto_create_fnames = {
            NarrativeMap.SUMMARY_FILE,
            NarrativeMap.PROSE_FILE,
        }

        for path in edit_paths:
            coder.abs_fnames.add(os.path.abspath(path))
        for path in read_only_paths:
            coder.abs_read_only_fnames.add(os.path.abspath(path))

        # Re-add core context (style guide, etc.) — cleared above,
        # but skip any files that are already edit targets to avoid
        # the same file appearing in both editable and read-only sets.
        from .novel_coder import load_core_context

        load_core_context(coder)
        coder.abs_read_only_fnames -= coder.abs_fnames

        coder.run(prompt)

        raise SwitchCoder(
            edit_format=self.coder.edit_format,
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=False,
        )

    # ------------------------------------------------------------------
    # /edit — novel-friendly alias for /code (mode switch)
    # ------------------------------------------------------------------

    def cmd_edit(self, args):
        """Ask for changes to your manuscript."""
        from aider.commands import SwitchCoder

        if not args.strip():
            # Switch mode — same as /code with no args
            edit_format = "code"
            if self.coder:
                edit_format = self.coder.main_model.edit_format
            raise SwitchCoder(
                edit_format=edit_format,
                summarize_from_coder=False,
            )

        # One-shot prompt — run with the coder's preferred edit format
        # Novel mode is activated automatically by Coder.create()
        from aider.coders.base_coder import Coder

        edit_format = self.coder.main_model.edit_format if self.coder else "whole"

        coder = Coder.create(
            io=self.io,
            from_coder=self.coder,
            edit_format=edit_format,
            summarize_from_coder=False,
        )

        coder.run(args)

        raise SwitchCoder(
            edit_format=self.coder.edit_format,
            summarize_from_coder=False,
            from_coder=coder,
            show_announcements=False,
        )

    def completions_edit(self):
        from aider.io import CommandCompletionException

        raise CommandCompletionException()

    # ------------------------------------------------------------------
    # /save — save chat history or context to cache
    # ------------------------------------------------------------------

    def _save_cache_dir(self, kind):
        """Return the cache directory for a save kind ('chat' or 'context')."""
        return os.path.join(self.root, "cache", kind)

    def _ensure_cache_dirs(self):
        """Create cache/chat and cache/context if they don't exist."""
        for kind in ("chat", "context"):
            os.makedirs(self._save_cache_dir(kind), exist_ok=True)

    def _parse_save_args(self, args, cmd="save"):
        """Parse save/load args into (kind, name).

        Returns (kind, name) or None on error.
        kind is 'chat' or 'context'.
        name defaults to 'default'.
        """
        tokens = args.strip().split()
        if not tokens:
            self.io.tool_error(f"Usage: /{cmd} chat|ctx [name]")
            return None

        kind_token = tokens[0].lower()
        if kind_token == "chat":
            kind = "chat"
        elif kind_token in ("ctx", "context"):
            kind = "context"
        else:
            self.io.tool_error(
                f"Unknown {cmd} target: {kind_token}. Use 'chat' or 'ctx'."
            )
            return None

        name = tokens[1] if len(tokens) > 1 else "default"
        return kind, name

    def _save_path(self, kind, name):
        """Return the full path for a save file."""
        return os.path.join(self._save_cache_dir(kind), f"{name}.yml")

    def _resolve_ctx_path(self, name, must_exist=False):
        """Resolve a context save/load path.

        If *name* looks like a file path (contains ``/`` or ends with
        ``.yml``/``.yaml``), resolve it relative to the project root.
        For loads (*must_exist=True*), falls back to
        ``cache/context/{name}.yml`` if the direct path doesn't exist.
        Otherwise uses the standard cache location.
        """
        is_path = "/" in name or name.endswith((".yml", ".yaml"))
        if is_path:
            direct = os.path.join(self.root, name)
            if must_exist and not os.path.isfile(direct):
                return self._save_path("context", name)
            return direct
        return self._save_path("context", name)

    def cmd_save(self, args):
        """Save chat history or file context to a named cache slot.

        Usage:
            /save chat [name]     — save chat history
            /save ctx [name]      — save added files (editable + read-only)
            /save ctx path/to/file.yml — save to a specific file path

        If no name is given, saves to 'default'.
        Files are stored as YAML in cache/chat/ or cache/context/.
        A name containing '/' or ending in .yml is treated as a direct path.
        """
        parsed = self._parse_save_args(args)
        if parsed is None:
            return

        kind, name = parsed

        if kind == "chat":
            self._ensure_cache_dirs()
            self._save_chat(name)
        else:
            path = self._resolve_ctx_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._save_context_to(path)

    def _save_chat(self, name):
        """Save done_messages + cur_messages to a YAML file."""
        messages = []
        for msg in self.coder.done_messages:
            messages.append({"role": msg["role"], "content": msg["content"]})
        for msg in self.coder.cur_messages:
            messages.append({"role": msg["role"], "content": msg["content"]})

        if not messages:
            self.io.tool_warning("No chat history to save.")
            return

        path = self._save_path("chat", name)
        data = {"messages": messages}
        Path(path).write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        self.io.tool_output(f"Saved {len(messages)} messages to cache/chat/{name}.yml")

    def _save_context_to(self, path):
        """Save the current file context (abs_fnames + abs_read_only_fnames) to *path*."""
        root = self.root

        editable = sorted(
            os.path.relpath(p, root) for p in self.coder.abs_fnames
        )
        read_only = sorted(
            os.path.relpath(p, root) for p in self.coder.abs_read_only_fnames
        )

        if not editable and not read_only:
            self.io.tool_warning("No files in context to save.")
            return

        data = {"editable": editable, "read_only": read_only}
        Path(path).write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        total = len(editable) + len(read_only)
        rel = os.path.relpath(path, root)
        self.io.tool_output(f"Saved {total} files to {rel}")

    def completions_save(self):
        """Return completions for /save command."""
        return ["chat", "ctx"]

    # ------------------------------------------------------------------
    # /load — restore chat history or context from cache
    # ------------------------------------------------------------------

    def cmd_load(self, args):
        """Restore chat history or file context from a named cache slot.

        Usage:
            /load chat [name]   — restore chat history
            /load ctx [name]    — restore added files
            /load ctx path/to/file.yml — restore from a specific file path

        If no name is given, restores from 'default'.
        Restoring chat replaces the current history.
        Restoring context replaces the current file sets.
        A name containing '/' or ending in .yml is treated as a direct path
        (falls back to cache/ if the direct path doesn't exist).
        """
        parsed = self._parse_save_args(args, cmd="load")
        if parsed is None:
            return

        kind, name = parsed

        if kind == "chat":
            path = self._save_path("chat", name)
        else:
            path = self._resolve_ctx_path(name, must_exist=True)

        if not os.path.isfile(path):
            self.io.tool_error(f"No saved {kind} found with name '{name}'.")
            return

        if kind == "chat":
            self._load_chat(path, name)
        else:
            self._load_context(path, name)

    def _load_chat(self, path, name):
        """Restore chat messages from a YAML file."""
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not data or "messages" not in data:
            self.io.tool_error(f"Invalid save file: cache/chat/{name}.yml")
            return

        messages = data["messages"]
        self.coder.done_messages = [
            dict(role=m["role"], content=m["content"]) for m in messages
        ]
        self.coder.cur_messages = []
        self.io.tool_output(
            f"Restored {len(messages)} messages from cache/chat/{name}.yml"
        )

    def _load_context(self, path, name):
        """Restore file context from a YAML file."""
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not data:
            self.io.tool_error(f"Invalid save file: {name}")
            return

        root = self.root
        editable = data.get("editable", [])
        read_only = data.get("read_only", [])

        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()

        missing = []
        for rel in editable:
            abs_path = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(abs_path):
                self.coder.abs_fnames.add(abs_path)
            else:
                missing.append(rel)
        for rel in read_only:
            abs_path = os.path.normpath(os.path.join(root, rel))
            if os.path.isfile(abs_path):
                self.coder.abs_read_only_fnames.add(abs_path)
            else:
                missing.append(rel)

        total = len(self.coder.abs_fnames) + len(self.coder.abs_read_only_fnames)
        rel_path = os.path.relpath(path, root)
        self.io.tool_output(f"Restored {total} files from {rel_path}")
        if missing:
            self.io.tool_warning(
                f"Skipped {len(missing)} missing files: {', '.join(missing)}"
            )

    def completions_load(self):
        """Return completions for /load command."""
        return ["chat", "ctx"]

    # ------------------------------------------------------------------
    # /analyze-style — add prose files to context for style analysis
    # ------------------------------------------------------------------

    def cmd_analyze_style(self, args):
        """Add prose files to the chat context for style analysis.

        With a location or file path, adds the PROSE.md file(s) as
        read-only context.  Without arguments, nothing is added.

        Usage:
            /analyze-style act 1 chapter 2 scene 3
            /analyze-style 1 2 3        (shorthand: act chapter scene)
            /analyze-style act 1 chapter 2
            /analyze-style 1 2          (shorthand: act chapter)
            /analyze-style act 1
            /analyze-style 1            (shorthand: act)
            /analyze-style path/to/file.md
        """
        args = args.strip()
        if not args:
            return

        paths = self._collect_style_source_files(args)
        if paths is None:
            return
        if not paths:
            self.io.tool_error("No prose files found at that location.")
            return

        added = self._add_read_only(paths)
        if added:
            rel = [os.path.relpath(p, self.root) for p in added]
            self.io.tool_output(
                f"Added {len(rel)} file(s) as read-only context: "
                + ", ".join(rel)
            )

    def _collect_style_source_files(self, args):
        """Collect prose files for style analysis from location args or file paths.

        Returns a list of absolute paths, or None on parse error.
        """
        # Try narrative location first
        parsed = self._parse_location_args(args)
        if parsed is not None:
            node = self._resolve_node(parsed)
            if node is None:
                return None

            leaves = self._collect_leaf_nodes(node) if not node.is_leaf else [node]
            prose_paths = []
            for leaf in leaves:
                prose_path = os.path.join(leaf.path, NarrativeMap.PROSE_FILE)
                if self._has_content(prose_path):
                    prose_paths.append(prose_path)
            return prose_paths

        # Fall back to treating args as file paths
        fnames = []
        for word in args.split():
            path = os.path.join(self.root, word) if not os.path.isabs(word) else word
            if os.path.isfile(path):
                fnames.append(os.path.abspath(path))
            else:
                self.io.tool_warning(f"File not found: {word}")
        return fnames

    def completions_analyze_style(self):
        """Return completions for /analyze-style command."""
        return [l.lower() for l in self.levels]

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _add_read_only(self, paths):
        """Add *paths* to the main coder's read-only context.

        Skips files that are already present (editable or read-only).
        Returns the list of paths that were actually added.
        """
        already = self.coder.abs_fnames | self.coder.abs_read_only_fnames
        added = []
        for path in paths:
            abs_path = os.path.abspath(path)
            if abs_path not in already:
                self.coder.abs_read_only_fnames.add(abs_path)
                added.append(abs_path)
        return added
