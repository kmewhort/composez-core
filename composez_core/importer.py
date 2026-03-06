"""
Novelcrafter import module.

Imports a Novelcrafter **markdown** export (zip file or unzipped directory) into
the aider novel project structure:

    Act N - Title/Chapter N - Title/Scene N - Title/{PROSE.md, SUMMARY.md}
    (non-leaf directories contain no system-generated files)
    db/<category>/*.md    (characters, locations, lore, objects, subplots, other, …)
    db/snippets/*.md

Only the Novelcrafter *Markdown* export format is supported.  When exporting
from Novelcrafter, choose "Export as Markdown" (not Word/DOCX).

The Novelcrafter Markdown export layout:

    novel.md         — full manuscript:  # Title / ## Act / ### Chapter / scenes
    characters/      — codex entries: slug-ID/entry.md + metadata.json + notes.md
    locations/        — same structure as characters
    lore/            — same structure
    objects/         — same structure
    subplots/        — same structure
    other/           — same structure
    snippets/        — standalone markdown files with YAML frontmatter
    chats/           — chat history markdown files (imported as snippets)
    codex.html       — HTML codex export (ignored, redundant with individual entries)

Any subdirectory that contains codex entries (directories with entry.md files)
is auto-discovered and imported — the list above is not exhaustive.
"""

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from .config import NOVEL_DIR, get_levels
from .narrative_map import make_titled_dir


# Regex to detect heading levels
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)")

# Matches "Chapter N: Title" or "Chapter N"
_CHAPTER_RE = re.compile(r"chapter\s+(\d+)\s*:?\s*(.*)", re.IGNORECASE)

# Bold scene description at the start of a line: **Scene N: Title** ...
_BOLD_SCENE_RE = re.compile(r"^\*\*Scene\s+\d+.*?\*\*")

# Directories to skip when auto-discovering codex categories
_SKIP_DIRS = {"snippets", "chats"}

# Characters that are invalid in filenames on major OSes
_INVALID_FNAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Special rename: Novelcrafter name → (destination filename without .md, target category).
# When target category is not None the entry is redirected to db/<category>/.
_CODEX_RENAMES = {
    "Prose Style Guide": ("style", "core"),
}


def _strip_yaml_frontmatter(text):
    """Remove YAML frontmatter delimited by --- from markdown text."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].strip()
    return text


def _extract_frontmatter(text):
    """Extract YAML frontmatter as a dict (simple key: value parsing)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 3:].strip()

    fm = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Handle simple YAML values
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            elif value == "null":
                value = None
            elif value.startswith("[") and value.endswith("]"):
                # Simple list like []
                inner = value[1:-1].strip()
                value = [v.strip() for v in inner.split(",")] if inner else []
            fm[key] = value
    return fm, body


def _slugify(text):
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "_", text)
    text = text.strip("_")
    return text or "untitled"


def _safe_filename(text):
    """Sanitise *text* for use as a filename, preserving case and spaces.

    Only strips characters that are invalid on major operating systems.
    Falls back to ``"Untitled"`` if nothing remains.
    """
    text = text.strip()
    text = _INVALID_FNAME_RE.sub("", text)
    # Collapse runs of whitespace but preserve single spaces
    text = re.sub(r"\s+", " ", text).strip()
    # Trim trailing dots/spaces (Windows restriction)
    text = text.rstrip(". ")
    return text or "Untitled"


