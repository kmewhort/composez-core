"""
Narrative map for novel projects.

Replaces RepoMap's tree-sitter-based code analysis with a structure-aware
representation of hierarchical narrative levels.  Reads SUMMARY.md files from
leaf nodes to build contextual awareness of the narrative hierarchy.

The directory structure uses *collapsed* directories where each level name
is embedded in the directory name::

    Act 1 - The Rising Action/
        Chapter 1 - Beginnings/
            Scene 1 - The Lake Encounter/
                SUMMARY.md
                PROSE.md

Levels are configurable (default: Act, Chapter, Scene).  The last level in
the list is the *leaf* level; leaf directories contain ``SUMMARY.md`` and
``PROSE.md``.  Non-leaf directories may contain user-added ``.md`` files
(notes, outlines, etc.) but no system-generated summaries.
"""

import os
import re
from pathlib import Path

from .config import DEFAULT_LEVELS


# ---------------------------------------------------------------------------
# Directory naming conventions
# ---------------------------------------------------------------------------

# Match numbered titled directories like "1 - The Rising Action" or bare "1".
# Group 1 = number, Group 2 = title (optional, may be None).
_NUM_TITLE_RE = re.compile(r"^(\d+)(?:\s*-\s*(.+))?$")


def _build_level_re(levels):
    """Build a regex that matches ``Level N - Title`` for the given level names.

    Group 1 = level name, Group 2 = number, Group 3 = title (optional).
    """
    escaped = "|".join(re.escape(l) for l in levels)
    return re.compile(
        rf"^({escaped})\s+(\d+)(?:\s*-\s*(.+))?$",
        re.IGNORECASE,
    )


def natural_sort_key(text):
    """Split *text* into ``(str, int, str, …)`` chunks for natural ordering.

    This ensures ``"Act 2 - Beta"`` sorts before ``"Act 11 - Gamma"`` by
    comparing numeric segments as integers instead of lexicographically.
    """
    parts = []
    for part in re.split(r"(\d+)", text.lower()):
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part))
    return parts


def make_titled_dir(level_name, number, title=None):
    """Build a directory name like ``Act 2 - The Lake Encounter``.

    Parameters
    ----------
    level_name : str
        The level prefix (e.g. ``"Act"``, ``"Chapter"``, ``"Scene"``).
    number : int
        The ordinal number.
    title : str or None
        Human-readable title.  Falls back to ``"Untitled"``.
    """
    return f"{level_name} {number} - {title or 'Untitled'}"


def parse_level_dir(name, levels):
    """Parse a directory name into ``(level_name, number, title)`` or ``None``.

    Matches names like ``Act 1 - The Beginning`` or ``Chapter 2``.
    Returns ``None`` if the name doesn't match any configured level.
    """
    level_re = _build_level_re(levels)
    m = level_re.match(name)
    if not m:
        return None
    # Normalize level name to title case
    level_name = m.group(1).title()
    number = int(m.group(2))
    title = m.group(3)
    return (level_name, number, title)


class NarrativeNode:
    """A single node in the narrative hierarchy."""

    def __init__(self, kind, number, path, title=None, summary=None,
                 word_count=0, is_leaf=False):
        self.kind = kind          # level name, e.g. "Act", "Chapter", "Scene"
        self.number = number      # int ordinal
        self.path = path          # absolute path to directory
        self.title = title        # from directory name (e.g. "Act 1 - Title" → "Title")
        self.summary = summary    # full SUMMARY.md content
        self.word_count = word_count
        self.is_leaf = is_leaf    # True if this is the deepest configured level
        self.children = []        # child NarrativeNodes

    def total_word_count(self):
        """Recursively compute word count for this node and its children."""
        total = self.word_count
        for child in self.children:
            total += child.total_word_count()
        return total

    def __repr__(self):
        return f"NarrativeNode({self.kind} {self.number}, words={self.total_word_count()})"


