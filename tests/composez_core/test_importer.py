"""Tests for the Novelcrafter importer module."""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from composez_core.config import NOVEL_DIR
from composez_core.importer import (
    MarkdownImporter,
    NovelcrafterImporter,
    _extract_frontmatter,
    _safe_filename,
    _slugify,
    _strip_yaml_frontmatter,
)
from composez_core.narrative_map import _build_level_re


def _novel_dir(root):
    """Return the novel/ subdirectory path for a project root."""
    return os.path.join(root, NOVEL_DIR)


# Path to the Novelcrafter fixture directory
FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), "fixtures", "novelcrafter"
)


def _find_dir_by_number(parent, level_name, number):
    """Find a child directory matching ``Level N - Title`` with the given *number*."""
    if not os.path.isdir(parent):
        return None
    level_re = _build_level_re([level_name])
    for name in os.listdir(parent):
        m = level_re.match(name)
        if m and int(m.group(2)) == number:
            full = os.path.join(parent, name)
            if os.path.isdir(full):
                return full
    return None


class TestStripYamlFrontmatter(unittest.TestCase):
    """Tests for _strip_yaml_frontmatter helper."""

    def test_strips_frontmatter(self):
        text = "---\ntitle: Hello\n---\nBody text here."
        result = _strip_yaml_frontmatter(text)
        self.assertEqual(result, "Body text here.")

    def test_no_frontmatter(self):
        text = "Just plain text."
        result = _strip_yaml_frontmatter(text)
        self.assertEqual(result, "Just plain text.")

    def test_unclosed_frontmatter(self):
        text = "---\ntitle: Hello\nNo closing delimiter."
        result = _strip_yaml_frontmatter(text)
        self.assertEqual(result, text)

    def test_empty_frontmatter(self):
        text = "---\n---\nBody."
        result = _strip_yaml_frontmatter(text)
        self.assertEqual(result, "Body.")

    def test_frontmatter_with_extra_content(self):
        text = "---\nkey: val\n---\n\nParagraph 1.\n\nParagraph 2."
        result = _strip_yaml_frontmatter(text)
        self.assertEqual(result, "Paragraph 1.\n\nParagraph 2.")


class TestExtractFrontmatter(unittest.TestCase):
    """Tests for _extract_frontmatter helper."""

    def test_basic_extraction(self):
        text = "---\nname: Alice\ntype: character\n---\nBody content."
        fm, body = _extract_frontmatter(text)
        self.assertEqual(fm["name"], "Alice")
        self.assertEqual(fm["type"], "character")
        self.assertEqual(body, "Body content.")

    def test_no_frontmatter(self):
        text = "No frontmatter here."
        fm, body = _extract_frontmatter(text)
        self.assertEqual(fm, {})
        self.assertEqual(body, "No frontmatter here.")

    def test_boolean_values(self):
        text = "---\nactive: true\nhidden: false\n---\nBody."
        fm, body = _extract_frontmatter(text)
        self.assertTrue(fm["active"])
        self.assertFalse(fm["hidden"])

    def test_null_value(self):
        text = "---\ncolor: null\n---\nBody."
        fm, body = _extract_frontmatter(text)
        self.assertIsNone(fm["color"])

    def test_empty_list(self):
        text = "---\ntags: []\n---\nBody."
        fm, body = _extract_frontmatter(text)
        self.assertEqual(fm["tags"], [])

    def test_list_with_items(self):
        text = "---\ntags: [a, b, c]\n---\nBody."
        fm, body = _extract_frontmatter(text)
        self.assertEqual(fm["tags"], ["a", "b", "c"])

    def test_unclosed_frontmatter(self):
        text = "---\nname: Alice\nNo closing."
        fm, body = _extract_frontmatter(text)
        self.assertEqual(fm, {})
        self.assertEqual(body, text)


class TestSlugify(unittest.TestCase):
    """Tests for _slugify helper."""

    def test_basic(self):
        self.assertEqual(_slugify("Hello World"), "hello_world")

    def test_special_chars(self):
        self.assertEqual(_slugify("It's a test!"), "its_a_test")

    def test_spaces_and_underscores(self):
        self.assertEqual(_slugify("hello   world__test"), "hello_world_test")

    def test_empty(self):
        self.assertEqual(_slugify(""), "untitled")

    def test_only_special_chars(self):
        self.assertEqual(_slugify("!!!"), "untitled")

    def test_leading_trailing_spaces(self):
        self.assertEqual(_slugify("  hello  "), "hello")


class TestSafeFilename(unittest.TestCase):
    """Tests for _safe_filename helper."""

    def test_preserves_case(self):
        self.assertEqual(_safe_filename("Hello World"), "Hello World")

    def test_strips_invalid_chars(self):
        self.assertEqual(_safe_filename('He said "hello"'), "He said hello")

    def test_preserves_apostrophes(self):
        # Apostrophes are valid in filenames
        self.assertEqual(_safe_filename("It's a test"), "It's a test")

    def test_strips_colons(self):
        self.assertEqual(_safe_filename("Chapter 1: Title"), "Chapter 1 Title")

    def test_empty(self):
        self.assertEqual(_safe_filename(""), "Untitled")

    def test_only_invalid_chars(self):
        self.assertEqual(_safe_filename(':"?'), "Untitled")

    def test_preserves_hyphens(self):
        self.assertEqual(_safe_filename("The Ink-Debt"), "The Ink-Debt")

    def test_collapses_whitespace(self):
        self.assertEqual(_safe_filename("hello   world"), "hello world")

    def test_strips_trailing_dots(self):
        self.assertEqual(_safe_filename("test..."), "test")