class NovelcrafterImporter:
    """Imports a Novelcrafter export into the aider novel structure."""

    def __init__(self, source, dest, io=None, levels=None):
        """
        Parameters
        ----------
        source : str
            Path to a zip file or directory containing the Novelcrafter export.
        dest : str
            Path to the destination project root.
        io : optional
            Aider InputOutput instance for user feedback.
        levels : list[str] or None
            Narrative level names.  Defaults to config-based levels.
        """
        self.source = str(source)
        self.dest = str(dest)
        self.novel_root = os.path.join(self.dest, NOVEL_DIR)
        self.io = io
        self.levels = levels or get_levels(dest)
        self._tmpdir = None  # for zip extraction

    def run(self):
        """Execute the full import. Returns a summary dict."""
        export_dir = self._resolve_source()

        summary = {
            "acts": 0,
            "chapters": 0,
            "scenes": 0,
            "snippets": 0,
        }

        # 1. Import the manuscript (novel.md)
        novel_md = os.path.join(export_dir, "novel.md")
        if os.path.isfile(novel_md):
            counts = self._import_novel(novel_md)
            summary.update(counts)
        else:
            self._warn("No novel.md found in export.")

        # 2. Auto-discover and import codex categories.
        #    Any subdirectory (other than snippets/chats) that contains at least
        #    one entry sub-directory with an entry.md is treated as a codex category.
        for name in sorted(os.listdir(export_dir)):
            cat_dir = os.path.join(export_dir, name)
            if not os.path.isdir(cat_dir):
                continue
            if name in _SKIP_DIRS:
                continue
            # Probe: does it look like a codex category?
            if self._is_codex_category(cat_dir):
                count = self._import_codex_category(cat_dir, name)
                summary[name] = count

        # 3. Import snippets
        snippets_dir = os.path.join(export_dir, "snippets")
        if os.path.isdir(snippets_dir):
            count = self._import_snippets(snippets_dir)
            summary["snippets"] = count

        # 4. Import chats as snippets (they share the same format)
        chats_dir = os.path.join(export_dir, "chats")
        if os.path.isdir(chats_dir):
            count = self._import_snippets(chats_dir)
            summary["snippets"] += count

        # Clean up temp dir if we extracted a zip
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

        return summary

    @staticmethod
    def _is_codex_category(cat_dir):
        """Return True if *cat_dir* looks like a Novelcrafter codex category."""
        for entry_name in os.listdir(cat_dir):
            entry_path = os.path.join(cat_dir, entry_name)
            if os.path.isdir(entry_path):
                if os.path.isfile(os.path.join(entry_path, "entry.md")):
                    return True
        return False

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------

    def _resolve_source(self):
        """Return the directory to import from, extracting a zip if needed."""
        if os.path.isdir(self.source):
            return self.source

        if zipfile.is_zipfile(self.source):
            self._tmpdir = tempfile.mkdtemp(prefix="novelcrafter_import_")
            with zipfile.ZipFile(self.source, "r") as zf:
                zf.extractall(self._tmpdir)

            # Check if the zip contains a single top-level directory
            entries = os.listdir(self._tmpdir)
            if len(entries) == 1 and os.path.isdir(
                os.path.join(self._tmpdir, entries[0])
            ):
                return os.path.join(self._tmpdir, entries[0])
            return self._tmpdir

        raise FileNotFoundError(
            f"Source is neither a directory nor a zip file: {self.source}"
        )

    # ------------------------------------------------------------------
    # Novel manuscript import
    # ------------------------------------------------------------------

    def _import_novel(self, novel_md_path):
        """Parse novel.md and create the acts/chapters/scenes directory structure.

        The Novelcrafter Markdown export uses:

        * ``#``  — book title (+ optional "by Author" on the next line)
        * ``##`` — act heading
        * ``###`` — chapter heading (``Chapter N: Title``)
        * ``#####`` or ``######`` — scene separator / scene heading
        * ``**Scene N: Title** summary …`` — bold scene description (starts a scene)
        * ``---`` — boundary between scene summary and prose content
        * ``* * *`` — decorative scene separator (ignored)
        """
        text = Path(novel_md_path).read_text(encoding="utf-8")
        lines = text.splitlines()

        counts = {"acts": 0, "chapters": 0, "scenes": 0}

        # State tracking
        act_num = 0
        chapter_num = 0
        scene_num = 0
        title = None
        author = None

        current_act_dir = None      # absolute path to current act directory
        current_ch_dir = None       # absolute path to current chapter directory
        current_chapter_title = None
        current_scene_title = None
        current_scene_summary_lines = []
        current_scene_content_lines = []
        in_summary = False  # True when between scene start and first ---
        just_saw_title = False  # True on the line right after the # title

        def flush_scene():
            nonlocal scene_num
            if scene_num == 0 or act_num == 0 or chapter_num == 0:
                return

            content = "\n".join(current_scene_content_lines).strip()
            summary_lines_text = "\n".join(current_scene_summary_lines).strip()

            # If we never hit a ``---`` (still in summary mode), the
            # "summary" lines are actually prose — the scene had no
            # separate summary section.
            if in_summary and summary_lines_text and not content:
                content = summary_lines_text
                summary_lines_text = ""

            # Discard completely empty scenes (e.g. from a decorative
            # ``* * *`` immediately before a bold ``**Scene N:**``
            # marker that starts its own scene).
            summary_text = ""
            if current_scene_title:
                summary_text = current_scene_title + "\n"
            if summary_lines_text:
                summary_text += summary_lines_text
            if not content and not summary_text.strip():
                scene_num -= 1
                return

            scene_level = self.levels[2] if len(self.levels) > 2 else "Scene"
            scene_dir_name = make_titled_dir(scene_level, scene_num, current_scene_title)
            scene_dir = os.path.join(current_ch_dir, scene_dir_name)
            os.makedirs(scene_dir, exist_ok=True)

            # Write PROSE.md
            if content:
                Path(os.path.join(scene_dir, "PROSE.md")).write_text(
                    content + "\n", encoding="utf-8"
                )

            # Write SUMMARY.md
            if summary_text.strip():
                Path(os.path.join(scene_dir, "SUMMARY.md")).write_text(
                    summary_text.strip() + "\n", encoding="utf-8"
                )

            counts["scenes"] += 1

        def start_scene(scene_title):
            """Begin a new scene, flushing the previous one."""
            nonlocal scene_num, current_scene_title
            nonlocal current_scene_summary_lines, current_scene_content_lines
            nonlocal in_summary
            flush_scene()
            scene_num += 1
            current_scene_title = scene_title
            current_scene_summary_lines = []
            current_scene_content_lines = []
            in_summary = True

        for line in lines:
            stripped = line.strip()
            heading = _HEADING_RE.match(line)

            # Check for "by Author" line right after the title heading
            if just_saw_title:
                just_saw_title = False
                by_match = re.match(r"^by\s+(.+)", stripped, re.IGNORECASE)
                if by_match:
                    author = by_match.group(1).strip()
                    continue

            if heading:
                level = len(heading.group(1))
                text_content = heading.group(2).strip()

                if level == 1:
                    # Book title — store but don't create structure
                    title = text_content
                    just_saw_title = True
                    continue

                if level == 2:
                    # Act heading
                    flush_scene()
                    act_num += 1
                    chapter_num = 0
                    scene_num = 0
                    current_scene_content_lines = []
                    current_scene_summary_lines = []
                    in_summary = False

                    # Create act directory with summary
                    act_level = self.levels[0]
                    act_dir_name = make_titled_dir(
                        act_level, act_num, text_content or None
                    )
                    current_act_dir = os.path.join(
                        self.novel_root, act_dir_name
                    )
                    os.makedirs(current_act_dir, exist_ok=True)
                    counts["acts"] += 1
                    continue

                if level == 3:
                    # Chapter heading
                    flush_scene()
                    chapter_num += 1
                    scene_num = 0
                    current_scene_content_lines = []
                    current_scene_summary_lines = []
                    in_summary = False

                    # Parse chapter title
                    ch_match = _CHAPTER_RE.match(text_content)
                    if ch_match:
                        chapter_num = int(ch_match.group(1))
                        current_chapter_title = ch_match.group(2).strip() or None
                    else:
                        current_chapter_title = text_content

                    # Create chapter directory with summary
                    ch_level = self.levels[1] if len(self.levels) > 1 else "Chapter"
                    ch_dir_name = make_titled_dir(
                        ch_level, chapter_num, current_chapter_title
                    )
                    current_ch_dir = os.path.join(
                        current_act_dir, ch_dir_name
                    )
                    os.makedirs(current_ch_dir, exist_ok=True)
                    counts["chapters"] += 1
                    continue

                if level >= 4:
                    # H4/H5/H6 — scene heading or scene separator.
                    if not text_content:
                        # Empty heading like ``#####`` — just a separator.
                        # Don't start a new scene; the bold **Scene …**
                        # line that follows will do that.
                        continue
                    # Heading with text.  If we already have a scene open
                    # and it has *no content yet* (we just passed ``---``),
                    # this heading is the redundant prose-section label
                    # that Novelcrafter sometimes emits; absorb it and
                    # switch to content mode.
                    if (
                        scene_num > 0
                        and not in_summary
                        and not any(
                            l.strip() for l in current_scene_content_lines
                        )
                    ):
                        in_summary = False
                        continue
                    # Otherwise treat as a new scene.
                    start_scene(text_content.strip("* "))
                    continue

            # ``* * *`` — scene separator.  When inside a chapter, start
            # a new scene; otherwise treat as decorative and skip.
            if stripped == "* * *":
                if chapter_num > 0:
                    start_scene(None)
                continue

            # Bold scene description: **Scene N: Title** ...
            if (
                chapter_num > 0
                and _BOLD_SCENE_RE.match(stripped)
            ):
                # Extract title from the bold text
                m = re.match(r"\*\*(.+?)\*\*", stripped)
                scene_title = m.group(1) if m else stripped
                start_scene(scene_title)
                # The rest of this line (after the bold) is summary text
                remainder = stripped[m.end():].strip() if m else ""
                if remainder:
                    current_scene_summary_lines.append(remainder)
                continue

            # Non-heading, non-separator lines
            if scene_num > 0:
                if in_summary:
                    if stripped == "---":
                        in_summary = False
                    else:
                        current_scene_summary_lines.append(line)
                else:
                    current_scene_content_lines.append(line)
            elif chapter_num > 0 and scene_num == 0:
                # Content before the first scene in a chapter — treat as
                # scene 1 implicitly.  Start in summary mode so that a
                # ``---`` line correctly separates the summary from prose.
                if stripped and stripped != "---":
                    scene_num = 1
                    current_scene_title = current_chapter_title
                    current_scene_summary_lines = [line]
                    current_scene_content_lines = []
                    in_summary = True

        # Flush the final scene
        flush_scene()

        # Write metadata to db/core/metadata.yml
        if title:
            import yaml

            core_dir = os.path.join(self.dest, "db", "core")
            os.makedirs(core_dir, exist_ok=True)
            data = {"title": title}
            if author:
                data["author"] = author
            Path(os.path.join(core_dir, "metadata.yml")).write_text(
                yaml.dump(data, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )

        return counts

    # ------------------------------------------------------------------
    # Codex import
    # ------------------------------------------------------------------

    def _import_codex_category(self, source_dir, category):
        """Import a codex category directory (characters, locations, other).

        All entries go into ``db/<category>/``.  Entries listed in
        ``_CODEX_RENAMES`` are written under a different filename (e.g.
        "Prose Style Guide" → ``style.md``).
        """
        dest_cat_dir = os.path.join(self.dest, "db", category)
        os.makedirs(dest_cat_dir, exist_ok=True)

        count = 0
        for entry_name in sorted(os.listdir(source_dir)):
            entry_path = os.path.join(source_dir, entry_name)
            if not os.path.isdir(entry_path):
                continue

            entry_md = os.path.join(entry_path, "entry.md")
            if not os.path.isfile(entry_md):
                continue

            # Read entry.md and extract the name from frontmatter
            raw = Path(entry_md).read_text(encoding="utf-8")
            fm, body = _extract_frontmatter(raw)

            name = fm.get("name", entry_name.rsplit("-", 1)[0])
            name = str(name)

            # Check for special renames first, then use the name as-is
            if name in _CODEX_RENAMES:
                basename, target_cat = _CODEX_RENAMES[name]
            else:
                basename = _safe_filename(name)
                target_cat = None

            if target_cat:
                entry_dest_dir = os.path.join(self.dest, "db", target_cat)
                os.makedirs(entry_dest_dir, exist_ok=True)
            else:
                entry_dest_dir = dest_cat_dir

            dest_path = os.path.join(entry_dest_dir, f"{basename}.md")
            Path(dest_path).write_text(body + "\n" if body else "", encoding="utf-8")

            # If there's a notes.md, import it alongside
            notes_md = os.path.join(entry_path, "notes.md")
            if os.path.isfile(notes_md):
                notes = Path(notes_md).read_text(encoding="utf-8")
                notes_dest = os.path.join(entry_dest_dir, f"{basename} - notes.md")
                Path(notes_dest).write_text(notes, encoding="utf-8")

            # If there's a thumbnail image, copy it alongside with matching name
            for thumb_name in os.listdir(entry_path):
                if thumb_name.startswith("thumbnail."):
                    ext = os.path.splitext(thumb_name)[1]
                    thumb_src = os.path.join(entry_path, thumb_name)
                    thumb_dest = os.path.join(entry_dest_dir, f"{basename}{ext}")
                    shutil.copy2(thumb_src, thumb_dest)

            count += 1

        return count

    # ------------------------------------------------------------------
    # Snippets import
    # ------------------------------------------------------------------

    def _import_snippets(self, source_dir):
        """Import snippets as notes under db/snippets/."""
        dest_dir = os.path.join(self.dest, "db", "snippets")
        os.makedirs(dest_dir, exist_ok=True)

        count = 0
        for fname in sorted(os.listdir(source_dir)):
            if not fname.endswith(".md"):
                continue
            source_path = os.path.join(source_dir, fname)
            raw = Path(source_path).read_text(encoding="utf-8")
            body = _strip_yaml_frontmatter(raw)

            # Use the original filename (preserving case/spaces)
            dest_path = os.path.join(dest_dir, fname)
            Path(dest_path).write_text(body + "\n" if body else "", encoding="utf-8")
            count += 1

        return count

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _info(self, msg):
        if self.io:
            self.io.tool_output(msg)

    def _warn(self, msg):
        if self.io:
            self.io.tool_warning(msg)


class MarkdownImporter:
    """Import a plain markdown file into the narrative structure.

    Heading mapping:

    * ``#``   — top-level (Act)
    * ``##``  — mid-level (Chapter)
    * ``###`` — leaf-level (Scene)

    Everything below ``###`` headings (or between ``##`` headings when there
    are no ``###`` headings in that chapter) becomes scene prose.
    """

    def __init__(self, source, dest, io=None, levels=None):
        self.source = str(source)
        self.dest = str(dest)
        self.novel_root = os.path.join(self.dest, NOVEL_DIR)
        self.io = io
        self.levels = levels or get_levels(dest)

    def run(self):
        """Execute the import.  Returns a summary dict with counts."""
        text = Path(self.source).read_text(encoding="utf-8")
        lines = text.splitlines()

        counts = {"acts": 0, "chapters": 0, "scenes": 0}

        act_num = 0
        chapter_num = 0
        scene_num = 0

        current_act_dir = None
        current_ch_dir = None
        current_chapter_title = None
        current_scene_title = None
        current_scene_lines = []

        def flush_scene():
            nonlocal scene_num
            if scene_num == 0 or chapter_num == 0 or act_num == 0:
                return
            scene_level = self.levels[2] if len(self.levels) > 2 else "Scene"
            scene_dir_name = make_titled_dir(scene_level, scene_num, current_scene_title)
            scene_dir = os.path.join(current_ch_dir, scene_dir_name)
            os.makedirs(scene_dir, exist_ok=True)

            content = "\n".join(current_scene_lines).strip()
            if content:
                Path(os.path.join(scene_dir, "PROSE.md")).write_text(
                    content + "\n", encoding="utf-8"
                )
            counts["scenes"] += 1

        for line in lines:
            heading = _HEADING_RE.match(line)
            if not heading:
                if chapter_num > 0:
                    # Implicit scene 1 if we haven't seen a ### yet
                    if scene_num == 0 and line.strip():
                        scene_num = 1
                        current_scene_title = current_chapter_title
                        current_scene_lines = []
                    if scene_num > 0:
                        current_scene_lines.append(line)
                continue

            level = len(heading.group(1))
            text_content = heading.group(2).strip()

            if level == 1:
                # Act
                flush_scene()
                act_num += 1
                chapter_num = 0
                scene_num = 0
                current_scene_lines = []

                act_level = self.levels[0]
                act_dir_name = make_titled_dir(act_level, act_num, text_content or None)
                current_act_dir = os.path.join(self.novel_root, act_dir_name)
                os.makedirs(current_act_dir, exist_ok=True)
                counts["acts"] += 1

            elif level == 2:
                # Chapter
                flush_scene()
                chapter_num += 1
                scene_num = 0
                current_scene_lines = []
                current_chapter_title = text_content

                ch_level = self.levels[1] if len(self.levels) > 1 else "Chapter"
                ch_dir_name = make_titled_dir(ch_level, chapter_num, text_content or None)
                current_ch_dir = os.path.join(
                    current_act_dir, ch_dir_name
                )
                os.makedirs(current_ch_dir, exist_ok=True)
                counts["chapters"] += 1

            elif level == 3:
                # Scene
                flush_scene()
                scene_num += 1
                current_scene_title = text_content
                current_scene_lines = []

            else:
                # H4+ treated as regular prose content
                if scene_num > 0:
                    current_scene_lines.append(line)

        flush_scene()
        return counts
