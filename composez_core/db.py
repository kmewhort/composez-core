"""
Reference database for novel projects.

Manages reference material (characters, locations, themes, etc.)
stored under a ``db/`` directory in the project root.  Entries are text files
that can be selectively added to the LLM context as read-only references.
"""

import os
from pathlib import Path


# Category whose entries are always loaded as read-only context.
CORE_CATEGORY = "core"

# Default db categories and their subdirectory names.
DEFAULT_CATEGORIES = [
    CORE_CATEGORY,
    "characters",
    "locations",
    "items",
    "themes",
    "style",
    "notes",
    "other",
]

# Seed content for db/core/style.md.
_DEFAULT_STYLE_CONTENT = """\
# Example Style Guide

## Voice & Tone
- Write in third-person limited point of view unless otherwise specified.
- Match the tone to the scene: tension in conflict, warmth in intimacy, brevity in action.
- Avoid authorial intrusion — let characters' actions and dialogue reveal information.

## Prose Principles
- Favour concrete, sensory details over abstract description.
- Vary sentence length: short sentences for impact, longer ones for flow.
- Use active voice by default; reserve passive voice for deliberate effect.
- Show, don't tell — convey emotion through behaviour, body language, and subtext.

## Dialogue
- Each character should have a distinct speech pattern.
- Use dialogue beats (action during speech) instead of excessive adverbs on said-tags.
- Keep exposition in dialogue natural — characters don't explain things they both already know.

## Pacing
- Scene breaks signal jumps in time, location, or point of view.
- End scenes on a hook: a question, a revelation, or a moment of tension.
- Balance action, dialogue, and interiority within each scene.

## Consistency
- Maintain consistent tense throughout (past tense by default).
- Track character details (eye colour, speech habits, injuries) for continuity.
- Respect the established rules of the story's world.
"""