class TestNovelImport(unittest.TestCase):
    """Tests for importing the novel manuscript (novel.md) from the fixture."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="novel_import_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_import_creates_metadata_file(self):
        """Importing novel.md should create db/core/metadata.yml with title and author."""
        import yaml

        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()
        meta_path = os.path.join(self.dest, "db", "core", "metadata.yml")
        self.assertTrue(os.path.isfile(meta_path))
        data = yaml.safe_load(Path(meta_path).read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Test Novel")
        self.assertEqual(data["author"], "Kent Mewhort")

    def test_import_creates_acts(self):
        """Should create act directories (no SUMMARY.md — Novelcrafter lacks act summaries)."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["acts"], 0)
        # Find the Act 1 directory (may have a title suffix)
        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        self.assertIsNotNone(act1_dir, "Act 1 directory not found")
        # Novelcrafter import should NOT create SUMMARY.md for acts
        self.assertFalse(os.path.isfile(os.path.join(act1_dir, "SUMMARY.md")))

    def test_import_creates_chapters(self):
        """Should create chapter directories (no SUMMARY.md — Novelcrafter lacks chapter summaries)."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["chapters"], 0)
        # Find Chapter 1 under Act 1
        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        self.assertIsNotNone(act1_dir)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        self.assertIsNotNone(ch1_dir, "Chapter 1 directory not found")
        # Novelcrafter import should NOT create SUMMARY.md for chapters
        self.assertFalse(os.path.isfile(os.path.join(ch1_dir, "SUMMARY.md")))

    def test_import_creates_scenes(self):
        """Should create scene directories with PROSE.md."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["scenes"], 0)

    def test_scene_content_not_empty(self):
        """Scene PROSE.md files should have actual prose."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()
        # Find any scene content file
        found_content = False
        for root, dirs, files in os.walk(self.dest):
            if "PROSE.md" in files:
                content = Path(os.path.join(root, "PROSE.md")).read_text(
                    encoding="utf-8"
                )
                if content.strip():
                    found_content = True
                    # Should contain actual prose (multiple words)
                    self.assertGreater(len(content.split()), 10)
                    break
        self.assertTrue(found_content, "No non-empty PROSE.md found")

    def test_scene_summary_files(self):
        """Scene SUMMARY.md files should be created for scenes with summary text."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()
        found_summary = False
        for root, dirs, files in os.walk(self.dest):
            if "SUMMARY.md" in files and "Scene" in root:
                content = Path(os.path.join(root, "SUMMARY.md")).read_text(
                    encoding="utf-8"
                )
                if content.strip():
                    found_summary = True
                    break
        self.assertTrue(found_summary, "No non-empty scene SUMMARY.md found")

    def test_summary_counts_match_fixture(self):
        """The fixture should produce the expected counts."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        # The fixture has 1 act (## Act 1)
        self.assertEqual(summary["acts"], 1)
        # The fixture has 2 chapters (Chapter 1 and Chapter 2)
        self.assertEqual(summary["chapters"], 2)
        # The fixture has 6 scenes (3 per chapter)
        self.assertEqual(summary["scenes"], 6)


class TestNovelImportSmall(unittest.TestCase):
    """Test importing with a small, controlled novel.md."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="nc_src_")
        self.dest = tempfile.mkdtemp(prefix="nc_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def _write_novel_md(self, content):
        Path(os.path.join(self.src, "novel.md")).write_text(
            content, encoding="utf-8"
        )

    def test_simple_structure(self):
        """Test a minimal novel with 1 act, 1 chapter, 1 scene."""
        self._write_novel_md(
            "# My Book\n\n"
            "## Act One\n\n"
            "### Chapter 1: Beginnings\n\n"
            "###### The Start\n\n"
            "Summary of the scene.\n\n"
            "---\n\n"
            "Once upon a time, in a land far away.\n\n"
            "The end.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        self.assertEqual(summary["acts"], 1)
        self.assertEqual(summary["chapters"], 1)
        self.assertEqual(summary["scenes"], 1)

        # Check metadata
        import yaml

        meta = yaml.safe_load(
            Path(os.path.join(self.dest, "db", "core", "metadata.yml")).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(meta["title"], "My Book")

        # Novelcrafter import should NOT create SUMMARY.md for acts/chapters
        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        self.assertIsNotNone(act1_dir)
        self.assertFalse(os.path.isfile(os.path.join(act1_dir, "SUMMARY.md")))

        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        self.assertIsNotNone(ch1_dir)
        self.assertFalse(os.path.isfile(os.path.join(ch1_dir, "SUMMARY.md")))

        # Check scene content
        scene_dir = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(scene_dir)
        content = Path(os.path.join(scene_dir, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("Once upon a time", content)

        # Check scene summary
        scene_summary = Path(
            os.path.join(scene_dir, "SUMMARY.md")
        ).read_text(encoding="utf-8").strip()
        self.assertIn("The Start", scene_summary)
        self.assertIn("Summary of the scene", scene_summary)

    def test_multiple_scenes(self):
        """Test multiple scenes in one chapter."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act I\n\n"
            "### Chapter 1: First\n\n"
            "###### Scene A\n---\nContent A.\n\n"
            "###### Scene B\n---\nContent B.\n\n"
            "###### Scene C\n---\nContent C.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 3)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        for i, label in enumerate(["A", "B", "C"], 1):
            scene_dir = _find_dir_by_number(ch1_dir, "Scene", i)
            self.assertIsNotNone(scene_dir, f"scene {i} missing")
            content = Path(os.path.join(scene_dir, "PROSE.md")).read_text(
                encoding="utf-8"
            ).strip()
            self.assertEqual(content, f"Content {label}.")

    def test_multiple_acts(self):
        """Test multiple acts."""
        self._write_novel_md(
            "# Book\n\n"
            "## First Act\n\n"
            "### Chapter 1: Ch1\n\n"
            "###### S1\n---\nContent 1.\n\n"
            "## Second Act\n\n"
            "### Chapter 1: Ch1b\n\n"
            "###### S2\n---\nContent 2.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["acts"], 2)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        self.assertIsNotNone(act1_dir)
        # Act title is in the directory name, not in SUMMARY.md
        self.assertIn("First Act", os.path.basename(act1_dir))
        self.assertFalse(os.path.isfile(os.path.join(act1_dir, "SUMMARY.md")))
        act2_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 2)
        self.assertIsNotNone(act2_dir)
        self.assertIn("Second Act", os.path.basename(act2_dir))
        self.assertFalse(os.path.isfile(os.path.join(act2_dir, "SUMMARY.md")))

    def test_scene_heading_levels(self):
        """Headings at levels 4, 5, and 6 should all be treated as scenes."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Test\n\n"
            "#### Scene Four\n---\nContent 4.\n\n"
            "##### Scene Five\n---\nContent 5.\n\n"
            "###### Scene Six\n---\nContent 6.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 3)

    def test_implicit_scene(self):
        """Prose directly in a chapter (no scene heading) creates an implicit scene.

        Without a ``---`` delimiter, content is treated as prose (no summary).
        """
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Prologue\n\n"
            "This is prose directly in the chapter.\n\n"
            "More prose here.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 1)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        scene_dir = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(scene_dir)
        content = Path(os.path.join(scene_dir, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("This is prose directly", content)

    def test_implicit_scene_with_delimiter(self):
        """Implicit scene with --- correctly splits summary from prose."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Prologue\n\n"
            "Scene summary here.\n\n"
            "---\n\n"
            "This is prose directly in the chapter.\n\n"
            "More prose here.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 1)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        scene_dir = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(scene_dir)
        content = Path(os.path.join(scene_dir, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("This is prose directly", content)
        # Summary should have the text before ---
        summary_text = Path(os.path.join(scene_dir, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("Scene summary here", summary_text)

    def test_chapter_number_parsing(self):
        """Chapter headings like 'Chapter 5: Title' should use the number from the heading."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 5: The Jump\n\n"
            "###### S1\n---\nContent.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        importer.run()

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch5_dir = _find_dir_by_number(act1_dir, "Chapter", 5)
        self.assertIsNotNone(ch5_dir)

    def test_bold_scene_format(self):
        """Novelcrafter bold **Scene N:** format should be parsed as scenes."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act 1\n\n"
            "### Chapter 1: Opening\n\n"
            "**Scene 1: The Dawn** Morning arrives.\n"
            "- **The Action:** Something happens.\n\n"
            "---\n\n"
            "The sun rose over the hills.\n\n"
            "#####\n\n"
            "* * *\n\n"
            "**Scene 2: The Dusk** Evening falls.\n"
            "- **The Ending:** Night comes.\n\n"
            "---\n\n"
            "The moon appeared in the sky.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["acts"], 1)
        self.assertEqual(summary["chapters"], 1)
        self.assertEqual(summary["scenes"], 2)

        # Find act/chapter/scene dirs
        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        # Check scene 1 content
        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(s1)
        content1 = Path(os.path.join(s1, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("sun rose", content1)

        # Check scene 1 summary
        summary1 = Path(os.path.join(s1, "SUMMARY.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn("The Dawn", summary1)
        self.assertIn("The Action", summary1)

        # Check scene 2 content
        s2 = _find_dir_by_number(ch1_dir, "Scene", 2)
        self.assertIsNotNone(s2)
        content2 = Path(os.path.join(s2, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("moon appeared", content2)

    def test_bold_scene_with_h5_label(self):
        """Scenes with both bold summary and ##### heading for prose section."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act 1\n\n"
            "### Chapter 1: Test\n\n"
            "**Scene 1: Intro** A summary.\n\n"
            "---\n\n"
            "##### Scene 1: Intro\n\n"
            "The actual prose content here.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 1)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(s1)
        content = Path(os.path.join(s1, "PROSE.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertIn("actual prose content", content)

    def test_no_novel_md_warns(self):
        """If novel.md is missing, it should warn but not fail."""
        io = MagicMock()
        importer = NovelcrafterImporter(self.src, self.dest, io=io)
        summary = importer.run()
        io.tool_warning.assert_called()
        self.assertEqual(summary["acts"], 0)

    def test_no_title_heading(self):
        """A novel.md without a # title should still import structure."""
        self._write_novel_md(
            "## Act\n\n"
            "### Chapter 1: Ch\n\n"
            "###### S1\n---\nContent.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["acts"], 1)
        self.assertFalse(
            os.path.isfile(os.path.join(self.dest, "TITLE.md"))
        )


class TestSceneSeparatorImport(unittest.TestCase):
    """Tests for importing novel.md with ``* * *`` scene separators and ``---`` delimiters."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="nc_sep_src_")
        self.dest = tempfile.mkdtemp(prefix="nc_sep_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def _write_novel_md(self, content):
        Path(os.path.join(self.src, "novel.md")).write_text(
            content, encoding="utf-8"
        )

    def test_implicit_scene_summary_then_prose(self):
        """Content before --- is summary; content after --- is prose."""
        self._write_novel_md(
            "# My Book\n"
            "by Author\n\n"
            "## Act One\n\n"
            "### Chapter 1: Cold Water\n"
            "Scene summary:\n"
            "* Characters take a cold bath.\n"
            "* They sweep the temple.\n\n"
            "---\n\n"
            "The first splash was always the worst.\n"
            "More prose here.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        self.assertEqual(summary["scenes"], 1)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        scene_dir = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(scene_dir)

        # SUMMARY.md should have chapter title + summary content
        summary_text = Path(os.path.join(scene_dir, "SUMMARY.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn("Cold Water", summary_text)
        self.assertIn("cold bath", summary_text)
        self.assertIn("sweep the temple", summary_text)

        # PROSE.md should have only prose, not summary content
        prose = Path(os.path.join(scene_dir, "PROSE.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn("first splash", prose)
        self.assertNotIn("cold bath", prose)

    def test_star_separator_starts_new_scene(self):
        """``* * *`` between scenes should start a new scene."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Title\n"
            "First scene summary.\n\n"
            "---\n\n"
            "First scene prose.\n\n"
            "* * *\n\n"
            "Second scene summary.\n\n"
            "---\n\n"
            "Second scene prose.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        self.assertEqual(summary["scenes"], 2)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        # Scene 1
        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(s1)
        prose1 = Path(os.path.join(s1, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("First scene prose", prose1)
        summary1 = Path(os.path.join(s1, "SUMMARY.md")).read_text(encoding="utf-8")
        self.assertIn("First scene summary", summary1)

        # Scene 2
        s2 = _find_dir_by_number(ch1_dir, "Scene", 2)
        self.assertIsNotNone(s2)
        prose2 = Path(os.path.join(s2, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("Second scene prose", prose2)
        summary2 = Path(os.path.join(s2, "SUMMARY.md")).read_text(encoding="utf-8")
        self.assertIn("Second scene summary", summary2)

    def test_multiple_scenes_with_star_separator(self):
        """Three scenes separated by ``* * *`` in one chapter."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Ch\n"
            "Summary A.\n---\nProse A.\n\n"
            "* * *\n\n"
            "Summary B.\n---\nProse B.\n\n"
            "* * *\n\n"
            "Summary C.\n---\nProse C.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 3)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        for i, label in enumerate(["A", "B", "C"], 1):
            s = _find_dir_by_number(ch1_dir, "Scene", i)
            self.assertIsNotNone(s, f"Scene {i} should exist")
            prose = Path(os.path.join(s, "PROSE.md")).read_text(encoding="utf-8")
            self.assertIn(f"Prose {label}", prose)

    def test_star_separator_across_chapters(self):
        """``* * *`` scenes reset when a new chapter heading appears."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: First\n"
            "Ch1 summary.\n---\nCh1 prose.\n\n"
            "* * *\n\n"
            "Ch1 S2 summary.\n---\nCh1 S2 prose.\n\n"
            "### Chapter 2: Second\n"
            "Ch2 summary.\n---\nCh2 prose.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        self.assertEqual(summary["chapters"], 2)
        self.assertEqual(summary["scenes"], 3)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        ch2_dir = _find_dir_by_number(act1_dir, "Chapter", 2)

        # Chapter 1 should have 2 scenes
        self.assertIsNotNone(_find_dir_by_number(ch1_dir, "Scene", 1))
        self.assertIsNotNone(_find_dir_by_number(ch1_dir, "Scene", 2))

        # Chapter 2 should have 1 scene
        s1_ch2 = _find_dir_by_number(ch2_dir, "Scene", 1)
        self.assertIsNotNone(s1_ch2)
        prose = Path(os.path.join(s1_ch2, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("Ch2 prose", prose)

    def test_scene_without_summary(self):
        """A scene with no --- goes straight to prose (no summary)."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Title\n"
            "The first line of prose.\n"
            "More prose here.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        self.assertEqual(summary["scenes"], 1)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)
        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIsNotNone(s1)

        prose = Path(os.path.join(s1, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("first line of prose", prose)
        self.assertIn("More prose", prose)
        # No PROSE content should leak into SUMMARY
        self.assertFalse(
            os.path.isfile(os.path.join(s1, "SUMMARY.md"))
            and "first line" in Path(os.path.join(s1, "SUMMARY.md")).read_text(
                encoding="utf-8"
            )
        )

    def test_star_separator_no_summary(self):
        """``* * *`` scene without --- goes straight to prose."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Ch\n"
            "Summary A.\n---\nProse A.\n\n"
            "* * *\n\n"
            "Prose B without any summary.\n"
            "More of scene B.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 2)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        # Scene 1 has summary + prose
        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIn(
            "Summary A",
            Path(os.path.join(s1, "SUMMARY.md")).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Prose A",
            Path(os.path.join(s1, "PROSE.md")).read_text(encoding="utf-8"),
        )

        # Scene 2 has only prose (no ---)
        s2 = _find_dir_by_number(ch1_dir, "Scene", 2)
        self.assertIsNotNone(s2)
        prose2 = Path(os.path.join(s2, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("Prose B without any summary", prose2)
        self.assertIn("More of scene B", prose2)

    def test_mixed_summary_and_no_summary_scenes(self):
        """Scenes can mix: some with summary+---, some without."""
        self._write_novel_md(
            "# Book\n\n"
            "## Act\n\n"
            "### Chapter 1: Ch\n"
            "Summary for scene 1.\n---\nProse for scene 1.\n\n"
            "* * *\n\n"
            "Prose for scene 2 (no summary).\n\n"
            "* * *\n\n"
            "Summary for scene 3.\n---\nProse for scene 3.\n"
        )
        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["scenes"], 3)

        act1_dir = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        ch1_dir = _find_dir_by_number(act1_dir, "Chapter", 1)

        s1 = _find_dir_by_number(ch1_dir, "Scene", 1)
        self.assertIn(
            "Summary for scene 1",
            Path(os.path.join(s1, "SUMMARY.md")).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Prose for scene 1",
            Path(os.path.join(s1, "PROSE.md")).read_text(encoding="utf-8"),
        )

        s2 = _find_dir_by_number(ch1_dir, "Scene", 2)
        prose2 = Path(os.path.join(s2, "PROSE.md")).read_text(encoding="utf-8")
        self.assertIn("Prose for scene 2", prose2)

        s3 = _find_dir_by_number(ch1_dir, "Scene", 3)
        self.assertIn(
            "Summary for scene 3",
            Path(os.path.join(s3, "SUMMARY.md")).read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Prose for scene 3",
            Path(os.path.join(s3, "PROSE.md")).read_text(encoding="utf-8"),
        )


class TestCodexImport(unittest.TestCase):
    """Tests for importing codex entries from the fixture."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="codex_import_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_import_characters(self):
        """Should import character entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["characters"], 0)

        chars_dir = os.path.join(self.dest, "db", "characters")
        self.assertTrue(os.path.isdir(chars_dir))

    def test_character_content(self):
        """Character files should contain the body text (not frontmatter)."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        # Look for Elias Thorne.md (case-preserving filename)
        path = os.path.join(self.dest, "db", "characters", "Elias Thorne.md")
        self.assertTrue(os.path.isfile(path))
        content = Path(path).read_text(encoding="utf-8")
        # Should NOT contain frontmatter markers
        self.assertNotIn("---", content.split("\n")[0])
        # Should contain character description text
        self.assertIn("Elias Thorne", content)

    def test_character_notes_imported(self):
        """Characters with notes.md should have a separate notes file."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        notes_path = os.path.join(
            self.dest, "db", "characters", "Elias Thorne - notes.md"
        )
        self.assertTrue(os.path.isfile(notes_path))
        content = Path(notes_path).read_text(encoding="utf-8")
        self.assertGreater(len(content), 0)

    def test_import_locations(self):
        """Should import location entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["locations"], 0)

        locs_dir = os.path.join(self.dest, "db", "locations")
        self.assertTrue(os.path.isdir(locs_dir))

    def test_import_other(self):
        """Should import 'other' codex entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["other"], 0)

        other_dir = os.path.join(self.dest, "db", "other")
        self.assertTrue(os.path.isdir(other_dir))

    def test_import_lore(self):
        """Should auto-discover and import lore entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertEqual(summary["lore"], 1)

        lore_dir = os.path.join(self.dest, "db", "lore")
        self.assertTrue(os.path.isdir(lore_dir))

    def test_import_objects(self):
        """Should auto-discover and import object entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertEqual(summary["objects"], 1)

    def test_import_subplots(self):
        """Should auto-discover and import subplot entries from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertEqual(summary["subplots"], 1)

    def test_codex_counts_match_fixture(self):
        """Counts should match the fixture data."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        # The fixture has 1 character, 1 location, 1 lore, 1 object, 1 other, 1 subplot
        self.assertEqual(summary["characters"], 1)
        self.assertEqual(summary["locations"], 1)
        self.assertEqual(summary["lore"], 1)
        self.assertEqual(summary["objects"], 1)
        self.assertEqual(summary["other"], 1)
        self.assertEqual(summary["subplots"], 1)


class TestCodexImportSmall(unittest.TestCase):
    """Tests for codex import with controlled data."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="codex_src_")
        self.dest = tempfile.mkdtemp(prefix="codex_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_basic_codex_entry(self):
        """Import a simple codex entry with frontmatter."""
        char_dir = os.path.join(self.src, "characters", "alice-123")
        os.makedirs(char_dir)
        Path(os.path.join(char_dir, "entry.md")).write_text(
            "---\ntype: character\nname: Alice\n---\n"
            "Alice is a brave warrior.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["characters"], 1)

        dest_path = os.path.join(self.dest, "db", "characters", "Alice.md")
        self.assertTrue(os.path.isfile(dest_path))
        content = Path(dest_path).read_text(encoding="utf-8").strip()
        self.assertEqual(content, "Alice is a brave warrior.")

    def test_entry_without_name_in_frontmatter(self):
        """If name is missing from frontmatter, use directory slug."""
        char_dir = os.path.join(self.src, "characters", "bob-456")
        os.makedirs(char_dir)
        Path(os.path.join(char_dir, "entry.md")).write_text(
            "---\ntype: character\n---\nBob description.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["characters"], 1)

        # Should use "bob" from the directory name (slug before -ID)
        dest_path = os.path.join(self.dest, "db", "characters", "bob.md")
        self.assertTrue(os.path.isfile(dest_path))

    def test_entry_with_notes(self):
        """Import entry + notes.md."""
        char_dir = os.path.join(self.src, "characters", "carol-789")
        os.makedirs(char_dir)
        Path(os.path.join(char_dir, "entry.md")).write_text(
            "---\nname: Carol\n---\nCarol description.\n",
            encoding="utf-8",
        )
        Path(os.path.join(char_dir, "notes.md")).write_text(
            "Author notes about Carol.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()

        entry = Path(os.path.join(self.dest, "db", "characters", "Carol.md"))
        notes = Path(os.path.join(self.dest, "db", "characters", "Carol - notes.md"))
        self.assertTrue(entry.is_file())
        self.assertTrue(notes.is_file())
        self.assertIn("Author notes", notes.read_text(encoding="utf-8"))

    def test_directory_without_entry_md_skipped(self):
        """Directories without entry.md should be ignored."""
        char_dir = os.path.join(self.src, "characters", "ghost-999")
        os.makedirs(char_dir)
        # Only a metadata.json, no entry.md
        Path(os.path.join(char_dir, "metadata.json")).write_text(
            "{}", encoding="utf-8"
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        # 'characters' shouldn't even appear in summary since no valid entries
        self.assertNotIn("characters", summary)

    def test_auto_discover_custom_category(self):
        """Any directory with entry.md sub-dirs should be auto-discovered."""
        custom_dir = os.path.join(self.src, "magic_systems", "fireball-001")
        os.makedirs(custom_dir)
        Path(os.path.join(custom_dir, "entry.md")).write_text(
            "---\nname: Fireball\n---\nA ball of fire.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["magic_systems"], 1)

        dest_path = os.path.join(
            self.dest, "db", "magic_systems", "Fireball.md"
        )
        self.assertTrue(os.path.isfile(dest_path))

    def test_empty_body_entry(self):
        """Entry with only frontmatter and no body should create empty file."""
        char_dir = os.path.join(self.src, "characters", "empty-001")
        os.makedirs(char_dir)
        Path(os.path.join(char_dir, "entry.md")).write_text(
            "---\nname: Empty\n---\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["characters"], 1)
        dest_path = os.path.join(self.dest, "db", "characters", "Empty.md")
        self.assertTrue(os.path.isfile(dest_path))


class TestSnippetsImport(unittest.TestCase):
    """Tests for importing snippets."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="snippets_import_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_import_snippets_from_fixture(self):
        """Should import snippet files from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        self.assertGreater(summary["snippets"], 0)

        snippets_dir = os.path.join(self.dest, "db", "snippets")
        self.assertTrue(os.path.isdir(snippets_dir))

        snippet_files = [f for f in os.listdir(snippets_dir) if f.endswith(".md")]
        self.assertGreater(len(snippet_files), 0)

    def test_snippet_frontmatter_stripped(self):
        """Snippet files should have frontmatter stripped."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        snippets_dir = os.path.join(self.dest, "db", "snippets")
        for fname in os.listdir(snippets_dir):
            if fname.endswith(".md"):
                content = Path(os.path.join(snippets_dir, fname)).read_text(
                    encoding="utf-8"
                )
                # Should not start with frontmatter
                if content.strip():
                    self.assertFalse(
                        content.startswith("---"),
                        f"{fname} still has frontmatter",
                    )
                break

    def test_snippet_count_matches_fixture(self):
        """Should import all .md snippet and chat files from the fixture."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()
        # Count .md files in both snippets and chats dirs
        expected = 0
        for subdir in ("snippets", "chats"):
            d = os.path.join(FIXTURE_DIR, subdir)
            if os.path.isdir(d):
                expected += len([f for f in os.listdir(d) if f.endswith(".md")])
        self.assertEqual(summary["snippets"], expected)


class TestSnippetsImportSmall(unittest.TestCase):
    """Tests for snippets import with controlled data."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="snip_src_")
        self.dest = tempfile.mkdtemp(prefix="snip_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_basic_snippet(self):
        """Import a simple snippet file."""
        snippets_dir = os.path.join(self.src, "snippets")
        os.makedirs(snippets_dir)
        Path(os.path.join(snippets_dir, "my-note.md")).write_text(
            "---\ntitle: My Note\nfavourite: false\n---\n"
            "Some snippet content.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["snippets"], 1)

        dest_path = os.path.join(self.dest, "db", "snippets", "my-note.md")
        self.assertTrue(os.path.isfile(dest_path))
        content = Path(dest_path).read_text(encoding="utf-8").strip()
        self.assertEqual(content, "Some snippet content.")

    def test_non_md_files_skipped(self):
        """Non-.md files in snippets dir should be skipped."""
        snippets_dir = os.path.join(self.src, "snippets")
        os.makedirs(snippets_dir)
        Path(os.path.join(snippets_dir, "note.md")).write_text(
            "---\ntitle: X\n---\nContent.\n",
            encoding="utf-8",
        )
        Path(os.path.join(snippets_dir, "image.png")).write_bytes(b"\x89PNG")

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["snippets"], 1)


class TestZipImport(unittest.TestCase):
    """Tests for importing from a zip file."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="zip_import_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_import_from_zip_flat(self):
        """Import from a zip file with contents at the root level."""
        zip_path = os.path.join(self.dest, "export.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "novel.md",
                "# My Book\n\n## Act One\n\n### Chapter 1: Ch1\n\n"
                "###### Scene 1\n---\nHello world.\n",
            )
            zf.writestr(
                "characters/alice-123/entry.md",
                "---\nname: Alice\n---\nAlice is great.\n",
            )
            zf.writestr(
                "snippets/note.md",
                "---\ntitle: A Note\n---\nNote body.\n",
            )

        dest = os.path.join(self.dest, "project")
        os.makedirs(dest)
        importer = NovelcrafterImporter(zip_path, dest)
        summary = importer.run()

        self.assertEqual(summary["acts"], 1)
        self.assertEqual(summary["chapters"], 1)
        self.assertEqual(summary["scenes"], 1)
        self.assertEqual(summary["characters"], 1)
        self.assertEqual(summary["snippets"], 1)

    def test_import_from_zip_with_subdirectory(self):
        """Import from a zip where all content is in a subdirectory."""
        zip_path = os.path.join(self.dest, "export.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "my-novel/novel.md",
                "# Book\n\n## Act\n\n### Chapter 1: Ch\n\n"
                "###### S1\n---\nContent here.\n",
            )

        dest = os.path.join(self.dest, "project")
        os.makedirs(dest)
        importer = NovelcrafterImporter(zip_path, dest)
        summary = importer.run()
        self.assertEqual(summary["acts"], 1)

    def test_zip_cleanup(self):
        """Temp directory should be cleaned up after import."""
        zip_path = os.path.join(self.dest, "export.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("novel.md", "# Book\n\n## Act\n\n### Chapter 1: Ch\n\n"
                         "###### S1\n---\nContent.\n")

        dest = os.path.join(self.dest, "project")
        os.makedirs(dest)
        importer = NovelcrafterImporter(zip_path, dest)
        importer.run()

        # The tmpdir should be cleaned up
        self.assertIsNotNone(importer._tmpdir)
        self.assertFalse(os.path.exists(importer._tmpdir))

    def test_invalid_source_raises(self):
        """A non-existent path should raise FileNotFoundError."""
        importer = NovelcrafterImporter("/nonexistent/path", self.dest)
        with self.assertRaises(FileNotFoundError):
            importer.run()


class TestMarkdownImporter(unittest.TestCase):
    """Tests for the MarkdownImporter class."""

    def setUp(self):
        self.src_dir = tempfile.mkdtemp(prefix="md_src_")
        self.dest = tempfile.mkdtemp(prefix="md_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src_dir, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def _write(self, content, name="book.md"):
        p = os.path.join(self.src_dir, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def test_simple_structure(self):
        """H1=act, H2=chapter, H3=scene basic case."""
        md = self._write(
            "# Part One\n\n"
            "## The Beginning\n\n"
            "### Opening\n\n"
            "It was a dark and stormy night.\n"
        )
        summary = MarkdownImporter(md, self.dest).run()
        self.assertEqual(summary["acts"], 1)
        self.assertEqual(summary["chapters"], 1)
        self.assertEqual(summary["scenes"], 1)

        # Verify prose was written
        prose_files = []
        for root, dirs, files in os.walk(self.dest):
            if "PROSE.md" in files:
                prose_files.append(os.path.join(root, "PROSE.md"))
        self.assertEqual(len(prose_files), 1)
        content = Path(prose_files[0]).read_text(encoding="utf-8")
        self.assertIn("dark and stormy", content)

    def test_multiple_acts_chapters_scenes(self):
        """Multiple acts, chapters, and scenes."""
        md = self._write(
            "# Act I\n\n"
            "## Ch 1\n\n### S1\n\nProse 1.\n\n### S2\n\nProse 2.\n\n"
            "## Ch 2\n\n### S3\n\nProse 3.\n\n"
            "# Act II\n\n"
            "## Ch 3\n\n### S4\n\nProse 4.\n"
        )
        summary = MarkdownImporter(md, self.dest).run()
        self.assertEqual(summary["acts"], 2)
        self.assertEqual(summary["chapters"], 3)
        self.assertEqual(summary["scenes"], 4)

    def test_implicit_scene(self):
        """Text under a chapter without ### headings creates an implicit scene."""
        md = self._write(
            "# Act\n\n"
            "## Chapter\n\n"
            "Some prose without a scene heading.\n"
        )
        summary = MarkdownImporter(md, self.dest).run()
        self.assertEqual(summary["scenes"], 1)

    def test_h4_treated_as_prose(self):
        """H4+ headings inside a scene should be kept as prose content."""
        md = self._write(
            "# Act\n\n"
            "## Chapter\n\n"
            "### Scene\n\n"
            "Before.\n\n"
            "#### Subsection\n\n"
            "After.\n"
        )
        summary = MarkdownImporter(md, self.dest).run()
        self.assertEqual(summary["scenes"], 1)

        # Both the H4 and surrounding text should be in PROSE.md
        prose_files = []
        for root, dirs, files in os.walk(self.dest):
            if "PROSE.md" in files:
                prose_files.append(os.path.join(root, "PROSE.md"))
        content = Path(prose_files[0]).read_text(encoding="utf-8")
        self.assertIn("Before.", content)
        self.assertIn("#### Subsection", content)
        self.assertIn("After.", content)

    def test_act_no_summary_written(self):
        """Non-leaf levels (acts) should NOT get auto-generated SUMMARY.md."""
        md = self._write("# The Great Act\n\n## Ch\n\n### Sc\n\nText.\n")
        MarkdownImporter(md, self.dest).run()

        act1 = _find_dir_by_number(_novel_dir(self.dest), "Act", 1)
        self.assertIsNotNone(act1)
        # Title is in the directory name, not a SUMMARY.md
        self.assertIn("The Great Act", os.path.basename(act1))
        self.assertFalse(os.path.isfile(os.path.join(act1, "SUMMARY.md")))

    def test_no_db_created(self):
        """Markdown import should not create a db/ directory."""
        md = self._write("# Act\n\n## Ch\n\n### Sc\n\nText.\n")
        MarkdownImporter(md, self.dest).run()
        self.assertFalse(os.path.isdir(os.path.join(self.dest, "db")))

    def test_empty_file(self):
        """An empty file should produce zero counts."""
        md = self._write("")
        summary = MarkdownImporter(md, self.dest).run()
        self.assertEqual(summary, {"acts": 0, "chapters": 0, "scenes": 0})


class TestImportCommand(unittest.TestCase):
    """Tests for the /import command in NovelCommands."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="cmd_import_test_")
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.dest
        # Disable git operations by default — git-specific tests below
        self.coder.repo = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_import_novelcrafter_from_directory(self):
        """The /import novelcrafter command should work with a directory path."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        # Should have called tool_output with import summary
        self.io.tool_output.assert_called()
        output = self.io.tool_output.call_args[0][0]
        self.assertIn("Import complete", output)
        self.assertIn("acts", output)
        self.assertIn("chapters", output)
        self.assertIn("scenes", output)
        self.assertIn("characters", output)

    def test_import_empty_args(self):
        """Calling /import with no args should show usage."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("")

        self.io.tool_error.assert_called()
        self.assertIn("Usage", self.io.tool_error.call_args[0][0])

    def test_import_unknown_format(self):
        """Calling /import with unknown format should show error."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("foobar /some/path")

        self.io.tool_error.assert_called()
        self.assertIn("Unknown import format", self.io.tool_error.call_args[0][0])

    def test_import_novelcrafter_nonexistent_source(self):
        """Calling /import novelcrafter with nonexistent path should show error."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("novelcrafter /nonexistent/path")

        self.io.tool_error.assert_called()
        self.assertIn("not found", self.io.tool_error.call_args[0][0])

    def test_import_novelcrafter_relative_path(self):
        """Relative paths should be resolved relative to root."""
        from composez_core.novel_commands import NovelCommands

        # Create a small export inside root
        export_dir = os.path.join(self.dest, "my_export")
        os.makedirs(export_dir)
        Path(os.path.join(export_dir, "novel.md")).write_text(
            "# Book\n\n## Act\n\n### Chapter 1: Ch\n\n"
            "###### S1\n---\nContent.\n",
            encoding="utf-8",
        )

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("novelcrafter my_export")

        self.io.tool_output.assert_called()
        output = self.io.tool_output.call_args[0][0]
        self.assertIn("Import complete", output)

    def test_import_command_in_get_commands(self):
        """The import command should be listed in get_commands()."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        commands = cmds.get_commands()
        self.assertIn("import", commands)

    def test_import_novelcrafter_deletes_old_act_and_db(self):
        """Novelcrafter import should delete existing Act dirs and db/ before loading."""
        from composez_core.novel_commands import NovelCommands

        # Seed an old file under novel/ that won't exist in the import
        old_file = os.path.join(self.dest, "novel", "Act 99 - Stale", "stale.txt")
        os.makedirs(os.path.dirname(old_file))
        Path(old_file).write_text("old")

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        self.assertFalse(os.path.exists(old_file))

    def test_import_rejects_dirty_act_files(self):
        """Import should refuse if Act dirs have uncommitted changes."""
        from composez_core.novel_commands import NovelCommands

        repo = MagicMock()
        repo.get_dirty_files.return_value = ["novel/Act 1 - Title/SUMMARY.md"]
        self.coder.repo = repo

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        self.io.tool_error.assert_called()
        first_error = self.io.tool_error.call_args_list[0][0][0]
        self.assertIn("Uncommitted", first_error)

    def test_import_rejects_dirty_db_files(self):
        """Import should refuse if db/ has uncommitted changes."""
        from composez_core.novel_commands import NovelCommands

        repo = MagicMock()
        repo.get_dirty_files.return_value = ["db/characters/alice.md"]
        self.coder.repo = repo

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        self.io.tool_error.assert_called()
        first_error = self.io.tool_error.call_args_list[0][0][0]
        self.assertIn("Uncommitted", first_error)

    def test_import_allows_dirty_files_outside_act_db(self):
        """Dirty files outside act/ and db/ should not block import."""
        from composez_core.novel_commands import NovelCommands

        repo = MagicMock()
        repo.get_dirty_files.return_value = ["README.md", "src/main.py"]
        repo.is_dirty.return_value = True
        repo.get_head_commit_sha.return_value = "abc1234"
        self.coder.repo = repo

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        outputs = [c[0][0] for c in self.io.tool_output.call_args_list]
        self.assertTrue(any("Import complete" in o for o in outputs))

    def test_import_novelcrafter_commits_after_success(self):
        """Novelcrafter import should git-commit act/ and db/."""
        from composez_core.novel_commands import NovelCommands

        repo = MagicMock()
        repo.get_dirty_files.return_value = []
        repo.is_dirty.return_value = True
        repo.get_head_commit_sha.return_value = "abc1234"
        self.coder.repo = repo

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        repo.repo.git.add.assert_called_once_with("-A", "--", ".", "db")
        repo.repo.git.commit.assert_called_once()
        commit_args = repo.repo.git.commit.call_args[0]
        self.assertEqual(commit_args[0], "-m")
        self.assertIn("Import from Novelcrafter", commit_args[1])


class TestImportMarkdownCommand(unittest.TestCase):
    """Tests for /import markdown subcommand."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="cmd_md_import_test_")
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.dest
        self.coder.repo = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def _write_md(self, content, name="book.md"):
        p = os.path.join(self.dest, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def test_markdown_import_basic(self):
        """Import a simple markdown file with H1/H2/H3 structure."""
        from composez_core.novel_commands import NovelCommands

        self._write_md(
            "# Act One\n\n"
            "## The Beginning\n\n"
            "### Opening Scene\n\n"
            "It was a dark and stormy night.\n"
        )

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        self.io.tool_output.assert_called()
        output = self.io.tool_output.call_args[0][0]
        self.assertIn("Import complete", output)

        # Verify structure — an Act directory should exist
        self.assertIsNotNone(_find_dir_by_number(_novel_dir(self.dest), "Act", 1))

    def test_markdown_import_counts(self):
        """Verify act/chapter/scene counts from a structured markdown."""
        from composez_core.novel_commands import NovelCommands

        self._write_md(
            "# Part One\n\n"
            "## Chapter 1\n\n"
            "### Scene A\n\nProse A.\n\n"
            "### Scene B\n\nProse B.\n\n"
            "## Chapter 2\n\n"
            "### Scene C\n\nProse C.\n"
        )

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        output = self.io.tool_output.call_args[0][0]
        self.assertIn("1 acts", output)
        self.assertIn("2 chapters", output)
        self.assertIn("3 scenes", output)

    def test_markdown_import_no_args(self):
        """Calling /import markdown with no file should show error."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown")

        self.io.tool_error.assert_called()
        self.assertIn("Usage", self.io.tool_error.call_args[0][0])

    def test_markdown_import_nonexistent_file(self):
        """Calling /import markdown with nonexistent file should show error."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown /nonexistent/book.md")

        self.io.tool_error.assert_called()
        self.assertIn("not found", self.io.tool_error.call_args[0][0])

    def test_markdown_import_deletes_old_act(self):
        """Markdown import should delete existing Act dirs before loading."""
        from composez_core.novel_commands import NovelCommands

        old_file = os.path.join(self.dest, "novel", "Act 99 - Stale", "stale.txt")
        os.makedirs(os.path.dirname(old_file))
        Path(old_file).write_text("old")

        self._write_md("# Act\n\n## Ch\n\n### Sc\n\nText.\n")

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        self.assertFalse(os.path.exists(old_file))

    def test_markdown_import_preserves_db(self):
        """Markdown import should NOT delete db/."""
        from composez_core.novel_commands import NovelCommands

        db_file = os.path.join(self.dest, "db", "core", "style.md")
        os.makedirs(os.path.dirname(db_file))
        Path(db_file).write_text("style guide")

        self._write_md("# Act\n\n## Ch\n\n### Sc\n\nText.\n")

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        self.assertTrue(os.path.isfile(db_file))

    def test_markdown_import_commits_act_only(self):
        """Markdown import should only commit act/ (not db/)."""
        from composez_core.novel_commands import NovelCommands

        repo = MagicMock()
        repo.get_dirty_files.return_value = []
        repo.is_dirty.return_value = True
        repo.get_head_commit_sha.return_value = "def5678"
        self.coder.repo = repo

        self._write_md("# Act\n\n## Ch\n\n### Sc\n\nText.\n")

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        repo.repo.git.add.assert_called_once_with("-A", "--", ".")
        repo.repo.git.commit.assert_called_once()
        commit_args = repo.repo.git.commit.call_args[0]
        self.assertIn("Import from markdown", commit_args[1])


class TestImportIntegration(unittest.TestCase):
    """Full integration tests: import fixture and verify complete structure."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="integration_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_full_fixture_import(self):
        """Import the full fixture and verify all components."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary = importer.run()

        # Verify core keys are present
        for key in ("acts", "chapters", "scenes", "snippets"):
            self.assertIn(key, summary)

        # Verify codex categories from the fixture are present
        for key in ("characters", "locations", "lore", "objects",
                     "other", "subplots"):
            self.assertIn(key, summary)

        # Verify directory structure was created — Act directory should exist
        self.assertIsNotNone(_find_dir_by_number(_novel_dir(self.dest), "Act", 1))
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "db")))
        for cat in ("characters", "locations", "lore", "objects",
                     "other", "subplots"):
            self.assertTrue(
                os.path.isdir(os.path.join(self.dest, "db", cat)),
                f"db/{cat} should exist",
            )
        self.assertTrue(
            os.path.isdir(os.path.join(self.dest, "db", "snippets"))
        )

    def test_no_data_loss(self):
        """Every scene that has content in novel.md should have a PROSE.md."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        # Walk through and count all PROSE.md files
        content_count = 0
        for root, dirs, files in os.walk(self.dest):
            if "PROSE.md" in files:
                path = os.path.join(root, "PROSE.md")
                text = Path(path).read_text(encoding="utf-8").strip()
                if text:
                    content_count += 1

        # The fixture has 6 scenes, all with prose content
        self.assertEqual(content_count, 6)

    def test_idempotent_import(self):
        """Running import twice should overwrite cleanly."""
        importer1 = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary1 = importer1.run()

        importer2 = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        summary2 = importer2.run()

        # Counts should be the same
        self.assertEqual(summary1, summary2)


class TestCodexRenamesAndThumbnails(unittest.TestCase):
    """Tests for special codex renames, thumbnail import, and case-preserving filenames."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="codex_src_")
        self.dest = tempfile.mkdtemp(prefix="codex_dest_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_prose_style_guide_renamed_to_style(self):
        """'Prose Style Guide' should be renamed to style.md in db/core."""
        entry_dir = os.path.join(self.src, "other", "style-guide-001")
        os.makedirs(entry_dir)
        Path(os.path.join(entry_dir, "entry.md")).write_text(
            "---\nname: Prose Style Guide\n"
            "alwaysIncludeInContext: true\n---\n"
            "Write in active voice.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        summary = importer.run()
        self.assertEqual(summary["other"], 1)

        # Should be redirected to db/core/style.md
        style_path = os.path.join(self.dest, "db", "core", "style.md")
        self.assertTrue(os.path.isfile(style_path))

    def test_entry_stays_in_category(self):
        """Regular entries stay in their category directory."""
        entry_dir = os.path.join(self.src, "other", "note-001")
        os.makedirs(entry_dir)
        Path(os.path.join(entry_dir, "entry.md")).write_text(
            "---\nname: World Notes\n---\nSome notes.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        importer.run()

        regular_path = os.path.join(
            self.dest, "db", "other", "World Notes.md"
        )
        self.assertTrue(os.path.isfile(regular_path))

    def test_fixture_prose_style_guide_goes_to_core(self):
        """The fixture's prose style guide should go to db/core/style.md."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        style_path = os.path.join(self.dest, "db", "core", "style.md")
        self.assertTrue(
            os.path.isfile(style_path),
            "Fixture's prose style guide should land in db/core/style.md"
        )
        content = Path(style_path).read_text(encoding="utf-8")
        self.assertIn("Clinical Gothic", content)

    def test_notes_stay_in_category(self):
        """Notes for an entry should stay alongside the entry in its category."""
        entry_dir = os.path.join(self.src, "other", "style-001")
        os.makedirs(entry_dir)
        Path(os.path.join(entry_dir, "entry.md")).write_text(
            "---\nname: Style\nalwaysIncludeInContext: true\n---\n"
            "Body.\n",
            encoding="utf-8",
        )
        Path(os.path.join(entry_dir, "notes.md")).write_text(
            "Author notes.\n", encoding="utf-8"
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        importer.run()

        notes_path = os.path.join(self.dest, "db", "other", "Style - notes.md")
        self.assertTrue(os.path.isfile(notes_path))

    def test_thumbnail_imported(self):
        """Thumbnail images should be copied alongside the entry markdown."""
        entry_dir = os.path.join(self.src, "characters", "alice-001")
        os.makedirs(entry_dir)
        Path(os.path.join(entry_dir, "entry.md")).write_text(
            "---\nname: Alice\n---\nAlice description.\n",
            encoding="utf-8",
        )
        # Create a fake thumbnail
        Path(os.path.join(entry_dir, "thumbnail.jpg")).write_bytes(
            b"\xff\xd8\xff\xe0"  # JPEG magic bytes
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        importer.run()

        thumb_dest = os.path.join(self.dest, "db", "characters", "Alice.jpg")
        self.assertTrue(os.path.isfile(thumb_dest))

    def test_fixture_thumbnails_imported(self):
        """Thumbnails from the fixture should be imported."""
        importer = NovelcrafterImporter(FIXTURE_DIR, self.dest)
        importer.run()

        # The fixture has thumbnails for characters, locations, objects
        for cat, name in [
            ("characters", "Elias Thorne"),
            ("locations", "The Rookery Archive"),
            ("objects", "Raven's Ledger"),
        ]:
            thumb = os.path.join(self.dest, "db", cat, f"{name}.jpg")
            self.assertTrue(
                os.path.isfile(thumb),
                f"Thumbnail should exist at db/{cat}/{name}.jpg"
            )

    def test_case_preserving_filenames(self):
        """Entry filenames should preserve the original case and spaces."""
        entry_dir = os.path.join(self.src, "characters", "bob-smith-001")
        os.makedirs(entry_dir)
        Path(os.path.join(entry_dir, "entry.md")).write_text(
            "---\nname: Bob Smith\n---\nBob.\n",
            encoding="utf-8",
        )

        importer = NovelcrafterImporter(self.src, self.dest)
        importer.run()

        dest_path = os.path.join(self.dest, "db", "characters", "Bob Smith.md")
        self.assertTrue(os.path.isfile(dest_path))


class TestImportSeedsCover(unittest.TestCase):
    """Imports should (re)create db/cover/front.jpg when missing."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="cover_import_test_")
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.dest
        self.coder.repo = None

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dest, ignore_errors=True)

    def _cover_path(self):
        return os.path.join(self.dest, "db", "cover", "front.jpg")

    def test_novelcrafter_import_creates_cover(self):
        """Novelcrafter import should create db/cover/front.jpg."""
        from composez_core.novel_commands import NovelCommands

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import(f"novelcrafter {FIXTURE_DIR}")

        self.assertTrue(os.path.isfile(self._cover_path()))

    def test_markdown_import_creates_cover(self):
        """Markdown import should create db/cover/front.jpg."""
        from composez_core.novel_commands import NovelCommands

        md_path = os.path.join(self.dest, "book.md")
        Path(md_path).write_text(
            "# Act One\n\n## Chapter 1\n\n### Scene 1\n\nProse.\n",
            encoding="utf-8",
        )

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        self.assertTrue(os.path.isfile(self._cover_path()))

    def test_markdown_import_does_not_overwrite_existing_cover(self):
        """If db/cover/front.jpg already exists, markdown import should not overwrite."""
        from composez_core.novel_commands import NovelCommands

        cover = self._cover_path()
        os.makedirs(os.path.dirname(cover))
        Path(cover).write_bytes(b"original")

        md_path = os.path.join(self.dest, "book.md")
        Path(md_path).write_text(
            "# Act One\n\n## Chapter 1\n\n### Scene 1\n\nProse.\n",
            encoding="utf-8",
        )

        cmds = NovelCommands(self.io, self.coder, root=self.dest)
        cmds.cmd_import("markdown book.md")

        # Markdown import preserves db/, so existing cover should remain
        self.assertEqual(Path(cover).read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