class NarrativeMap:
    """Builds and renders a structural map of a novel project.

    The hierarchy levels are configurable via the ``levels`` parameter
    (default: ``["Act", "Chapter", "Scene"]``).

    Expected layout (with default levels)::

        <root>/
            Act 1 - The Rising Action/
                Chapter 1 - Beginnings/
                    Scene 1 - The Lake Encounter/
                        SUMMARY.md
                        PROSE.md

    Allowed files per level:

    - **Non-leaf levels** (Act, Chapter): any ``.md`` file (user notes, etc.)
    - **Leaf level** (Scene): SUMMARY.md + PROSE.md
    """

    SUMMARY_FILE = "SUMMARY.md"
    PROSE_FILE = "PROSE.md"

    def __init__(self, root, levels=None, io=None, map_tokens=1024,
                 main_model=None, verbose=False):
        self.root = str(root)
        self.levels = levels or list(DEFAULT_LEVELS)
        self.io = io
        self.map_tokens = map_tokens
        self.main_model = main_model
        self.verbose = verbose
        self._tree = None  # cached list of NarrativeNode (top-level)

    @property
    def leaf_level(self):
        """The name of the deepest (leaf) level."""
        return self.levels[-1]

    def allowed_files(self, level_name):
        """Return the set of allowed files for a given level.

        Leaf levels allow SUMMARY.md and PROSE.md.
        Non-leaf levels allow any ``.md`` file (user notes, summaries, etc.).
        """
        if level_name.title() == self.leaf_level.title():
            return {self.SUMMARY_FILE, self.PROSE_FILE}
        return None  # any .md file is allowed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_repo_map(
        self,
        chat_files=None,
        other_files=None,
        mentioned_fnames=None,
        mentioned_idents=None,
        force_refresh=False,
    ):
        """Return a markdown-formatted narrative map, or None."""
        if force_refresh:
            self._tree = None

        tree = self.get_tree()
        if not tree:
            return None

        return self.render(tree, chat_files=chat_files)

    def get_tree(self):
        """Parse the directory structure and return the narrative tree."""
        if self._tree is not None:
            return self._tree
        self._tree = self._build_tree()
        return self._tree

    def refresh(self):
        """Force a rebuild of the cached tree."""
        self._tree = None
        return self.get_tree()

    # ------------------------------------------------------------------
    # Tree building
    # ------------------------------------------------------------------

    def _build_tree(self):
        """Walk the directory structure and build the narrative tree."""
        return self._scan_level(self.root, 0)

    def _scan_level(self, parent_dir, depth):
        """Scan *parent_dir* for children at the given depth in the level hierarchy.

        ``depth=0`` scans for the first level (e.g. Act), ``depth=1`` for
        the second (e.g. Chapter), etc.
        """
        if depth >= len(self.levels):
            return []

        level_name = self.levels[depth]
        is_leaf = (depth == len(self.levels) - 1)
        level_re = _build_level_re([level_name])

        nodes = []
        try:
            entries = sorted(os.listdir(parent_dir), key=natural_sort_key)
        except OSError:
            return []

        for entry_name in entries:
            m = level_re.match(entry_name)
            if not m:
                continue
            entry_path = os.path.join(parent_dir, entry_name)
            if not os.path.isdir(entry_path):
                continue

            number = int(m.group(2))
            title = m.group(3)
            summary = self._read_summary(entry_path)
            wc = self._count_words(entry_path)

            node = NarrativeNode(
                kind=level_name.title(),
                number=number,
                path=entry_path,
                title=title,
                summary=summary,
                word_count=wc,
                is_leaf=is_leaf,
            )

            if not is_leaf:
                node.children = self._scan_level(entry_path, depth + 1)

            nodes.append(node)

        return nodes

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, tree, chat_files=None):
        """Render the narrative tree as markdown text."""
        if not tree:
            return None

        chat_files = set(chat_files or [])
        lines = ["# Narrative Structure\n"]
        self._render_nodes(tree, lines, chat_files, depth=0)
        return "\n".join(lines)

    def _render_nodes(self, nodes, lines, chat_files, depth):
        """Recursively render nodes at the given depth."""
        for node in nodes:
            label = f"{node.kind} {node.number}"
            if node.title:
                label += f": {node.title}"
            label += f" ({node.total_word_count():,} words)"

            if node.is_leaf:
                # Leaf nodes are list items
                marker = ""
                prose_path = os.path.join(node.path, self.PROSE_FILE)
                if prose_path in chat_files:
                    marker = " **(editing)**"
                indent = "  " * depth
                lines.append(f"{indent}- {label}{marker}")
                if node.summary:
                    lines.append(f"{indent}  > {node.summary.splitlines()[0]}")
            else:
                # Non-leaf nodes use headings (## for depth 0, ### for depth 1, etc.)
                heading = "#" * (depth + 2)
                lines.append(f"{heading} {label}")
                if node.summary:
                    lines.append(f"> {node.summary.splitlines()[0]}")
                lines.append("")

            if node.children:
                self._render_nodes(node.children, lines, chat_files, depth + 1)
                if not node.is_leaf:
                    lines.append("")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_outline(self, include_summaries=True):
        """Return a concise outline of the narrative structure."""
        tree = self.get_tree()
        if not tree:
            return "No narrative structure found."

        lines = []
        self._outline_nodes(tree, lines, include_summaries, depth=0)
        return "\n".join(lines)

    def _outline_nodes(self, nodes, lines, include_summaries, depth):
        """Recursively build outline lines."""
        indent = "  " * depth
        for node in nodes:
            label = f"{indent}{node.kind} {node.number}"
            if node.title:
                label += f": {node.title}"
            if node.is_leaf:
                label += f" ({node.word_count:,} words)"
            else:
                label += f" ({node.total_word_count():,} words)"
            lines.append(label)

            if include_summaries and node.summary:
                summary_indent = "  " * (depth + 1)
                if node.is_leaf:
                    first_line = node.summary.strip().splitlines()[0]
                    lines.append(f"{summary_indent}{first_line}")
                else:
                    for sline in node.summary.strip().splitlines():
                        lines.append(f"{summary_indent}{sline}")

            if node.children:
                self._outline_nodes(
                    node.children, lines, include_summaries, depth + 1
                )

    def get_word_counts(self):
        """Return a nested dict of word counts.

        The structure adapts to the configured levels.  For the default
        ``["Act", "Chapter", "Scene"]``, the result looks like::

            {
                "total": 1234,
                "levels": ["Act", "Chapter", "Scene"],
                "acts": [
                    {"act": 1, "title": "...", "total": 800,
                     "chapters": [
                         {"chapter": 1, "title": "...", "total": 400,
                          "scenes": [
                              {"scene": 1, "title": "...", "words": 200},
                          ]}
                     ]}
                ]
            }

        For backward compatibility, the top-level key is always ``"acts"`` and
        children keys follow the level names.
        """
        tree = self.get_tree()
        result = {"total": 0, "levels": list(self.levels), "acts": []}

        def _children_key(depth):
            if depth + 1 < len(self.levels):
                return self.levels[depth + 1].lower() + "s"
            return None

        def _build(nodes, depth):
            items = []
            level_key = self.levels[depth].lower()
            child_key = _children_key(depth)
            for node in nodes:
                entry = {
                    level_key: node.number,
                    "title": node.title,
                }
                if node.is_leaf:
                    entry["words"] = node.word_count
                else:
                    entry["total"] = node.total_word_count()
                    if child_key:
                        entry[child_key] = _build(node.children, depth + 1)
                items.append(entry)
            return items

        if tree:
            result["acts"] = _build(tree, 0)
            result["total"] = sum(n.total_word_count() for n in tree)

        return result

    def find_node(self, *nums, **kwargs):
        """Find a specific narrative node by level numbers.

        Can be called as:
        - ``find_node(1, 2, 3)``  — positional act, chapter, scene
        - ``find_node(act=1, chapter=2, scene=3)``  — legacy keyword form

        Returns the node or ``None``.
        """
        # Support legacy keyword arguments
        if kwargs and not nums:
            nums_list = []
            for level in self.levels:
                val = kwargs.get(level.lower())
                if val is None:
                    break
                nums_list.append(val)
            nums = tuple(nums_list)

        tree = self.get_tree()
        current_level = tree
        node = None

        for num in nums:
            found = None
            for n in current_level:
                if n.number == num:
                    found = n
                    break
            if found is None:
                return None
            node = found
            current_level = found.children

        return node

    def _read_summary(self, dir_path):
        """Read SUMMARY.md from a directory, return the full text or None."""
        summary_path = os.path.join(dir_path, self.SUMMARY_FILE)
        if not os.path.isfile(summary_path):
            return None
        try:
            text = Path(summary_path).read_text(encoding="utf-8").strip()
            return text or None
        except OSError:
            return None

    def _count_words(self, dir_path):
        """Count words in PROSE.md (leaf nodes only)."""
        prose_path = os.path.join(dir_path, self.PROSE_FILE)
        if os.path.isfile(prose_path):
            try:
                text = Path(prose_path).read_text(encoding="utf-8")
                return len(text.split())
            except OSError:
                return 0
        return 0

    def check_narrative_file(self, rel_path):
        """Validate that a file path respects narrative structure constraints.

        Returns an error message string if the file is disallowed, or ``None``
        if ok.  Only validates files within the narrative tree (paths under
        ``novel/`` whose directories match configured level names).
        """
        from .config import NOVEL_DIR

        parts = Path(rel_path).parts
        if len(parts) < 2:
            # Reject bare filenames in the root folder – this almost always
            # means a path with spaces was incorrectly split on whitespace
            # (e.g. "Act" or "1" from "novel/Act 1 - Title/…").
            return (
                f"File '{rel_path}' cannot be created in the project root. "
                f"Narrative files must be inside the {NOVEL_DIR}/ directory tree. "
                f"Please use the full path including the directory structure."
            )

        # Strip the novel/ prefix if present
        if parts[0] == NOVEL_DIR:
            parts = parts[1:]
            if len(parts) < 2:
                return None

        # Check if the first component matches the top-level pattern
        top_level = self.levels[0]
        top_re = _build_level_re([top_level])
        if not top_re.match(parts[0]):
            return None  # Not in the narrative tree

        filename = parts[-1]

        # Determine which level the file's parent directory is at
        # by walking down the path components matching level patterns
        depth = 0
        for part in parts[:-1]:  # exclude filename
            parsed = parse_level_dir(part, self.levels)
            if parsed is not None:
                level_name = parsed[0]
                try:
                    depth = self.levels.index(level_name.title())
                except ValueError:
                    pass

        level_name = self.levels[depth]
        is_leaf = (depth == len(self.levels) - 1)

        if is_leaf:
            allowed = {self.SUMMARY_FILE, self.PROSE_FILE}
            if filename not in allowed:
                allowed_str = ", ".join(sorted(allowed))
                return (
                    f"File '{rel_path}' is not allowed at the {level_name} level. "
                    f"Only {allowed_str} files are permitted in "
                    f"{level_name} directories. "
                    f"Please only create/edit the allowed files."
                )
        else:
            # Non-leaf levels allow any .md file (notes, summaries, etc.)
            if not filename.endswith(".md"):
                return (
                    f"File '{rel_path}' is not allowed at the {level_name} level. "
                    f"Only .md files are permitted in {level_name} directories."
                )
        return None