class DbEntry:
    """A single db entry (e.g. a character sheet)."""

    def __init__(self, category, name, path, content=None):
        self.category = category   # e.g. "characters"
        self.name = name           # stem of the file, e.g. "sarah"
        self.path = path           # absolute path to the file
        self._content = content

    @property
    def content(self):
        if self._content is None:
            self._content = self._read()
        return self._content

    def _read(self):
        try:
            return Path(self.path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""

    def __repr__(self):
        return f"DbEntry({self.category}/{self.name})"


class Db:
    """Manages the project reference database for the novel.

    Expected layout::

        <root>/
            db/
                characters/
                    sarah.md
                    tom.md
                locations/
                    apartment.md
                timeline.md        (file directly in db/)
                style_guide.md
    """

    DB_DIR = "db"

    def __init__(self, root, io=None):
        self.root = str(root)
        self.io = io
        self._entries = None  # cached list of DbEntry

    @property
    def db_path(self):
        return os.path.join(self.root, self.DB_DIR)

    # ------------------------------------------------------------------
    # Entry discovery
    # ------------------------------------------------------------------

    def get_entries(self, force_refresh=False):
        """Return all db entries, scanning the db/ directory."""
        if self._entries is not None and not force_refresh:
            return list(self._entries)
        self._entries = self._scan()
        return list(self._entries)

    def get_entry(self, name, category=None):
        """Find a db entry by name, optionally filtered by category.

        The *name* is matched case-insensitively against the file stem.
        """
        name_lower = name.lower()
        for entry in self.get_entries():
            if category and entry.category.lower() != category.lower():
                continue
            if entry.name.lower() == name_lower:
                return entry
        return None

    def get_entries_by_category(self, category):
        """Return all entries in a given category."""
        cat_lower = category.lower()
        return [e for e in self.get_entries() if e.category.lower() == cat_lower]

    def get_categories(self):
        """Return a sorted list of categories that have entries."""
        cats = set()
        for entry in self.get_entries():
            cats.add(entry.category)
        return sorted(cats)

    # ------------------------------------------------------------------
    # Entry management
    # ------------------------------------------------------------------

    def create_entry(self, category, name, content=""):
        """Create a new db entry file. Returns the DbEntry."""
        cat_dir = os.path.join(self.db_path, category)
        os.makedirs(cat_dir, exist_ok=True)

        # Ensure .md extension
        if not name.endswith(".md") and not name.endswith(".txt"):
            name = name + ".md"

        path = os.path.join(cat_dir, name)
        Path(path).write_text(content, encoding="utf-8")
        self._entries = None  # invalidate cache

        stem = Path(name).stem
        return DbEntry(category, stem, path, content=content)

    def delete_entry(self, name, category=None):
        """Delete a db entry by name. Returns the deleted DbEntry or None."""
        entry = self.get_entry(name, category=category)
        if entry is None:
            return None

        try:
            os.remove(entry.path)
        except OSError:
            return None

        self._entries = None  # invalidate cache
        return entry

    def init_db(self, categories=None):
        """Create the db/ directory structure with default categories."""
        if categories is None:
            categories = DEFAULT_CATEGORIES

        os.makedirs(self.db_path, exist_ok=True)
        created = []
        for cat in categories:
            cat_dir = os.path.join(self.db_path, cat)
            os.makedirs(cat_dir, exist_ok=True)
            created.append(cat)

        self.init_core_defaults()
        return created

    def init_core_defaults(self):
        """Seed ``db/core/style.md`` if it doesn't already exist."""
        core_dir = os.path.join(self.db_path, CORE_CATEGORY)
        os.makedirs(core_dir, exist_ok=True)
        style_path = os.path.join(core_dir, "style.md")
        if not os.path.isfile(style_path):
            Path(style_path).write_text(_DEFAULT_STYLE_CONTENT, encoding="utf-8")
            self._entries = None  # invalidate cache

    # ------------------------------------------------------------------
    # Context integration
    # ------------------------------------------------------------------

    def get_context_files(self, names=None, categories=None):
        """Return a list of absolute file paths for context injection.

        If *names* or *categories* are provided, filter accordingly.
        Otherwise return all entries.
        """
        entries = self.get_entries()

        if names:
            names_lower = {n.lower() for n in names}
            entries = [e for e in entries if e.name.lower() in names_lower]

        if categories:
            cats_lower = {c.lower() for c in categories}
            entries = [e for e in entries if e.category.lower() in cats_lower]

        return [e.path for e in entries]

    def get_all_context_paths(self):
        """Return all db file paths (for adding as read-only context)."""
        return self.get_context_files()

    def get_core_context_paths(self):
        """Return absolute paths for all ``db/core/`` entries."""
        return self.get_context_files(categories=[CORE_CATEGORY])

    def format_summary(self):
        """Return a formatted summary of the db for display."""
        entries = self.get_entries()
        if not entries:
            return "No db entries found."

        lines = ["db entries:"]
        by_cat = {}
        for entry in entries:
            by_cat.setdefault(entry.category, []).append(entry)

        for cat in sorted(by_cat.keys()):
            lines.append(f"  {cat}/")
            for entry in sorted(by_cat[cat], key=lambda e: e.name):
                lines.append(f"    {entry.name}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _scan(self):
        """Scan db/ directory and build the entries list."""
        entries = []
        db_path = self.db_path

        if not os.path.isdir(db_path):
            return entries

        for item in sorted(os.listdir(db_path)):
            if item.startswith("."):
                continue
            item_path = os.path.join(db_path, item)

            if os.path.isfile(item_path):
                # Files directly in db/ go under "general" category
                stem = Path(item).stem
                entries.append(DbEntry("general", stem, item_path))

            elif os.path.isdir(item_path):
                # Subdirectories are categories
                category = item
                for fname in sorted(os.listdir(item_path)):
                    if fname.startswith("."):
                        continue
                    fpath = os.path.join(item_path, fname)
                    if os.path.isfile(fpath):
                        stem = Path(fname).stem
                        entries.append(DbEntry(category, stem, fpath))

        return entries
