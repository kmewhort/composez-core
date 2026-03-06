import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from composez_core.config import NOVEL_DIR
from composez_core.db import Db
from composez_core.narrative_map import NarrativeMap, _NUM_TITLE_RE, make_titled_dir
from composez_core.novel_commands import NovelCommands


def _novel_dir(root):
    """Return the novel/ subdirectory path for a project root."""
    return os.path.join(root, NOVEL_DIR)


def _create_novel_structure(root, acts=2, chapters=2, scenes=2):
    """Helper to create a test novel directory structure.

    Follows the allowed file constraints:
    - Act: SUMMARY.md only
    - Chapter: SUMMARY.md only
    - Scene: SUMMARY.md + PROSE.md

    Creates the structure under ``root/novel/``.
    """
    novel_root = _novel_dir(root)
    for a in range(1, acts + 1):
        act_dir_name = make_titled_dir("Act", a, f"Act {a} Title")
        act_dir = os.path.join(novel_root, act_dir_name)
        os.makedirs(act_dir, exist_ok=True)
        Path(os.path.join(act_dir, "SUMMARY.md")).write_text(
            f"Act {a} Title\nSummary of act {a}.", encoding="utf-8"
        )

        for c in range(1, chapters + 1):
            ch_dir_name = make_titled_dir("Chapter", c, f"Chapter {c} Title")
            ch_dir = os.path.join(act_dir, ch_dir_name)
            os.makedirs(ch_dir, exist_ok=True)
            Path(os.path.join(ch_dir, "SUMMARY.md")).write_text(
                f"Chapter {c} Title\nSummary of chapter {c}.", encoding="utf-8"
            )

            for s in range(1, scenes + 1):
                sc_dir_name = make_titled_dir("Scene", s, f"Scene {s} Title")
                sc_dir = os.path.join(ch_dir, sc_dir_name)
                os.makedirs(sc_dir, exist_ok=True)
                words = " ".join([f"word{i}" for i in range(50 * s)])
                Path(os.path.join(sc_dir, "PROSE.md")).write_text(
                    words, encoding="utf-8"
                )
                Path(os.path.join(sc_dir, "SUMMARY.md")).write_text(
                    f"Scene {s} Title\nBrief summary of scene {s}.",
                    encoding="utf-8",
                )


def _create_db_structure(root):
    """Helper to create a test db structure."""
    db = Db(root)
    db.init_db()


def _find_dir_by_number(parent, level_name, number):
    """Find a child dir matching level-prefixed name with the given number."""
    from composez_core.narrative_map import _build_level_re
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


class TestNovelCommandsWordcount(unittest.TestCase):
    """Test the /wordcount command."""

    def test_wordcount(self):
        tmpdir = tempfile.mkdtemp()
        _create_novel_structure(tmpdir)
        io = MagicMock()
        cmds = NovelCommands(io, None, root=tmpdir)
        cmds.cmd_wordcount("")
        io.tool_output.assert_called_once()
        output = io.tool_output.call_args[0][0]
        self.assertIn("Total word count", output)
        self.assertIn("Act 1", output)

    def test_wordcount_empty(self):
        tmpdir = tempfile.mkdtemp()
        io = MagicMock()
        cmds = NovelCommands(io, None, root=tmpdir)
        cmds.cmd_wordcount("")
        output = io.tool_output.call_args[0][0]
        self.assertIn("0", output)


class TestNovelCommandsAddDb(unittest.TestCase):
    """Test /add db ... for adding db entries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)
        self.io = MagicMock()

    def test_add_db_entry(self):
        db = Db(self.tmpdir)
        db.create_entry("characters", "alice")
        coder = MagicMock()
        coder.root = self.tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(self.io, coder, root=self.tmpdir)
        cmds.cmd_add("db alice")
        self.assertEqual(len(coder.abs_fnames), 1)

    def test_add_db_category(self):
        db = Db(self.tmpdir)
        db.create_entry("characters", "alice")
        db.create_entry("characters", "bob")
        coder = MagicMock()
        coder.root = self.tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(self.io, coder, root=self.tmpdir)
        cmds.cmd_add("db characters")
        self.assertEqual(len(coder.abs_fnames), 2)

    def test_add_db_all(self):
        db = Db(self.tmpdir)
        db.create_entry("characters", "alice")
        db.create_entry("locations", "castle")
        coder = MagicMock()
        coder.root = self.tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(self.io, coder, root=self.tmpdir)
        cmds.cmd_add("db")
        # alice + castle + core/style.md (seeded by init_db)
        self.assertEqual(len(coder.abs_fnames), 3)

    def test_add_db_not_found(self):
        cmds = NovelCommands(self.io, None, root=self.tmpdir)
        cmds.cmd_add("db nonexistent")
        self.io.tool_error.assert_called()

    def test_add_non_db_delegates_to_parent(self):
        """Non-db /add args should delegate to the parent Commands.cmd_add"""
        parent = MagicMock()
        cmds = NovelCommands(self.io, None, root=self.tmpdir, parent_commands=parent)
        cmds.cmd_add("somefile.txt")
        parent.cmd_add.assert_called_once_with("somefile.txt")


class TestNovelCommandsAddNarrative(unittest.TestCase):
    """Test /add with narrative location and summary/prose type."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_add_chapter_summary(self):
        """/add summaries act 1 chapter 1 → adds scene SUMMARY.md files under chapter 1"""
        self.cmds.cmd_add("summaries act 1 chapter 1")
        # Chapter has 2 scenes, each with SUMMARY.md
        self.assertEqual(len(self.coder.abs_fnames), 2)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("SUMMARY.md"))
            self.assertIn("Scene", path)

    def test_add_chapter_summary_shorthand(self):
        """/add summaries 1 1 → shorthand for act 1, chapter 1, scene summaries"""
        self.cmds.cmd_add("summaries 1 1")
        # 2 scenes under chapter 1
        self.assertEqual(len(self.coder.abs_fnames), 2)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("SUMMARY.md"))

    def test_add_act_summary(self):
        """/add summaries act 1 → adds all scene SUMMARY.md files under act 1"""
        self.cmds.cmd_add("summaries act 1")
        # 2 chapters × 2 scenes = 4 scene SUMMARY.md files
        self.assertEqual(len(self.coder.abs_fnames), 4)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("SUMMARY.md"))
            self.assertIn("Scene", path)

    def test_add_act_summary_shorthand(self):
        """/add summaries 1 → act 1 scene summaries"""
        self.cmds.cmd_add("summaries 1")
        # 2 chapters × 2 scenes = 4
        self.assertEqual(len(self.coder.abs_fnames), 4)

    def test_add_chapter_prose(self):
        """/add prose act 1 chapter 1 → adds all scene PROSE.md in chapter"""
        self.cmds.cmd_add("prose act 1 chapter 1")
        # Chapter has 2 scenes, each with PROSE.md
        self.assertEqual(len(self.coder.abs_fnames), 2)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("PROSE.md"))

    def test_add_chapter_prose_shorthand(self):
        """/add prose 1 1 → act 1, chapter 1, prose"""
        self.cmds.cmd_add("prose 1 1")
        self.assertEqual(len(self.coder.abs_fnames), 2)

    def test_add_act_prose(self):
        """/add prose act 1 → adds all scene PROSE.md in act"""
        self.cmds.cmd_add("prose act 1")
        # Act has 2 chapters × 2 scenes = 4 PROSE.md files
        self.assertEqual(len(self.coder.abs_fnames), 4)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("PROSE.md"))

    def test_add_act_prose_shorthand(self):
        """/add prose 1 → act 1 prose"""
        self.cmds.cmd_add("prose 1")
        self.assertEqual(len(self.coder.abs_fnames), 4)

    def test_add_scene_summary(self):
        """/add summaries act 1 chapter 1 scene 1 → adds scene's SUMMARY.md"""
        self.cmds.cmd_add("summaries act 1 chapter 1 scene 1")
        self.assertEqual(len(self.coder.abs_fnames), 1)
        path = list(self.coder.abs_fnames)[0]
        self.assertIn("Scene", path)
        self.assertTrue(path.endswith("SUMMARY.md"))

    def test_add_scene_prose(self):
        """/add prose act 1 chapter 1 scene 1 → adds that scene's PROSE.md"""
        self.cmds.cmd_add("prose act 1 chapter 1 scene 1")
        self.assertEqual(len(self.coder.abs_fnames), 1)
        path = list(self.coder.abs_fnames)[0]
        self.assertTrue(path.endswith("PROSE.md"))

    def test_add_not_found(self):
        """/add summaries act 99 → should error"""
        self.cmds.cmd_add("summaries act 99")
        self.io.tool_error.assert_called()

    def test_add_prose_no_prose_files(self):
        """Should error when no PROSE.md files exist."""
        # Create an act with no scenes (no leaf-level dirs with PROSE.md)
        io = MagicMock()
        tmpdir = tempfile.mkdtemp()
        act_dir = os.path.join(tmpdir, make_titled_dir("Act", 1, "Empty"))
        ch_dir = os.path.join(act_dir, make_titled_dir("Chapter", 1, "Empty"))
        os.makedirs(ch_dir, exist_ok=True)
        Path(os.path.join(act_dir, "SUMMARY.md")).write_text("Empty\n", encoding="utf-8")
        Path(os.path.join(ch_dir, "SUMMARY.md")).write_text(
            "Empty\n", encoding="utf-8"
        )
        coder = MagicMock()
        coder.root = tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(io, coder, root=tmpdir)
        cmds.cmd_add("prose 1")
        io.tool_error.assert_called()

    def test_completions_add(self):
        completions = self.cmds.completions_add()
        self.assertIn("db", completions)
        self.assertIn("summaries", completions)
        self.assertIn("prose", completions)

    def test_add_summaries_all(self):
        """/add summaries → adds all scene-level SUMMARY.md files"""
        self.cmds.cmd_add("summaries")
        # 2 acts × 2 chapters × 2 scenes = 8 scene SUMMARY.md files
        self.assertEqual(len(self.coder.abs_fnames), 8)
        for path in self.coder.abs_fnames:
            self.assertTrue(path.endswith("SUMMARY.md"))
            self.assertIn("Scene", path)

    def test_add_summaries_empty_novel(self):
        """/add summaries on empty novel → warns"""
        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, "novel"), exist_ok=True)
        io = MagicMock()
        coder = MagicMock()
        coder.root = tmpdir
        coder.abs_fnames = set()
        cmds = NovelCommands(io, coder, root=tmpdir)
        cmds.cmd_add("summaries")
        io.tool_warning.assert_called()

    def test_add_summaries_case_insensitive(self):
        """/add Summaries → works case-insensitively"""
        self.cmds.cmd_add("Summaries")
        self.assertEqual(len(self.coder.abs_fnames), 8)


class TestNovelCommandsAddBareLocation(unittest.TestCase):
    """Test /add with bare narrative location (no summary/prose suffix)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_add_chapter_bare(self):
        """/add act 1 chapter 1 → adds chapter SUMMARY + scene files"""
        self.cmds.cmd_add("act 1 chapter 1")
        paths = self.coder.abs_fnames
        # Chapter SUMMARY + 2 scenes × (SUMMARY + PROSE) = 5
        self.assertEqual(len(paths), 5)
        summaries = [p for p in paths if p.endswith("SUMMARY.md")]
        proses = [p for p in paths if p.endswith("PROSE.md")]
        self.assertEqual(len(summaries), 3)  # chapter + 2 scenes
        self.assertEqual(len(proses), 2)  # 2 scenes

    def test_add_chapter_bare_shorthand(self):
        """/add 1 1 → shorthand for act 1, chapter 1 (all files)"""
        self.cmds.cmd_add("1 1")
        paths = self.coder.abs_fnames
        self.assertEqual(len(paths), 5)

    def test_add_act_bare(self):
        """/add act 1 → adds all files under act 1"""
        self.cmds.cmd_add("act 1")
        paths = self.coder.abs_fnames
        # Act SUMMARY + 2 chapters × (ch SUMMARY + 2 scenes × (SUMMARY + PROSE))
        # = 1 + 2 × (1 + 2 × 2) = 1 + 2 × 5 = 11
        self.assertEqual(len(paths), 11)

    def test_add_scene_bare(self):
        """/add act 1 chapter 1 scene 1 → adds scene SUMMARY + PROSE"""
        self.cmds.cmd_add("act 1 chapter 1 scene 1")
        paths = self.coder.abs_fnames
        self.assertEqual(len(paths), 2)
        summaries = [p for p in paths if p.endswith("SUMMARY.md")]
        proses = [p for p in paths if p.endswith("PROSE.md")]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(proses), 1)

    def test_add_bare_does_not_delegate_to_parent(self):
        """Bare location should NOT fall through to parent cmd_add."""
        parent = MagicMock()
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir, parent_commands=parent)
        cmds.cmd_add("act 1 chapter 1")
        parent.cmd_add.assert_not_called()

    def test_add_bare_nonexistent_location(self):
        """/add act 99 → should error"""
        self.cmds.cmd_add("act 99")
        self.io.tool_error.assert_called()

    def test_add_non_narrative_delegates_to_parent(self):
        """Non-narrative args should still delegate to parent."""
        parent = MagicMock()
        cmds = NovelCommands(self.io, self.coder, root=self.tmpdir, parent_commands=parent)
        cmds.cmd_add("somefile.txt")
        parent.cmd_add.assert_called_once_with("somefile.txt")


class TestNovelCommandsCreate(unittest.TestCase):
    """Test the /new command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_create_db_entry(self):
        """Creating a db entry like /new db characters alice"""
        self.cmds.cmd_new("db characters alice")
        self.io.tool_output.assert_called()
        self.assertEqual(len(self.coder.abs_fnames), 1)

    def test_create_db_missing_name(self):
        """Should error when name is missing for db entry"""
        self.cmds.cmd_new("db characters")
        self.io.tool_error.assert_called()

    def test_create_empty_args(self):
        """Should error with no arguments"""
        self.cmds.cmd_new("")
        self.io.tool_error.assert_called()

    def test_create_act(self):
        """Creating a new act auto-creates an initial leaf node"""
        self.cmds.cmd_new("act")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        self.assertIsNotNone(act_dir)
        self.assertTrue(os.path.isdir(act_dir))
        # Non-leaf nodes don't get auto-generated SUMMARY.md at their level
        self.assertFalse(os.path.isfile(os.path.join(act_dir, "SUMMARY.md")))
        # But an initial leaf node (Chapter 1 / Scene 1) is auto-created
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch_dir)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "PROSE.md")))
        # Leaf files are added to chat
        self.assertEqual(len(self.coder.abs_fnames), 2)

    def test_create_act_with_title(self):
        """Creating an act with a title auto-creates an initial leaf node"""
        self.cmds.cmd_new("act The Rising Action")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        self.assertIsNotNone(act_dir)
        self.assertTrue(os.path.isdir(act_dir))
        self.assertIn("The Rising Action", os.path.basename(act_dir))
        # Non-leaf nodes don't get auto-generated SUMMARY.md at their level
        self.assertFalse(os.path.isfile(os.path.join(act_dir, "SUMMARY.md")))
        # But an initial leaf is created inside
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch_dir)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))

    def test_create_act_increments(self):
        """Creating acts should auto-increment"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("act")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 3)
        self.assertIsNotNone(act_dir)
        self.assertTrue(os.path.isdir(act_dir))

    def test_create_chapter(self):
        """Creating a chapter auto-creates an initial scene"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=2, scenes=1)
        self.cmds.cmd_new("chapter")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))
        # Non-leaf nodes don't get SUMMARY.md at their level
        self.assertFalse(os.path.isfile(os.path.join(ch_dir, "SUMMARY.md")))
        # But an initial scene is auto-created inside
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "PROSE.md")))

    def test_create_chapter_with_title(self):
        """Creating a chapter with a title auto-creates an initial scene"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("chapter The Arrival")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))
        self.assertIn("The Arrival", os.path.basename(ch_dir))
        # Non-leaf nodes don't get SUMMARY.md at their level
        self.assertFalse(os.path.isfile(os.path.join(ch_dir, "SUMMARY.md")))
        # But an initial scene is auto-created inside
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))

    def test_create_chapter_in_specific_act(self):
        """/new act 1 chapter"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("act 1 chapter")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))

    def test_create_chapter_in_specific_act_with_title(self):
        """/new act 2 chapter The Storm"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("act 2 chapter The Storm")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        self.assertIn("The Storm", os.path.basename(ch_dir))
        self.assertTrue(os.path.isdir(ch_dir))

    def test_create_chapter_shorthand(self):
        """/new 1 chapter"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("1 chapter")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))

    def test_create_chapter_no_acts(self):
        """Should error when no acts exist"""
        self.cmds.cmd_new("chapter")
        self.io.tool_error.assert_called()

    def test_create_scene(self):
        """Creating a scene in the last chapter of the last act"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("scene")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "PROSE.md")))

    def test_create_scene_with_title(self):
        """Creating a scene with a title"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("scene The Lake Encounter")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))
        self.assertIn("The Lake Encounter", os.path.basename(sc_dir))
        summary = Path(os.path.join(sc_dir, "SUMMARY.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn("The Lake Encounter", summary)

    def test_create_scene_specific_location(self):
        """/new act 1 chapter 2 scene"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("act 1 chapter 2 scene")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))

    def test_create_scene_shorthand(self):
        """/new 2 1 scene"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("2 1 scene")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))

    def test_create_scene_shorthand_with_title(self):
        """/new 2 1 scene The Duel"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("2 1 scene The Duel")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertIn("The Duel", os.path.basename(sc_dir))
        self.assertTrue(os.path.isdir(sc_dir))

    def test_create_scene_shorthand_act_only(self):
        """/new 1 scene (uses last chapter in act 1)"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=3, scenes=1)
        self.cmds.cmd_new("1 scene")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 3)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))

    def test_create_scene_no_acts(self):
        """Should error when no acts exist"""
        self.cmds.cmd_new("scene")
        self.io.tool_error.assert_called()

    def test_create_scene_no_chapters(self):
        """Should error when no chapters exist in the act"""
        os.makedirs(os.path.join(
            self.tmpdir, make_titled_dir("Act", 1)
        ))
        self.cmds.cmd_new("scene")
        self.io.tool_error.assert_called()

    def test_shorthand_two_nums_chapter_invalid(self):
        """/new 2 1 chapter is invalid"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("2 1 chapter")
        self.io.tool_error.assert_called()

    def test_shorthand_nums_act_invalid(self):
        """/new 2 act is invalid"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("2 act")
        self.io.tool_error.assert_called()

    # ---- Specific target tests ----

    def test_specific_scene_keyword(self):
        """/new act 1 chapter 1 scene 3 My Title"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("act 1 chapter 1 scene 3 My Title")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))
        self.assertIn("My Title", os.path.basename(sc_dir))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "SUMMARY.md")))
        self.assertTrue(os.path.isfile(os.path.join(sc_dir, "PROSE.md")))
        summary = Path(os.path.join(sc_dir, "SUMMARY.md")).read_text(
            encoding="utf-8"
        )
        self.assertIn("My Title", summary)

    def test_specific_scene_shorthand(self):
        """/new 1 1 3 My Title"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("1 1 3 My Title")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertTrue(os.path.isdir(sc_dir))
        self.assertIn("My Title", os.path.basename(sc_dir))

    def test_specific_scene_shorthand_quoted_title(self):
        """/new 1 1 3 "My Title" (with quotes)"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new('1 1 3 "My Title"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertIn("My Title", os.path.basename(sc_dir))
        # Quotes should be stripped from the title
        self.assertNotIn('"', os.path.basename(sc_dir))

    def test_specific_chapter_no_auto_scene(self):
        """/new act 1 chapter 1 "My Title" — should NOT auto-create scenes"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=0, scenes=0)
        self.cmds.cmd_new('act 1 chapter 1 "My Title"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch_dir)
        self.assertIn("My Title", os.path.basename(ch_dir))
        # Should NOT have auto-created a scene inside
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNone(sc_dir)

    def test_specific_chapter_shorthand_no_auto_scene(self):
        """/new 1 1 "My Title" — creates chapter 1 in act 1, no auto-scene"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=0, scenes=0)
        self.cmds.cmd_new('1 1 "My Title"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch_dir)
        self.assertIn("My Title", os.path.basename(ch_dir))
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNone(sc_dir)

    def test_title_strips_redundant_level_prefix(self):
        """/new 1 1 3 "Scene 3 - It Begins" should not double the prefix"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new('1 1 3 "Scene 3 - It Begins"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        basename = os.path.basename(sc_dir)
        # Should be "Scene 3 - It Begins", not "Scene 3 - Scene 3 - It Begins"
        self.assertEqual(basename.count("Scene"), 1)
        self.assertIn("It Begins", basename)

    def test_title_strips_redundant_chapter_prefix(self):
        """/new act 1 chapter 2 "Chapter 2 - The Storm" strips prefix"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new('act 1 chapter 2 "Chapter 2 - The Storm"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        basename = os.path.basename(ch_dir)
        self.assertEqual(basename.count("Chapter"), 1)
        self.assertIn("The Storm", basename)

    def test_title_strips_prefix_without_dash(self):
        """/new 1 1 3 "Scene 3 It Begins" (no dash) also strips"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new('1 1 3 "Scene 3 It Begins"')
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        basename = os.path.basename(sc_dir)
        self.assertEqual(basename.count("Scene"), 1)
        self.assertIn("It Begins", basename)

    def test_title_no_strip_when_no_prefix(self):
        """Title without level prefix is left intact"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("1 1 3 It Begins")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertIn("It Begins", os.path.basename(sc_dir))

    def test_specific_scene_already_exists_warns(self):
        """/new act 1 chapter 1 scene 1 should warn when scene 1 exists"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("act 1 chapter 1 scene 1")
        self.io.tool_warning.assert_called()

    def test_specific_target_creates_missing_ancestors(self):
        """/new act 2 chapter 1 scene 1 Title — creates act 2 automatically"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("act 2 chapter 1 scene 1 New Scene")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        self.assertIsNotNone(act_dir)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch_dir)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc_dir)
        self.assertIn("New Scene", os.path.basename(sc_dir))

    def test_db_path_syntax(self):
        """/new db/characters/tom.md"""
        self.cmds.cmd_new("db/characters/tom.md")
        self.io.tool_output.assert_called()
        # File should be created
        db_path = os.path.join(self.tmpdir, "db", "characters", "tom.md")
        self.assertTrue(os.path.isfile(db_path))

    def test_db_path_syntax_no_extension(self):
        """/new db/characters/tom — .md is added automatically"""
        self.cmds.cmd_new("db/characters/tom")
        db_path = os.path.join(self.tmpdir, "db", "characters", "tom.md")
        self.assertTrue(os.path.isfile(db_path))

    def test_specific_scene_shorthand_no_title(self):
        """/new 1 1 3 — specific target without title"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("1 1 3")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)

    def test_specific_keyword_with_shorthand_nums(self):
        """/new 1 1 scene 3 The Duel — mixed shorthand+keyword with target num"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_new("1 1 scene 3 The Duel")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(sc_dir)
        self.assertIn("The Duel", os.path.basename(sc_dir))


class TestNovelCommandsCreateCompletions(unittest.TestCase):
    """Test the completions_new method."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)

    def test_completions_include_structural_types(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root=self.tmpdir)
        completions = cmds.completions_new()
        self.assertIn("act", completions)
        self.assertIn("chapter", completions)
        self.assertIn("scene", completions)

    def test_completions_include_db(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root=self.tmpdir)
        completions = cmds.completions_new()
        self.assertIn("db", completions)


class TestCreateRawCompletions(unittest.TestCase):
    """Test completions_raw_new for context-aware db category completions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)
        self.io = MagicMock()
        self.cmds = NovelCommands(self.io, None, root=self.tmpdir)

    def _complete(self, text):
        """Helper: simulate typing and collect completion texts."""
        from prompt_toolkit.document import Document

        doc = Document(text, cursor_position=len(text))
        return [c.text for c in self.cmds.completions_raw_new(doc, None)]

    def test_first_arg_offers_keywords(self):
        results = self._complete("/new d")
        self.assertIn("db", results)

    def test_after_db_space_offers_categories(self):
        results = self._complete("/new db ")
        self.assertIn("characters", results)
        self.assertIn("core", results)
        self.assertIn("locations", results)

    def test_after_db_partial_filters_categories(self):
        results = self._complete("/new db ch")
        self.assertIn("characters", results)
        self.assertNotIn("locations", results)

    def test_after_db_category_no_further_completions(self):
        """After the category is typed and space pressed, no more completions."""
        results = self._complete("/new db characters ")
        self.assertEqual(results, [])


class TestNodeLabel(unittest.TestCase):
    """Test _node_label returns descriptive labels for narrative nodes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_act_label(self):
        node = self.cmds._resolve_node([1])
        label = self.cmds._node_label(node)
        self.assertIn("Act 1", label)
        self.assertIn("Act 1 Title", label)

    def test_chapter_label(self):
        node = self.cmds._resolve_node([1, 2])
        label = self.cmds._node_label(node)
        self.assertIn("Act 1", label)
        self.assertIn("Chapter 2", label)
        self.assertIn("Chapter 2 Title", label)

    def test_scene_label(self):
        node = self.cmds._resolve_node([1, 2, 1])
        label = self.cmds._node_label(node)
        self.assertIn("Act 1", label)
        self.assertIn("Chapter 2", label)
        self.assertIn("Scene 1", label)
        self.assertIn("Scene 1 Title", label)

    def test_label_only_shows_title_on_leaf(self):
        """Ancestor levels should not include their titles."""
        node = self.cmds._resolve_node([1, 2, 1])
        label = self.cmds._node_label(node)
        # "Act 1 Title" and "Chapter 2 Title" should NOT appear
        self.assertNotIn("Act 1 Title", label)
        self.assertNotIn("Chapter 2 Title", label)
        # But "Scene 1 Title" should
        self.assertIn("Scene 1 Title", label)


class TestDescriptiveOutputOnShorthand(unittest.TestCase):
    """Commands using shorthand should show descriptive output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_add_narrative_summary_shows_label(self):
        """'/add summaries 1' should mention the act in output, not just '1'."""
        self.cmds.cmd_add("summaries 1")
        output_calls = [str(c) for c in self.io.tool_output.call_args_list]
        combined = " ".join(output_calls)
        self.assertIn("Act 1", combined)
        self.assertIn("Act 1 Title", combined)

    def test_add_narrative_prose_shows_label(self):
        """'/add prose 1' should mention the act in output."""
        self.cmds.cmd_add("prose 1")
        output_calls = [str(c) for c in self.io.tool_output.call_args_list]
        combined = " ".join(output_calls)
        self.assertIn("Act 1", combined)


class TestNovelCommandsGetCommands(unittest.TestCase):
    """Test get_commands method."""

    def test_get_commands_returns_dict(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        self.assertIsInstance(commands, dict)
        self.assertIn("new", commands)

    def test_get_commands_values_callable(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        for name, func in cmds.get_commands().items():
            self.assertTrue(callable(func), f"{name} should be callable")

    def test_removed_commands_not_present(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        for removed in ("cast", "scene", "scaffold", "outline", "map", "summarize-quick"):
            self.assertNotIn(removed, commands, f"/{removed} should not exist")


class TestNovelCommandsCreateEdgeCases(unittest.TestCase):
    """Test /new edge cases."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_create_chapter_invalid_act(self):
        """Should error when the specified act doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("act 99 chapter")
        self.io.tool_error.assert_called()

    def test_create_chapter_non_numeric_is_title(self):
        """Non-numeric arg treated as title"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("chapter abc")
        # "abc" is a title — should create chapter 2 in last act
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch_dir)
        self.assertIn("abc", os.path.basename(ch_dir))
        self.assertTrue(os.path.isdir(ch_dir))

    def test_create_scene_non_numeric_is_title(self):
        """Non-numeric arg treated as title for scene"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("scene abc")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc_dir)
        self.assertIn("abc", os.path.basename(sc_dir))
        self.assertTrue(os.path.isdir(sc_dir))

    def test_create_specific_chapter_keyword_all_numbers(self):
        """/new act 1 chapter 3 creates chapter 3 specifically (no auto-scene)"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("act 1 chapter 3")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))
        # Specific target: no auto-scaffolded scene inside
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNone(sc_dir)

    def test_create_specific_chapter_keyword_already_exists(self):
        """/new act 1 chapter 2 warns when chapter 2 already exists"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("act 1 chapter 2")
        self.io.tool_warning.assert_called()

    def test_create_specific_chapter_shorthand(self):
        """/new 1 3 creates chapter 3 in act 1 (specific target, no scene)"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_new("1 3")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch_dir)
        self.assertTrue(os.path.isdir(ch_dir))

    def test_create_specific_scene_shorthand_adds_to_chat(self):
        """/new 1 1 2 adds scene files to chat"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("1 1 2")
        self.assertEqual(len(self.coder.abs_fnames), 2)
        paths = sorted(self.coder.abs_fnames)
        self.assertTrue(any(p.endswith("PROSE.md") for p in paths))
        self.assertTrue(any(p.endswith("SUMMARY.md") for p in paths))

    def test_create_specific_act_shorthand(self):
        """/new 3 creates act 3 (specific target, no auto-scaffold)"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_new("3")
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 3)
        self.assertIsNotNone(act_dir)
        self.assertTrue(os.path.isdir(act_dir))
        # Specific target: no auto-scaffolded chapter/scene
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNone(ch_dir)

    def test_create_scene_nonexistent_chapter(self):
        """Should error when the specified chapter doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("act 1 chapter 99 scene")
        self.io.tool_error.assert_called()

    def test_create_db_entry_creates_file(self):
        """Db entry should exist on disk after creation"""
        _create_db_structure(self.tmpdir)
        self.cmds.cmd_new("db characters elena")
        entry_path = os.path.join(self.tmpdir, "db", "characters", "elena.md")
        self.assertTrue(os.path.isfile(entry_path))

    def test_create_db_new_category(self):
        """Creating an entry in a category that doesn't exist yet should work"""
        _create_db_structure(self.tmpdir)
        self.cmds.cmd_new("db factions rebels")
        entry_path = os.path.join(self.tmpdir, "db", "factions", "rebels.md")
        self.assertTrue(os.path.isfile(entry_path))

    def test_create_scene_adds_both_files_to_chat(self):
        """Scene creation should add both PROSE.md and SUMMARY.md to chat"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_new("scene")
        self.assertEqual(len(self.coder.abs_fnames), 2)
        paths = sorted(self.coder.abs_fnames)
        self.assertTrue(any(p.endswith("PROSE.md") for p in paths))
        self.assertTrue(any(p.endswith("SUMMARY.md") for p in paths))


class TestSetupNovelProject(unittest.TestCase):
    """Test the setup_novel_project() startup check."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()

    def test_skips_when_already_set_up(self):
        """Should return immediately when act/, db/, and instructions/ exist"""
        from composez_core import setup_novel_project

        # No container dir needed in collapsed format
        os.makedirs(os.path.join(self.tmpdir, "db"))
        os.makedirs(os.path.join(self.tmpdir, "instructions"))
        setup_novel_project(self.tmpdir, self.io)
        self.io.confirm_ask.assert_not_called()

    def test_prompts_when_missing(self):
        """Should prompt for levels then setup when db/ and instructions/ missing"""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = False
        setup_novel_project(self.tmpdir, self.io)
        # prompt_ask for levels, then confirm_ask for setup
        self.io.prompt_ask.assert_called_once()
        self.io.confirm_ask.assert_called_once()

    def test_creates_structure_on_confirm(self):
        """Should create db/ and instructions/ when user confirms"""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "db")))
        self.assertTrue(
            os.path.isdir(os.path.join(self.tmpdir, "db", "characters"))
        )
        self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "instructions")))

    def test_does_nothing_on_decline(self):
        """Should not create directories when user declines setup"""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = False
        setup_novel_project(self.tmpdir, self.io)

        self.assertFalse(os.path.isdir(os.path.join(self.tmpdir, "db")))

    def test_prompts_when_only_act_exists(self):
        """Should prompt when only act/ exists but db/ is missing"""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = False
        setup_novel_project(self.tmpdir, self.io)
        self.io.confirm_ask.assert_called_once()

    def test_prompts_when_only_db_exists(self):
        """Should prompt when only db/ exists but instructions/ is missing"""
        from composez_core import setup_novel_project

        os.makedirs(os.path.join(self.tmpdir, "db"))
        self.io.confirm_ask.return_value = False
        setup_novel_project(self.tmpdir, self.io)
        self.io.confirm_ask.assert_called_once()

    def test_uses_cwd_when_no_git_root(self):
        """Should fall back to cwd when git_root is None"""
        from composez_core import setup_novel_project

        original_cwd = os.getcwd()
        try:
            os.chdir(self.tmpdir)
            self.io.confirm_ask.return_value = True
            setup_novel_project(None, self.io)
            self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "db")))
            self.assertTrue(os.path.isdir(os.path.join(self.tmpdir, "instructions")))
        finally:
            os.chdir(original_cwd)


class TestSetupNovelProjectLevels(unittest.TestCase):
    """Test the interactive level configuration during setup_novel_project.

    Flow: (1) prompt_ask for levels (with defaults pre-filled),
          (2) confirm_ask for setup.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()

    def test_default_levels_accepted(self):
        """Hitting enter on the level prompt should use Act/Chapter/Scene."""
        from composez_core import setup_novel_project

        # Empty input (just hit Enter) should use defaults
        self.io.prompt_ask.return_value = ""
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        config_path = os.path.join(self.tmpdir, ".composez")
        self.assertTrue(os.path.isfile(config_path))
        import yaml
        data = yaml.safe_load(Path(config_path).read_text())
        self.assertEqual(data["levels"], ["Act", "Chapter", "Scene"])

    def test_custom_levels(self):
        """Typing custom levels should save them."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "Part, Chapter, Section"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        config_path = os.path.join(self.tmpdir, ".composez")
        self.assertTrue(os.path.isfile(config_path))
        import yaml
        data = yaml.safe_load(Path(config_path).read_text())
        self.assertEqual(data["levels"], ["Part", "Chapter", "Section"])

    def test_custom_levels_title_cased(self):
        """Custom level names should be title-cased."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "book, volume, page"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        import yaml
        data = yaml.safe_load(
            Path(os.path.join(self.tmpdir, ".composez")).read_text()
        )
        self.assertEqual(data["levels"], ["Book", "Volume", "Page"])

    def test_too_few_levels_falls_back_to_defaults(self):
        """If user provides fewer than 2 levels, defaults are used."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "Chapter"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        import yaml
        data = yaml.safe_load(
            Path(os.path.join(self.tmpdir, ".composez")).read_text()
        )
        self.assertEqual(data["levels"], ["Act", "Chapter", "Scene"])
        # Should warn the user
        self.io.tool_output.assert_any_call(
            "Need at least 2 levels. Using defaults."
        )

    def test_empty_input_falls_back_to_defaults(self):
        """If user provides empty input, defaults are used."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = ""
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        import yaml
        data = yaml.safe_load(
            Path(os.path.join(self.tmpdir, ".composez")).read_text()
        )
        self.assertEqual(data["levels"], ["Act", "Chapter", "Scene"])

    def test_skips_level_prompt_when_composez_exists(self):
        """If .composez already exists, level prompt should be skipped."""
        from composez_core import setup_novel_project
        from composez_core.config import save_config

        # Pre-create .composez with custom levels
        save_config(self.tmpdir, {"levels": ["Part", "Section"]})

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        self.io.confirm_ask.assert_called_once()
        self.io.prompt_ask.assert_not_called()

    def test_output_shows_configured_levels(self):
        """The summary output should show the configured levels."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "Part, Chapter"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        # Check that the output mentions the configured levels
        tool_output_calls = [
            str(call) for call in self.io.tool_output.call_args_list
        ]
        levels_output = [c for c in tool_output_calls if "Part > Chapter" in c]
        self.assertTrue(levels_output, "Output should mention configured levels")

    def test_output_uses_first_level_in_new_hint(self):
        """The /new hint should use the first configured level name."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "Part, Chapter"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        tool_output_calls = [
            str(call) for call in self.io.tool_output.call_args_list
        ]
        new_hint = [c for c in tool_output_calls if "/new part" in c]
        self.assertTrue(new_hint, "Output should show /new with first level name")


class TestSetupNovelProjectScaffold(unittest.TestCase):
    """Test that setup_novel_project creates the first narrative node."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()

    def test_creates_first_node_default_levels(self):
        """Should scaffold Act 1/Chapter 1/Scene 1 with default levels."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        scene_dir = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "Scene 1 - Untitled",
        )
        self.assertTrue(os.path.isdir(scene_dir))
        self.assertTrue(os.path.isfile(os.path.join(scene_dir, "PROSE.md")))
        self.assertTrue(os.path.isfile(os.path.join(scene_dir, "SUMMARY.md")))

    def test_leaf_prose_has_starter_text(self):
        """Leaf PROSE.md should have starter text."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        prose_path = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "Scene 1 - Untitled",
            "PROSE.md",
        )
        content = Path(prose_path).read_text()
        self.assertIn("dark and stormy night", content)

    def test_leaf_summary_mentions_summarize(self):
        """Leaf SUMMARY.md should explain /summarize and /write but not /summarize-quick."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        summary_path = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "Scene 1 - Untitled",
            "SUMMARY.md",
        )
        content = Path(summary_path).read_text()
        self.assertIn("/summarize", content)
        self.assertIn("/write", content)
        self.assertNotIn("/summarize-quick", content)

    def test_nonleaf_has_no_summary(self):
        """Non-leaf levels should NOT get auto-generated SUMMARY.md."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        act_summary = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "SUMMARY.md",
        )
        self.assertFalse(os.path.isfile(act_summary))

        ch_summary = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "SUMMARY.md",
        )
        self.assertFalse(os.path.isfile(ch_summary))

    def test_creates_first_node_custom_levels(self):
        """Should scaffold with custom level names."""
        from composez_core import setup_novel_project

        self.io.prompt_ask.return_value = "Part, Section"
        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        leaf_dir = os.path.join(
            self.tmpdir,
            "novel",
            "Part 1 - Untitled",
            "Section 1 - Untitled",
        )
        self.assertTrue(os.path.isdir(leaf_dir))
        self.assertTrue(os.path.isfile(os.path.join(leaf_dir, "PROSE.md")))
        self.assertTrue(os.path.isfile(os.path.join(leaf_dir, "SUMMARY.md")))

        # Non-leaf should NOT have auto-generated SUMMARY.md
        part_summary = os.path.join(
            self.tmpdir, "novel", "Part 1 - Untitled", "SUMMARY.md"
        )
        self.assertFalse(os.path.isfile(part_summary))

    def test_no_prose_in_nonleaf(self):
        """Non-leaf directories should NOT have PROSE.md."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        act_prose = os.path.join(
            self.tmpdir, "novel", "Act 1 - Untitled", "PROSE.md"
        )
        ch_prose = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "PROSE.md",
        )
        self.assertFalse(os.path.isfile(act_prose))
        self.assertFalse(os.path.isfile(ch_prose))

    def test_leaf_summary_has_full_location_syntax(self):
        """Leaf SUMMARY.md should use full hierarchy in command examples."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        summary_path = os.path.join(
            self.tmpdir,
            "novel",
            "Act 1 - Untitled",
            "Chapter 1 - Untitled",
            "Scene 1 - Untitled",
            "SUMMARY.md",
        )
        content = Path(summary_path).read_text()
        # Full location syntax
        self.assertIn("/summarize act 1 chapter 1 scene 1", content)
        self.assertIn("/write act 1 chapter 1 scene 1", content)
        # Short form
        self.assertIn("/summarize 1 1 1", content)
        self.assertIn("/write 1 1 1", content)

    def test_nonleaf_dirs_exist_but_empty(self):
        """Non-leaf directories should exist but have no auto-generated files."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        act_dir = os.path.join(self.tmpdir, "novel", "Act 1 - Untitled")
        self.assertTrue(os.path.isdir(act_dir))
        self.assertFalse(os.path.isfile(os.path.join(act_dir, "SUMMARY.md")))

        ch_dir = os.path.join(
            self.tmpdir, "novel", "Act 1 - Untitled", "Chapter 1 - Untitled"
        )
        self.assertTrue(os.path.isdir(ch_dir))
        self.assertFalse(os.path.isfile(os.path.join(ch_dir, "SUMMARY.md")))

    def test_leaf_summary_includes_encouragement(self):
        """Leaf SUMMARY.md should include the reassuring note."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        leaf_path = os.path.join(
            "Act 1 - Untitled", "Chapter 1 - Untitled",
            "Scene 1 - Untitled", "SUMMARY.md",
        )
        content = Path(os.path.join(
            self.tmpdir, "novel", leaf_path
        )).read_text()
        self.assertIn(
            "just tell the console what you want to do",
            content,
        )


class TestNovelCommandOverridePriority(unittest.TestCase):
    """Test that novel commands override base commands with the same name."""

    def test_do_run_falls_back_to_base(self):
        """do_run should fall back to base for commands not in novel"""
        from aider.commands import Commands

        io = MagicMock()
        commands = Commands(io, None)

        # Novel commands that don't have cmd_clear
        mock_novel = MagicMock(spec=[])
        commands._novel_commands = mock_novel

        # Should not crash — falls back to base cmd_clear
        commands.do_run("clear", "")

    def test_get_completions_prefers_novel(self):
        """get_completions should use novel completions when available"""
        from aider.commands import Commands

        io = MagicMock()
        commands = Commands(io, None)

        mock_novel = MagicMock()
        mock_novel.completions_new = MagicMock(
            return_value=["act", "chapter", "scene"]
        )
        commands._novel_commands = mock_novel

        result = commands.get_completions("/new")
        self.assertEqual(result, ["act", "chapter", "scene"])

    def test_get_completions_falls_back_to_base(self):
        """get_completions should use base completions for non-novel commands"""
        import aider.commands as commands_mod
        from aider.commands import Commands

        fake_litellm = MagicMock()
        fake_litellm.model_cost = {"gpt-4": {}, "gpt-3.5-turbo": {}}
        original = commands_mod.litellm
        commands_mod.litellm = fake_litellm
        try:
            io = MagicMock()
            commands = Commands(io, None)

            mock_novel = MagicMock(spec=[])
            commands._novel_commands = mock_novel

            # /model has completions on the base class that don't need a coder
            result = commands.get_completions("/model")
            # Should return a sorted list from the base class completions_model
            self.assertIsInstance(result, list)
            self.assertGreater(len(result), 0)
        finally:
            commands_mod.litellm = original

    def test_get_raw_completions_prefers_novel(self):
        """get_raw_completions should prefer novel over base"""
        from aider.commands import Commands

        io = MagicMock()
        commands = Commands(io, None)

        mock_completer = MagicMock()
        mock_novel = MagicMock()
        mock_novel.completions_raw_new = mock_completer
        commands._novel_commands = mock_novel

        result = commands.get_raw_completions("/new")
        self.assertIs(result, mock_completer)


class TestInsertAfter(unittest.TestCase):
    """Test the /insert-after command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    # --- Acts ---

    def test_insert_act_after(self):
        """Insert act after act 1 should create act 2 and renumber old act 2"""
        _create_novel_structure(self.tmpdir, acts=3, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 1 New Act")

        act_parent = _novel_dir(self.tmpdir)
        # New act 2 should have title "New Act"
        new_act = _find_dir_by_number(act_parent, "Act", 2)
        self.assertIsNotNone(new_act)
        summary = Path(os.path.join(new_act, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "New Act")

        # Old act 2 should now be act 3
        old_act2 = _find_dir_by_number(act_parent, "Act", 3)
        self.assertIsNotNone(old_act2)
        self.assertIn("Act 2 Title", os.path.basename(old_act2))

        # Old act 3 should now be act 4
        old_act3 = _find_dir_by_number(act_parent, "Act", 4)
        self.assertIsNotNone(old_act3)
        self.assertIn("Act 3 Title", os.path.basename(old_act3))

        # Act 1 should be unchanged
        act1 = _find_dir_by_number(act_parent, "Act", 1)
        self.assertIn("Act 1 Title", os.path.basename(act1))

    def test_insert_act_after_last(self):
        """Insert after the last act should just append"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 2")

        act_parent = _novel_dir(self.tmpdir)
        new_act = _find_dir_by_number(act_parent, "Act", 3)
        self.assertIsNotNone(new_act)
        # No renumbering needed, act 1 and 2 unchanged
        self.assertIsNotNone(_find_dir_by_number(act_parent, "Act", 1))
        self.assertIsNotNone(_find_dir_by_number(act_parent, "Act", 2))

    def test_insert_act_after_nonexistent(self):
        """Should error when reference act doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 99")
        self.io.tool_error.assert_called()

    def test_insert_act_after_adds_to_chat(self):
        """Should add SUMMARY.md to coder"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 1")
        self.assertEqual(len(self.coder.abs_fnames), 1)

    # --- Chapters ---

    def test_insert_chapter_after(self):
        """Insert chapter after chapter 1 in act 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_insert_after("act 1 chapter 1 The Storm")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)

        # New chapter 2 should have title "The Storm"
        new_ch = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(new_ch)
        summary = Path(os.path.join(new_ch, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "The Storm")

        # Old chapter 2 → chapter 3, old chapter 3 → chapter 4
        old_ch2 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(old_ch2)
        self.assertIn("Chapter 2 Title", os.path.basename(old_ch2))

        old_ch3 = _find_dir_by_number(act_dir, "Chapter", 4)
        self.assertIsNotNone(old_ch3)
        self.assertIn("Chapter 3 Title", os.path.basename(old_ch3))

    def test_insert_chapter_uses_last_act(self):
        """When act is omitted, should use the last act"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_insert_after("chapter 1")

        # Should insert in act 2 (the last act)
        act2_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        # Should now have 3 chapters (1, new 2, old 2→3)
        new_ch = _find_dir_by_number(act2_dir, "Chapter", 2)
        self.assertIsNotNone(new_ch)
        old_ch2 = _find_dir_by_number(act2_dir, "Chapter", 3)
        self.assertIsNotNone(old_ch2)

    def test_insert_chapter_after_nonexistent(self):
        """Should error when reference chapter doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=2, scenes=1)
        self.cmds.cmd_insert_after("act 1 chapter 99")
        self.io.tool_error.assert_called()

    def test_insert_chapter_no_acts(self):
        """Should error when no acts exist"""
        self.cmds.cmd_insert_after("chapter 1")
        self.io.tool_error.assert_called()

    # --- Scenes ---

    def test_insert_scene_after(self):
        """Insert scene after scene 1 in act 1, chapter 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=3)
        self.cmds.cmd_insert_after("act 1 chapter 1 scene 1 The Dawn")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)

        # New scene 2
        new_sc = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(new_sc)
        summary = Path(os.path.join(new_sc, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "The Dawn")

        # Old scene 2 → 3, old scene 3 → 4
        old_sc2 = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(old_sc2)
        self.assertIn("Scene 2 Title", os.path.basename(old_sc2))

        old_sc3 = _find_dir_by_number(ch_dir, "Scene", 4)
        self.assertIsNotNone(old_sc3)
        self.assertIn("Scene 3 Title", os.path.basename(old_sc3))

    def test_insert_scene_uses_last_act_and_chapter(self):
        """When act and chapter are omitted, use the last of each"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.cmds.cmd_insert_after("scene 1")

        # Should insert in act 2, chapter 2 (last of each)
        act2_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 2)
        ch2_dir = _find_dir_by_number(act2_dir, "Chapter", 2)
        # Should now have scene 1, new scene 2, old scene 2→3
        self.assertIsNotNone(_find_dir_by_number(ch2_dir, "Scene", 1))
        self.assertIsNotNone(_find_dir_by_number(ch2_dir, "Scene", 2))
        self.assertIsNotNone(_find_dir_by_number(ch2_dir, "Scene", 3))

    def test_insert_scene_after_nonexistent(self):
        """Should error when reference scene doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_insert_after("act 1 chapter 1 scene 99")
        self.io.tool_error.assert_called()

    # --- Parsing errors ---

    def test_insert_after_empty_args(self):
        """Should error with empty args"""
        self.cmds.cmd_insert_after("")
        self.io.tool_error.assert_called()

    def test_insert_after_missing_number(self):
        """Should error when number is missing after keyword"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act")
        self.io.tool_error.assert_called()

    def test_insert_after_chapter_missing_number(self):
        """Should error when chapter number is missing"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 1 chapter")
        self.io.tool_error.assert_called()

    def test_insert_after_scene_missing_number(self):
        """Should error when scene number is missing"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("act 1 chapter 1 scene")
        self.io.tool_error.assert_called()

    def test_insert_after_invalid_keyword(self):
        """Should error with invalid keyword"""
        self.cmds.cmd_insert_after("foobar 1")
        self.io.tool_error.assert_called()

    # --- Shorthand syntax ---

    def test_insert_act_shorthand(self):
        """Shorthand: '1' should insert after act 1"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("1 New Act")

        act_parent = _novel_dir(self.tmpdir)
        new_act = _find_dir_by_number(act_parent, "Act", 2)
        self.assertIsNotNone(new_act)
        summary = Path(os.path.join(new_act, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "New Act")
        # Old act 2 should now be act 3
        old_act2 = _find_dir_by_number(act_parent, "Act", 3)
        self.assertIsNotNone(old_act2)

    def test_insert_chapter_shorthand(self):
        """Shorthand: '1 2' should insert chapter after chapter 2 in act 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_insert_after("1 2 Shorthand Chapter")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        new_ch = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(new_ch)
        summary = Path(os.path.join(new_ch, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Shorthand Chapter")

    def test_insert_scene_shorthand(self):
        """Shorthand: '1 1 2' should insert scene after scene 2 in act 1, chapter 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=3)
        self.cmds.cmd_insert_after("1 1 2 Shorthand Scene")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        new_sc = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIsNotNone(new_sc)
        summary = Path(os.path.join(new_sc, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Shorthand Scene")

    def test_insert_shorthand_no_title(self):
        """Shorthand without title should default to Untitled"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("1")

        act_parent = _novel_dir(self.tmpdir)
        new_act = _find_dir_by_number(act_parent, "Act", 2)
        self.assertIsNotNone(new_act)
        self.assertIn("Untitled", os.path.basename(new_act))

    def test_insert_shorthand_too_many_numbers(self):
        """Shorthand with 4+ numbers should error"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_insert_after("1 1 1 1")
        self.io.tool_error.assert_called()


class TestInsertBeforeShorthand(unittest.TestCase):
    """Test shorthand syntax for /insert-before command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_insert_before_act_shorthand(self):
        """Shorthand: '1 Prologue' should insert before act 1"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_before("1 Prologue")

        act_parent = _novel_dir(self.tmpdir)
        new_act = _find_dir_by_number(act_parent, "Act", 1)
        self.assertIsNotNone(new_act)
        summary = Path(os.path.join(new_act, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Prologue")
        # Old act 1 should now be act 2
        old_act1 = _find_dir_by_number(act_parent, "Act", 2)
        self.assertIn("Act 1 Title", os.path.basename(old_act1))

    def test_insert_before_chapter_shorthand(self):
        """Shorthand: '1 1 Interlude' should insert chapter before chapter 1 in act 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=2, scenes=1)
        self.cmds.cmd_insert_before("1 1 Interlude")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        new_ch = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(new_ch)
        summary = Path(os.path.join(new_ch, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Interlude")

    def test_insert_before_scene_shorthand(self):
        """Shorthand: '1 1 1 Flashback' should insert scene before scene 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_insert_before("1 1 1 Flashback")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        new_sc = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(new_sc)
        summary = Path(os.path.join(new_sc, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Flashback")


class TestInsertBefore(unittest.TestCase):
    """Test the /insert-before command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_insert_act_before(self):
        """Insert act before act 1 should create new act 1 and shift all up"""
        _create_novel_structure(self.tmpdir, acts=3, chapters=1, scenes=1)
        self.cmds.cmd_insert_before("act 1 Prologue")

        act_parent = _novel_dir(self.tmpdir)
        # New act 1 should have title "Prologue"
        new_act = _find_dir_by_number(act_parent, "Act", 1)
        self.assertIsNotNone(new_act)
        summary = Path(os.path.join(new_act, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Prologue")

        # Old act 1 → 2, old act 2 → 3, old act 3 → 4
        old_act1 = _find_dir_by_number(act_parent, "Act", 2)
        self.assertIn("Act 1 Title", os.path.basename(old_act1))
        old_act2 = _find_dir_by_number(act_parent, "Act", 3)
        self.assertIn("Act 2 Title", os.path.basename(old_act2))
        old_act3 = _find_dir_by_number(act_parent, "Act", 4)
        self.assertIn("Act 3 Title", os.path.basename(old_act3))

    def test_insert_chapter_before(self):
        """Insert chapter before chapter 2 in act 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_insert_before("act 1 chapter 2 Interlude")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)

        # New chapter 2
        new_ch = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(new_ch)
        summary = Path(os.path.join(new_ch, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Interlude")

        # Old chapter 1 stays, old 2→3, old 3→4
        ch1 = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIn("Chapter 1 Title", os.path.basename(ch1))
        old_ch2 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIn("Chapter 2 Title", os.path.basename(old_ch2))
        old_ch3 = _find_dir_by_number(act_dir, "Chapter", 4)
        self.assertIn("Chapter 3 Title", os.path.basename(old_ch3))

    def test_insert_scene_before(self):
        """Insert scene before scene 2 in act 1, chapter 1"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=3)
        self.cmds.cmd_insert_before("act 1 chapter 1 scene 2 Flashback")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)

        # Scene 1 unchanged
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIn("Scene 1 Title", os.path.basename(sc1))

        # New scene 2
        new_sc = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(new_sc)
        summary = Path(os.path.join(new_sc, "SUMMARY.md")).read_text(
            encoding="utf-8"
        ).strip()
        self.assertEqual(summary, "Flashback")

        # Old scene 2→3, old scene 3→4
        old_sc2 = _find_dir_by_number(ch_dir, "Scene", 3)
        self.assertIn("Scene 2 Title", os.path.basename(old_sc2))
        old_sc3 = _find_dir_by_number(ch_dir, "Scene", 4)
        self.assertIn("Scene 3 Title", os.path.basename(old_sc3))

    def test_insert_before_nonexistent_ref(self):
        """Should error when reference doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_before("act 5")
        self.io.tool_error.assert_called()

    def test_insert_before_no_title(self):
        """Inserting without a title should default to Untitled"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_insert_before("act 1")

        act_parent = _novel_dir(self.tmpdir)
        new_act = _find_dir_by_number(act_parent, "Act", 1)
        self.assertIn("Untitled", os.path.basename(new_act))


class TestInsertCompletions(unittest.TestCase):
    """Test completions for insert commands."""

    def test_insert_after_completions(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        completions = cmds.completions_insert_after()
        self.assertIn("act", completions)
        self.assertIn("chapter", completions)
        self.assertIn("scene", completions)

    def test_insert_before_completions(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        completions = cmds.completions_insert_before()
        self.assertIn("act", completions)
        self.assertIn("chapter", completions)
        self.assertIn("scene", completions)


class TestInsertGetCommands(unittest.TestCase):
    """Test that insert commands appear in get_commands."""

    def test_insert_commands_registered(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        self.assertIn("insert-after", commands)
        self.assertIn("insert-before", commands)


class TestEditCommand(unittest.TestCase):
    """Test the /edit command (novel alias for /code)."""

    def test_edit_registered(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        self.assertIn("edit", commands)

    def test_edit_no_args_switches_mode(self):
        """With no args, /edit should raise SwitchCoder"""
        from aider.commands import SwitchCoder

        io = MagicMock()
        coder = MagicMock()
        coder.main_model.edit_format = "whole"
        cmds = NovelCommands(io, coder, root="/tmp")
        with self.assertRaises(SwitchCoder):
            cmds.cmd_edit("")

    def test_completions_edit(self):
        from aider.io import CommandCompletionException

        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        with self.assertRaises(CommandCompletionException):
            cmds.completions_edit()


class TestParseLocationArgs(unittest.TestCase):
    """Test the _parse_location_args helper."""

    def setUp(self):
        self.io = MagicMock()
        self.cmds = NovelCommands(self.io, None, root="/tmp")

    def test_keyword_act(self):
        self.assertEqual(self.cmds._parse_location_args("act 1"), [1])

    def test_keyword_act_chapter(self):
        self.assertEqual(
            self.cmds._parse_location_args("act 1 chapter 2"), [1, 2]
        )

    def test_keyword_act_chapter_scene(self):
        self.assertEqual(
            self.cmds._parse_location_args("act 1 chapter 2 scene 3"), [1, 2, 3]
        )

    def test_shorthand_one_number(self):
        self.assertEqual(self.cmds._parse_location_args("1"), [1])

    def test_shorthand_two_numbers(self):
        self.assertEqual(self.cmds._parse_location_args("1 2"), [1, 2])

    def test_shorthand_three_numbers(self):
        self.assertEqual(self.cmds._parse_location_args("1 2 3"), [1, 2, 3])

    def test_too_many_numbers(self):
        result = self.cmds._parse_location_args("1 2 3 4")
        self.assertIsNone(result)
        self.io.tool_error.assert_called()

    def test_missing_number_after_act(self):
        result = self.cmds._parse_location_args("act")
        self.assertIsNone(result)

    def test_missing_number_after_chapter(self):
        result = self.cmds._parse_location_args("act 1 chapter")
        self.assertIsNone(result)

    def test_unexpected_text(self):
        result = self.cmds._parse_location_args("hello")
        self.assertIsNone(result)
        self.io.tool_error.assert_called()


class TestSummarizeCommand(unittest.TestCase):
    """Test the /summarize command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_summarize_registered(self):
        commands = self.cmds.get_commands()
        self.assertIn("summarize", commands)

    def test_summarize_no_args(self):
        self.cmds.cmd_summarize("")
        self.io.tool_error.assert_called()

    def test_summarize_not_found(self):
        self.cmds.cmd_summarize("act 99")
        self.io.tool_error.assert_called()

    def test_summarize_scene_no_prose(self):
        """Summarizing a scene with empty prose should error."""
        # Clear the scene prose
        tree = self.cmds.narrative_map.get_tree()
        scene = tree[0].children[0].children[0]
        prose_path = os.path.join(scene.path, "PROSE.md")
        Path(prose_path).write_text("", encoding="utf-8")
        self.cmds._narrative_map = None
        self.cmds.cmd_summarize("1 1 1")
        self.io.tool_error.assert_called()

    @patch.object(NovelCommands, "_run_with_files")
    def test_summarize_scene_calls_llm(self, mock_run):
        """Summarizing a scene with content should call _run_with_files."""
        self.cmds.cmd_summarize("1 1 1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit the scene's SUMMARY.md
        self.assertTrue(any("SUMMARY.md" in p for p in kwargs["edit_paths"]))
        # Should have PROSE.md as read-only
        self.assertTrue(any("PROSE.md" in p for p in kwargs["read_only_paths"]))

    def test_summarize_chapter_no_content(self):
        """Summarizing a chapter with no scene prose or summaries should error."""
        tree = self.cmds.narrative_map.get_tree()
        scene = tree[0].children[0].children[0]
        # Clear both prose and summary
        Path(os.path.join(scene.path, "SUMMARY.md")).write_text("", encoding="utf-8")
        Path(os.path.join(scene.path, "PROSE.md")).write_text("", encoding="utf-8")
        self.cmds._narrative_map = None
        self.cmds.cmd_summarize("1 1")
        self.io.tool_error.assert_called()

    @patch.object(NovelCommands, "_run_with_files")
    def test_summarize_chapter_drills_to_scenes(self, mock_run):
        """Summarizing a chapter should drill down and summarize each scene."""
        self.cmds.cmd_summarize("1 1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit scene SUMMARY.md files (not chapter-level)
        self.assertTrue(
            any("Scene" in p and "SUMMARY.md" in p for p in kwargs["edit_paths"])
        )
        # Should have scene PROSE.md as read-only
        self.assertTrue(
            any("Scene" in p and "PROSE.md" in p for p in kwargs["read_only_paths"])
        )

    @patch.object(NovelCommands, "_run_with_files")
    def test_summarize_act_drills_to_scenes(self, mock_run):
        """Summarizing an act should drill down and summarize each scene."""
        self.cmds.cmd_summarize("1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit scene SUMMARY.md files
        self.assertTrue(
            any("SUMMARY.md" in p for p in kwargs["edit_paths"])
        )
        # Should have scene PROSE.md as read-only
        self.assertTrue(
            any("PROSE.md" in p for p in kwargs["read_only_paths"])
        )

    def test_completions_summarize(self):
        result = self.cmds.completions_summarize()
        self.assertIn("act", result)
        self.assertIn("chapter", result)
        self.assertIn("scene", result)


class TestSummarizeQuickRemoved(unittest.TestCase):
    """Test that /summarize-quick has been removed."""

    def test_summarize_quick_not_registered(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        self.assertNotIn("summarize-quick", commands)


class TestWriteCommand(unittest.TestCase):
    """Test the /write command (generate content from summary)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_write_registered(self):
        commands = self.cmds.get_commands()
        self.assertIn("write", commands)

    def test_write_no_args(self):
        self.cmds.cmd_write("")
        self.io.tool_error.assert_called()

    def test_write_not_found(self):
        self.cmds.cmd_write("act 99")
        self.io.tool_error.assert_called()

    def test_write_no_summary(self):
        """Writing content without a summary should error."""
        tree = self.cmds.narrative_map.get_tree()
        scene = tree[0].children[0].children[0]
        summary_path = os.path.join(scene.path, "SUMMARY.md")
        Path(summary_path).write_text("", encoding="utf-8")
        self.cmds._narrative_map = None
        self.cmds.cmd_write("1 1 1")
        self.io.tool_error.assert_called()

    @patch.object(NovelCommands, "_run_with_files")
    def test_write_scene_calls_llm(self, mock_run):
        """Writing a scene with a summary should call _run_with_files."""
        self.cmds.cmd_write("1 1 1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit PROSE.md (scenes use PROSE.md, not CONTENT.md)
        self.assertTrue(any("PROSE.md" in p for p in kwargs["edit_paths"]))
        # Should have SUMMARY.md as read-only
        self.assertTrue(any("SUMMARY.md" in p for p in kwargs["read_only_paths"]))

    @patch.object(NovelCommands, "_run_with_files")
    def test_write_chapter_calls_llm(self, mock_run):
        """Writing a chapter should write all scene PROSE.md files."""
        self.cmds.cmd_write("1 1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit PROSE.md for each scene in the chapter
        self.assertTrue(any("PROSE.md" in p for p in kwargs["edit_paths"]))
        # Should have scene SUMMARY.md as read-only context
        scene_summaries = [p for p in kwargs["read_only_paths"]
                           if "Scene" in p and "SUMMARY.md" in p]
        self.assertTrue(len(scene_summaries) > 0)

    @patch.object(NovelCommands, "_run_with_files")
    def test_write_act_calls_llm(self, mock_run):
        """Writing an act should write all scene PROSE.md files across chapters."""
        self.cmds.cmd_write("1")
        mock_run.assert_called_once()
        kwargs = mock_run.call_args[1]
        # Should edit PROSE.md for scenes
        self.assertTrue(any("PROSE.md" in p for p in kwargs["edit_paths"]))
        # Should have scene SUMMARY.md as read-only context
        scene_summaries = [p for p in kwargs["read_only_paths"]
                           if "Scene" in p and "SUMMARY.md" in p]
        self.assertTrue(len(scene_summaries) > 0)

    def test_completions_write(self):
        result = self.cmds.completions_write()
        self.assertIn("act", result)
        self.assertIn("scene", result)


class TestFeedbackCommand(unittest.TestCase):
    """Test the /feedback command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.edit_format = "whole"
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_feedback_registered(self):
        commands = self.cmds.get_commands()
        self.assertIn("feedback", commands)

    def test_feedback_no_args_no_chat_files(self):
        """No args and no files in chat should error."""
        self.coder.abs_fnames = set()
        self.cmds.cmd_feedback("")
        self.io.tool_error.assert_called()

    @patch("composez_core.novel_commands.NovelCommands._get_target_files")
    def test_feedback_no_files_found(self, mock_get):
        """When _get_target_files returns empty list, should error."""
        mock_get.return_value = []
        self.cmds.cmd_feedback("nonexistent")
        self.io.tool_error.assert_called()

    @patch("aider.coders.base_coder.Coder.create")
    @patch("composez_core.novel_coder.load_core_context")
    def test_feedback_scene_loads_files_as_readonly(self, mock_ctx, mock_create):
        """Feedback on a scene should load its files as read-only."""
        from aider.commands import SwitchCoder

        mock_coder = MagicMock()
        mock_coder.abs_fnames = set()
        mock_coder.abs_read_only_fnames = set()
        mock_create.return_value = mock_coder

        with self.assertRaises(SwitchCoder):
            self.cmds.cmd_feedback("1 1 1")

        # The coder should have been created in query mode
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        self.assertEqual(call_kwargs["edit_format"], "query")

        # Files should be read-only (no editable files)
        self.assertEqual(mock_coder.abs_fnames, set())
        # Should have at least PROSE.md and SUMMARY.md as read-only
        ro_files = mock_coder.abs_read_only_fnames
        has_prose = any("PROSE.md" in f for f in ro_files)
        has_summary = any("SUMMARY.md" in f for f in ro_files)
        self.assertTrue(has_prose, "PROSE.md should be in read-only files")
        self.assertTrue(has_summary, "SUMMARY.md should be in read-only files")

        # The prompt should mention priority levels
        mock_coder.run.assert_called_once()
        prompt = mock_coder.run.call_args[0][0]
        self.assertIn("High Priority", prompt)
        self.assertIn("Medium Priority", prompt)
        self.assertIn("Low Priority", prompt)

    @patch("aider.coders.base_coder.Coder.create")
    @patch("composez_core.novel_coder.load_core_context")
    def test_feedback_chapter_loads_all_descendant_files(self, mock_ctx, mock_create):
        """Feedback on a chapter should load all files under it."""
        from aider.commands import SwitchCoder

        mock_coder = MagicMock()
        mock_coder.abs_fnames = set()
        mock_coder.abs_read_only_fnames = set()
        mock_create.return_value = mock_coder

        with self.assertRaises(SwitchCoder):
            self.cmds.cmd_feedback("1 1")

        # Should have scene files as read-only
        ro_files = mock_coder.abs_read_only_fnames
        self.assertTrue(len(ro_files) > 0)

    @patch("aider.coders.base_coder.Coder.create")
    @patch("composez_core.novel_coder.load_core_context")
    def test_feedback_act_loads_all_descendant_files(self, mock_ctx, mock_create):
        """Feedback on an act should load all files under it."""
        from aider.commands import SwitchCoder

        mock_coder = MagicMock()
        mock_coder.abs_fnames = set()
        mock_coder.abs_read_only_fnames = set()
        mock_create.return_value = mock_coder

        with self.assertRaises(SwitchCoder):
            self.cmds.cmd_feedback("1")

        ro_files = mock_coder.abs_read_only_fnames
        self.assertTrue(len(ro_files) > 0)

    @patch("aider.coders.base_coder.Coder.create")
    @patch("composez_core.novel_coder.load_core_context")
    def test_feedback_file_path(self, mock_ctx, mock_create):
        """Feedback on a specific file path should load that file."""
        from aider.commands import SwitchCoder

        mock_coder = MagicMock()
        mock_coder.abs_fnames = set()
        mock_coder.abs_read_only_fnames = set()
        mock_create.return_value = mock_coder

        # Create a standalone file to reference
        test_file = os.path.join(self.tmpdir, "test_chapter.md")
        Path(test_file).write_text("Some chapter content.", encoding="utf-8")

        with self.assertRaises(SwitchCoder):
            self.cmds.cmd_feedback("test_chapter.md")

        ro_files = mock_coder.abs_read_only_fnames
        self.assertTrue(
            any("test_chapter.md" in f for f in ro_files),
            "The specified file should be in read-only files",
        )

    def test_feedback_not_found(self):
        """Requesting feedback on a non-existent location should error."""
        self.cmds.cmd_feedback("act 99")
        self.io.tool_error.assert_called()

    def test_completions_feedback(self):
        result = self.cmds.completions_feedback()
        self.assertIn("act", result)
        self.assertIn("chapter", result)
        self.assertIn("scene", result)


class TestInstructCommand(unittest.TestCase):
    """Test the /instruct command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def _create_instruction(self, name, content="Test instruction content"):
        """Helper to create an instruction file."""
        instructions_dir = os.path.join(self.tmpdir, "instructions")
        os.makedirs(instructions_dir, exist_ok=True)
        path = os.path.join(instructions_dir, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def test_instruct_loads_content(self):
        """Should return the content of the instruction file."""
        self._create_instruction("voice.md", "Write in first person POV.")
        result = self.cmds.cmd_instruct("voice")
        self.assertEqual(result, "Write in first person POV.")

    def test_instruct_with_extension(self):
        """Should find instruction when name includes extension."""
        self._create_instruction("voice.md", "First person.")
        result = self.cmds.cmd_instruct("voice.md")
        self.assertEqual(result, "First person.")

    def test_instruct_txt_extension(self):
        """Should find .txt instruction files."""
        self._create_instruction("tone.txt", "Keep it dark.")
        result = self.cmds.cmd_instruct("tone")
        self.assertEqual(result, "Keep it dark.")

    def test_instruct_case_insensitive(self):
        """Should find instructions case-insensitively."""
        self._create_instruction("Voice.md", "First person.")
        result = self.cmds.cmd_instruct("voice")
        self.assertEqual(result, "First person.")

    def test_instruct_not_found(self):
        """Should error when instruction doesn't exist."""
        os.makedirs(os.path.join(self.tmpdir, "instructions"))
        self.cmds.cmd_instruct("nonexistent")
        self.io.tool_error.assert_called()

    def test_instruct_not_found_lists_available(self):
        """Error message should list available instructions."""
        self._create_instruction("voice.md", "content")
        self.cmds.cmd_instruct("nonexistent")
        error_msg = self.io.tool_error.call_args[0][0]
        self.assertIn("voice", error_msg)

    def test_instruct_empty_args(self):
        """Should error with no arguments."""
        self.cmds.cmd_instruct("")
        self.io.tool_error.assert_called()

    def test_instruct_empty_file(self):
        """Should warn when instruction file is empty."""
        self._create_instruction("empty.md", "")
        self.cmds.cmd_instruct("empty")
        self.io.tool_warning.assert_called()

    def test_instruct_no_directory(self):
        """Should error when instructions/ directory doesn't exist."""
        self.cmds.cmd_instruct("voice")
        self.io.tool_error.assert_called()

    def test_completions_instruct(self):
        """Should return instruction names."""
        self._create_instruction("voice.md", "content")
        self._create_instruction("tone.txt", "content")
        completions = self.cmds.completions_instruct()
        self.assertIn("voice", completions)
        self.assertIn("tone", completions)

    def test_completions_instruct_empty(self):
        """Should return empty list when no instructions exist."""
        cmds = NovelCommands(self.io, None, root=self.tmpdir)
        completions = cmds.completions_instruct()
        self.assertEqual(completions, [])


class TestCreateInstruction(unittest.TestCase):
    """Test /new instruction <name>."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_create_instruction(self):
        """Should create an instruction file."""
        self.cmds.cmd_new("instruction voice")
        path = os.path.join(self.tmpdir, "instructions", "voice.md")
        self.assertTrue(os.path.isfile(path))

    def test_create_instruction_adds_to_chat(self):
        """Should add the new instruction file to chat for editing."""
        self.cmds.cmd_new("instruction voice")
        self.assertEqual(len(self.coder.abs_fnames), 1)
        path = list(self.coder.abs_fnames)[0]
        self.assertTrue(path.endswith("voice.md"))

    def test_create_instruction_creates_dir(self):
        """Should create instructions/ directory if needed."""
        self.cmds.cmd_new("instruction voice")
        self.assertTrue(os.path.isdir(
            os.path.join(self.tmpdir, "instructions")
        ))

    def test_create_instruction_already_exists(self):
        """Should error if instruction already exists."""
        instructions_dir = os.path.join(self.tmpdir, "instructions")
        os.makedirs(instructions_dir)
        Path(os.path.join(instructions_dir, "voice.md")).write_text(
            "existing", encoding="utf-8"
        )
        self.cmds.cmd_new("instruction voice")
        self.io.tool_error.assert_called()

    def test_create_instruction_no_name(self):
        """Should error with no name."""
        self.cmds.cmd_new("instruction")
        self.io.tool_error.assert_called()

    def test_create_instruction_with_extension(self):
        """Should respect explicit extension."""
        self.cmds.cmd_new("instruction notes.txt")
        path = os.path.join(self.tmpdir, "instructions", "notes.txt")
        self.assertTrue(os.path.isfile(path))

    def test_completions_new_includes_instruction(self):
        """completions_new should include 'instruction'."""
        completions = self.cmds.completions_new()
        self.assertIn("instruction", completions)


class TestSetupNovelProjectInstructions(unittest.TestCase):
    """Test that setup_novel_project creates instructions/ directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()

    def test_creates_instructions_on_confirm(self):
        """Should create instructions/ when user confirms."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)
        self.assertTrue(os.path.isdir(
            os.path.join(self.tmpdir, "instructions")
        ))

    def test_skips_when_all_dirs_exist(self):
        """Should skip when act/, db/, and instructions/ all exist."""
        from composez_core import setup_novel_project

        # No container dir needed in collapsed format
        os.makedirs(os.path.join(self.tmpdir, "db"))
        os.makedirs(os.path.join(self.tmpdir, "instructions"))
        setup_novel_project(self.tmpdir, self.io)
        self.io.confirm_ask.assert_not_called()

    def test_prompts_when_instructions_missing(self):
        """Should prompt when act/ and db/ exist but instructions/ is missing."""
        from composez_core import setup_novel_project

        # No container dir needed in collapsed format
        os.makedirs(os.path.join(self.tmpdir, "db"))
        self.io.confirm_ask.return_value = False
        setup_novel_project(self.tmpdir, self.io)
        self.io.confirm_ask.assert_called_once()

    def test_seeds_default_instructions(self):
        """Should seed elaborate.md and condense.md in instructions/."""
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        instructions_dir = os.path.join(self.tmpdir, "instructions")
        elaborate = os.path.join(instructions_dir, "elaborate.md")
        condense = os.path.join(instructions_dir, "condense.md")
        self.assertTrue(os.path.isfile(elaborate))
        self.assertTrue(os.path.isfile(condense))
        self.assertIn("Elaborate", Path(elaborate).read_text(encoding="utf-8"))
        self.assertIn("Condense", Path(condense).read_text(encoding="utf-8"))

    def test_does_not_overwrite_existing_instructions(self):
        """Should not overwrite user-modified instruction files."""
        from composez_core import setup_novel_project

        instructions_dir = os.path.join(self.tmpdir, "instructions")
        os.makedirs(instructions_dir, exist_ok=True)
        custom = os.path.join(instructions_dir, "elaborate.md")
        Path(custom).write_text("My custom elaborate instruction\n", encoding="utf-8")

        # setup_novel_project skips when instructions/ already exists,
        # so call _seed_default_instructions directly
        from composez_core import _seed_default_instructions

        _seed_default_instructions(instructions_dir)

        content = Path(custom).read_text(encoding="utf-8")
        self.assertEqual(content, "My custom elaborate instruction\n")


class TestDeleteDbEntry(unittest.TestCase):
    """Test /delete db ... for deleting db entries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_delete_db_entry(self):
        """Should delete a db entry file."""
        db = Db(self.tmpdir)
        entry = db.create_entry("characters", "alice")
        self.assertTrue(os.path.isfile(entry.path))

        self.cmds.cmd_delete("db alice")
        self.assertFalse(os.path.isfile(entry.path))
        self.io.tool_output.assert_called()

    def test_delete_db_entry_with_category(self):
        """Should delete a db entry when category is specified."""
        db = Db(self.tmpdir)
        entry = db.create_entry("characters", "alice")

        self.cmds.cmd_delete("db characters alice")
        self.assertFalse(os.path.isfile(entry.path))

    def test_delete_db_entry_not_found(self):
        """Should error when entry doesn't exist."""
        self.cmds.cmd_delete("db nonexistent")
        self.io.tool_error.assert_called()

    def test_delete_db_removes_from_chat(self):
        """Should remove deleted file from coder's file sets."""
        db = Db(self.tmpdir)
        entry = db.create_entry("characters", "alice")
        abs_path = os.path.abspath(entry.path)
        self.coder.abs_fnames.add(abs_path)
        self.coder.abs_read_only_fnames.add(abs_path)

        self.cmds.cmd_delete("db alice")
        self.assertNotIn(abs_path, self.coder.abs_fnames)
        self.assertNotIn(abs_path, self.coder.abs_read_only_fnames)

    def test_delete_db_no_name(self):
        """Should error with no name."""
        self.cmds.cmd_delete("db")
        self.io.tool_error.assert_called()

    def test_delete_empty_args(self):
        """Should error with empty args."""
        self.cmds.cmd_delete("")
        self.io.tool_error.assert_called()

    def test_delete_invalid_keyword(self):
        """Should error with invalid keyword."""
        self.cmds.cmd_delete("foobar something")
        self.io.tool_error.assert_called()


class TestDeleteInstruction(unittest.TestCase):
    """Test /delete instruction ... for deleting instruction files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def _create_instruction(self, name, content="Test content"):
        instructions_dir = os.path.join(self.tmpdir, "instructions")
        os.makedirs(instructions_dir, exist_ok=True)
        path = os.path.join(instructions_dir, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def test_delete_instruction(self):
        """Should delete an instruction file."""
        path = self._create_instruction("voice.md")
        self.assertTrue(os.path.isfile(path))

        self.cmds.cmd_delete("instruction voice")
        self.assertFalse(os.path.isfile(path))
        self.io.tool_output.assert_called()

    def test_delete_instruction_not_found(self):
        """Should error when instruction doesn't exist."""
        os.makedirs(os.path.join(self.tmpdir, "instructions"))
        self.cmds.cmd_delete("instruction nonexistent")
        self.io.tool_error.assert_called()

    def test_delete_instruction_lists_available(self):
        """Error message should list available instructions."""
        self._create_instruction("voice.md")
        self.cmds.cmd_delete("instruction nonexistent")
        error_msg = self.io.tool_error.call_args[0][0]
        self.assertIn("voice", error_msg)

    def test_delete_instruction_no_directory(self):
        """Should error when instructions/ doesn't exist."""
        self.cmds.cmd_delete("instruction voice")
        self.io.tool_error.assert_called()

    def test_delete_instruction_no_name(self):
        """Should error with no name."""
        self.cmds.cmd_delete("instruction")
        self.io.tool_error.assert_called()

    def test_delete_instruction_removes_from_chat(self):
        """Should remove deleted file from coder's file sets."""
        path = self._create_instruction("voice.md")
        abs_path = os.path.abspath(path)
        self.coder.abs_fnames.add(abs_path)

        self.cmds.cmd_delete("instruction voice")
        self.assertNotIn(abs_path, self.coder.abs_fnames)

    def test_delete_instruction_with_extension(self):
        """Should find instruction by full filename."""
        path = self._create_instruction("voice.md")
        self.cmds.cmd_delete("instruction voice.md")
        self.assertFalse(os.path.isfile(path))

    def test_delete_instruction_txt(self):
        """Should delete .txt instruction files."""
        path = self._create_instruction("tone.txt")
        self.cmds.cmd_delete("instruction tone")
        self.assertFalse(os.path.isfile(path))


class TestDeleteProse(unittest.TestCase):
    """Test /delete prose ... for deleting PROSE.md files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_delete_prose_scene(self):
        """Delete prose for a specific scene."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("prose act 1 chapter 1 scene 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)

        # PROSE.md removed from scene 1 but not scene 2
        self.assertFalse(os.path.exists(os.path.join(sc1, "PROSE.md")))
        self.assertTrue(os.path.exists(os.path.join(sc2, "PROSE.md")))
        # Directory structure preserved
        self.assertTrue(os.path.isdir(sc1))
        # SUMMARY.md untouched
        self.assertTrue(os.path.exists(os.path.join(sc1, "SUMMARY.md")))

    def test_delete_prose_chapter(self):
        """Delete prose for a chapter should remove all PROSE.md in descendant scenes."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("prose act 1 chapter 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)

        # Both scenes should have PROSE.md removed
        self.assertFalse(os.path.exists(os.path.join(sc1, "PROSE.md")))
        self.assertFalse(os.path.exists(os.path.join(sc2, "PROSE.md")))
        # Directories preserved
        self.assertTrue(os.path.isdir(sc1))
        self.assertTrue(os.path.isdir(sc2))

    def test_delete_prose_shorthand(self):
        """Shorthand: 'prose 1 1' should delete PROSE.md under chapter 1."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("prose 1 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertFalse(os.path.exists(os.path.join(sc1, "PROSE.md")))

    def test_delete_prose_cancelled(self):
        """Declining confirmation should not delete."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.io.confirm_ask.return_value = False

        self.cmds.cmd_delete("prose 1 1 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertTrue(os.path.exists(os.path.join(sc1, "PROSE.md")))

    def test_delete_prose_not_found(self):
        """Nonexistent location should error."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_delete("prose act 99")
        self.io.tool_error.assert_called()

    def test_delete_prose_no_files(self):
        """Should error when no PROSE.md files exist at the location."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        # Remove the PROSE.md first
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        os.remove(os.path.join(sc1, "PROSE.md"))

        self.cmds.cmd_delete("prose 1 1 1")
        self.io.tool_error.assert_called()

    def test_delete_prose_removes_from_coder(self):
        """Deleting should remove files from coder's file sets."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        prose_path = os.path.abspath(os.path.join(sc1, "PROSE.md"))
        self.coder.abs_fnames = {prose_path}
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("prose 1 1 1")
        self.assertNotIn(prose_path, self.coder.abs_fnames)

    def test_delete_prose_no_args(self):
        """Missing location should error."""
        self.cmds.cmd_delete("prose")
        self.io.tool_error.assert_called()


class TestDeleteSummaries(unittest.TestCase):
    """Test /delete summaries ... for deleting SUMMARY.md files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_delete_summaries_scene(self):
        """Delete summaries for a specific scene."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("summaries act 1 chapter 1 scene 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)

        # SUMMARY.md removed from scene 1 but not scene 2
        self.assertFalse(os.path.exists(os.path.join(sc1, "SUMMARY.md")))
        self.assertTrue(os.path.exists(os.path.join(sc2, "SUMMARY.md")))
        # PROSE.md untouched
        self.assertTrue(os.path.exists(os.path.join(sc1, "PROSE.md")))

    def test_delete_summaries_chapter(self):
        """Delete summaries for a chapter should remove all SUMMARY.md in descendants."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("summaries act 1 chapter 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)

        # Chapter SUMMARY.md and both scene SUMMARY.md removed
        self.assertFalse(os.path.exists(os.path.join(ch_dir, "SUMMARY.md")))
        self.assertFalse(os.path.exists(os.path.join(sc1, "SUMMARY.md")))
        self.assertFalse(os.path.exists(os.path.join(sc2, "SUMMARY.md")))

    def test_delete_summaries_act(self):
        """Delete summaries for an act should remove all SUMMARY.md in descendants."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.io.confirm_ask.return_value = True

        self.cmds.cmd_delete("summaries act 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)

        # All SUMMARY.md removed
        self.assertFalse(os.path.exists(os.path.join(act_dir, "SUMMARY.md")))
        self.assertFalse(os.path.exists(os.path.join(ch_dir, "SUMMARY.md")))
        self.assertFalse(os.path.exists(os.path.join(sc1, "SUMMARY.md")))
        # PROSE.md untouched
        self.assertTrue(os.path.exists(os.path.join(sc1, "PROSE.md")))

    def test_delete_summaries_no_args(self):
        """Missing location should error."""
        self.cmds.cmd_delete("summaries")
        self.io.tool_error.assert_called()


class TestDeleteNarrativeBlocked(unittest.TestCase):
    """Test that /delete no longer allows deleting whole directories."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_delete_act_blocked(self):
        """Bare /delete act 1 should be rejected."""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_delete("act 1")
        self.io.tool_error.assert_called()
        # Directories should still exist
        self.assertIsNotNone(
            _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        )

    def test_delete_shorthand_blocked(self):
        """Bare /delete 1 should be rejected."""
        _create_novel_structure(self.tmpdir, acts=2, chapters=1, scenes=1)
        self.cmds.cmd_delete("1")
        self.io.tool_error.assert_called()

    def test_delete_scene_shorthand_blocked(self):
        """Bare /delete 1 1 1 should be rejected."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        self.cmds.cmd_delete("1 1 1")
        self.io.tool_error.assert_called()

    def test_delete_empty_args(self):
        """Empty args should error."""
        self.cmds.cmd_delete("")
        self.io.tool_error.assert_called()


class TestDeleteCompletions(unittest.TestCase):
    """Test completions for /delete command."""

    def test_completions_delete(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        completions = cmds.completions_delete()
        self.assertIn("db", completions)
        self.assertIn("instruction", completions)
        self.assertIn("prose", completions)
        self.assertIn("summaries", completions)
        # Level names should no longer appear (no direct directory deletion)
        self.assertNotIn("act", completions)
        self.assertNotIn("chapter", completions)
        self.assertNotIn("scene", completions)


class TestDeleteGetCommands(unittest.TestCase):
    """Test that /delete appears in get_commands."""

    def test_delete_registered(self):
        io = MagicMock()
        cmds = NovelCommands(io, None, root="/tmp")
        commands = cmds.get_commands()
        self.assertIn("delete", commands)


class TestDbDeleteEntry(unittest.TestCase):
    """Test Db.delete_entry method."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Db(self.tmpdir)
        self.db.init_db()

    def test_delete_entry(self):
        entry = self.db.create_entry("characters", "alice")
        self.assertTrue(os.path.isfile(entry.path))

        deleted = self.db.delete_entry("alice")
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted.name, "alice")
        self.assertFalse(os.path.isfile(entry.path))

    def test_delete_entry_with_category(self):
        self.db.create_entry("characters", "alice")
        self.db.create_entry("locations", "alice")

        deleted = self.db.delete_entry("alice", category="locations")
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted.category, "locations")

        # characters/alice should still exist
        remaining = self.db.get_entry("alice", category="characters")
        self.assertIsNotNone(remaining)

    def test_delete_entry_not_found(self):
        deleted = self.db.delete_entry("nonexistent")
        self.assertIsNone(deleted)

    def test_delete_invalidates_cache(self):
        entries_baseline = self.db.get_entries()  # core/style.md from init_db
        self.db.create_entry("characters", "alice")
        entries_before = self.db.get_entries()
        self.assertEqual(len(entries_before), len(entries_baseline) + 1)

        self.db.delete_entry("alice")
        entries_after = self.db.get_entries()
        self.assertEqual(len(entries_after), len(entries_baseline))


class TestCommandOverrides(unittest.TestCase):
    """Test overridden standard commands."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_hidden_commands(self):
        """hidden_commands should include /test, /code, /architect, /context, /map, /map-refresh."""
        hidden = self.cmds.hidden_commands()
        self.assertIn("/test", hidden)
        self.assertIn("/code", hidden)
        self.assertIn("/architect", hidden)
        self.assertIn("/context", hidden)
        self.assertIn("/map", hidden)
        self.assertIn("/map-refresh", hidden)

    def test_hidden_not_in_get_commands(self):
        """/test and /code should not appear in get_commands."""
        commands = self.cmds.get_commands()
        self.assertNotIn("test", commands)
        self.assertNotIn("code", commands)

    def test_ls_shows_narrative(self):
        """/ls should show narrative structure."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        self.assertIn("Narrative structure", output_text)

    def test_ls_shows_db_entries(self):
        """/ls should show db entries."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        # Create a db entry
        db = Db(self.tmpdir)
        db.init_db()
        db.create_entry("characters", "alice")
        self.cmds._db = db

        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        self.assertIn("alice", output_text)

    def test_ls_shows_chat_files(self):
        """/ls should show files currently in the chat."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        prose_path = os.path.abspath(os.path.join(sc_dir, "PROSE.md"))
        self.coder.abs_fnames = {prose_path}

        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        self.assertIn("Files in chat", output_text)

    def test_ls_shows_word_counts(self):
        """/ls should show word counts next to each file."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        prose_path = os.path.abspath(os.path.join(sc_dir, "PROSE.md"))
        self.coder.abs_fnames = {prose_path}

        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        self.assertIn("words)", output_text)

    def test_ls_collapses_full_scene(self):
        """/ls should show scene directory when all its files are in chat."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        # Add both SUMMARY.md and PROSE.md for the scene
        self.coder.abs_fnames = {
            os.path.abspath(os.path.join(sc_dir, "SUMMARY.md")),
            os.path.abspath(os.path.join(sc_dir, "PROSE.md")),
        }
        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        # Should show the scene directory, not individual files
        self.assertNotIn("SUMMARY.md", output_text)
        self.assertNotIn("PROSE.md", output_text)
        self.assertIn("Scene", output_text)

    def test_ls_no_collapse_partial(self):
        """/ls should list individual files when directory is not fully covered."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        # Add only PROSE.md — not all files in the scene directory
        self.coder.abs_fnames = {
            os.path.abspath(os.path.join(sc_dir, "PROSE.md")),
        }
        self.cmds.cmd_ls("")
        output_calls = self.io.tool_output.call_args_list
        output_text = " ".join(
            str(call[0][0]) for call in output_calls if call[0]
        )
        self.assertIn("PROSE.md", output_text)

    def test_collapse_paths_recursive(self):
        """_collapse_paths should recursively collapse fully-covered subtrees."""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        # Collect ALL files in the structure
        all_files = set()
        for dirpath, _dirnames, filenames in os.walk(self.tmpdir):
            for fname in filenames:
                if fname.startswith("."):
                    continue
                full = os.path.join(dirpath, fname)
                all_files.add(os.path.relpath(full, self.tmpdir))
        collapsed = self.cmds._collapse_paths(all_files)
        # Everything should collapse up to the novel/ directory
        self.assertEqual(len(collapsed), 1)
        self.assertTrue(collapsed[0].startswith("novel/Act ") or collapsed[0] == "novel/")

    def test_ls_registered(self):
        """cmd_ls should be discoverable."""
        commands = self.cmds.get_commands()
        self.assertIn("ls", commands)

    def test_compose_registered(self):
        """cmd_compose should be discoverable."""
        commands = self.cmds.get_commands()
        self.assertIn("compose", commands)


class TestCoreDbCategory(unittest.TestCase):
    """Test /new core and /drop with automatic core context re-loading."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)

        # Initialize db with core seeded
        from composez_core.db import Db

        db = Db(self.tmpdir)
        db.init_db()

        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_create_core_adds_as_read_only(self):
        """/new db core voice should add the entry as read-only, not editable."""
        self.cmds.cmd_new("db core voice")
        # Should be in read-only, not editable
        self.assertEqual(len(self.coder.abs_read_only_fnames), 1)
        self.assertEqual(len(self.coder.abs_fnames), 0)
        path = list(self.coder.abs_read_only_fnames)[0]
        self.assertIn("core", path)
        self.assertIn("voice", path)

    def test_create_non_core_adds_as_editable(self):
        """/new db characters alice should add as editable (not read-only)."""
        self.cmds.cmd_new("db characters alice")
        self.assertEqual(len(self.coder.abs_fnames), 1)
        self.assertEqual(len(self.coder.abs_read_only_fnames), 0)

    def test_drop_reloads_core(self):
        """/drop (no args) should re-load core files after clearing."""
        # Set up a parent_commands mock that has cmd_drop
        parent_commands = MagicMock()
        self.cmds._parent_commands = parent_commands

        self.cmds.cmd_drop("")

        parent_commands.cmd_drop.assert_called_once_with("")
        # Core style.md should be loaded into read-only
        core_paths = [
            p for p in self.coder.abs_read_only_fnames if "core" in p
        ]
        self.assertGreaterEqual(len(core_paths), 1)

    def test_drop_specific_file_reloads_core(self):
        """/drop somefile should still re-load core after the parent drop."""
        parent_commands = MagicMock()
        self.cmds._parent_commands = parent_commands

        self.cmds.cmd_drop("somefile.md")
        parent_commands.cmd_drop.assert_called_once_with("somefile.md")

    def test_drop_registered(self):
        """cmd_drop should be in get_commands."""
        commands = self.cmds.get_commands()
        self.assertIn("drop", commands)


class TestSaveLoad(unittest.TestCase):
    """Test /save and /load commands for chat and context."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=1)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.done_messages = []
        self.coder.cur_messages = []
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    # -- argument parsing --

    def test_save_no_args_shows_error(self):
        self.cmds.cmd_save("")
        self.io.tool_error.assert_called()

    def test_save_invalid_kind_shows_error(self):
        self.cmds.cmd_save("banana")
        self.io.tool_error.assert_called()

    def test_load_no_args_shows_error(self):
        self.cmds.cmd_load("")
        self.io.tool_error.assert_called()

    # -- save/load chat --

    def test_save_chat_default_name(self):
        """Saving chat with no name uses 'default'."""
        self.coder.done_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        self.cmds.cmd_save("chat")
        path = os.path.join(self.tmpdir, "cache", "chat", "default.yml")
        self.assertTrue(os.path.isfile(path))
        self.io.tool_output.assert_called()

    def test_save_chat_named(self):
        """Saving chat with a name creates the right file."""
        self.coder.done_messages = [
            {"role": "user", "content": "test"},
        ]
        self.cmds.cmd_save("chat myslot")
        path = os.path.join(self.tmpdir, "cache", "chat", "myslot.yml")
        self.assertTrue(os.path.isfile(path))

    def test_save_chat_empty_warns(self):
        """Saving empty chat shows a warning."""
        self.cmds.cmd_save("chat")
        self.io.tool_warning.assert_called()

    def test_load_chat_missing_shows_error(self):
        """Loading from non-existent slot shows error."""
        self.cmds.cmd_load("chat nosuch")
        self.io.tool_error.assert_called()

    def test_save_load_chat_roundtrip(self):
        """Save and load chat preserves messages."""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "write a scene"},
        ]
        self.coder.done_messages = messages[:2]
        self.coder.cur_messages = messages[2:]

        self.cmds.cmd_save("chat")

        # Clear messages
        self.coder.done_messages = []
        self.coder.cur_messages = []

        self.cmds.cmd_load("chat")

        # All messages should be in done_messages now
        self.assertEqual(len(self.coder.done_messages), 3)
        self.assertEqual(self.coder.done_messages[0]["content"], "hello")
        self.assertEqual(self.coder.done_messages[2]["content"], "write a scene")
        self.assertEqual(self.coder.cur_messages, [])

    # -- save/load context --

    def test_save_ctx_default_name(self):
        """Saving context with no name uses 'default'."""
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc_dir = _find_dir_by_number(ch_dir, "Scene", 1)
        prose = os.path.abspath(os.path.join(sc_dir, "PROSE.md"))
        self.coder.abs_fnames = {prose}
        self.cmds.cmd_save("ctx")
        path = os.path.join(self.tmpdir, "cache", "context", "default.yml")
        self.assertTrue(os.path.isfile(path))

    def test_save_context_keyword(self):
        """'context' should work as an alias for 'ctx'."""
        self.coder.abs_fnames = {os.path.join(self.tmpdir, "act", "dummy.md")}
        self.cmds.cmd_save("context")
        path = os.path.join(self.tmpdir, "cache", "context", "default.yml")
        self.assertTrue(os.path.isfile(path))

    def test_save_ctx_empty_warns(self):
        """Saving empty context shows a warning."""
        self.cmds.cmd_save("ctx")
        self.io.tool_warning.assert_called()

    def test_save_load_ctx_roundtrip(self):
        """Save and load context preserves file sets."""
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        summary = os.path.abspath(os.path.join(act_dir, "SUMMARY.md"))
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        ch_summary = os.path.abspath(os.path.join(ch_dir, "SUMMARY.md"))

        self.coder.abs_fnames = {summary}
        self.coder.abs_read_only_fnames = {ch_summary}

        self.cmds.cmd_save("ctx myctx")

        # Clear file sets
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()

        self.cmds.cmd_load("ctx myctx")

        self.assertEqual(len(self.coder.abs_fnames), 1)
        self.assertIn(summary, self.coder.abs_fnames)
        self.assertEqual(len(self.coder.abs_read_only_fnames), 1)
        self.assertIn(ch_summary, self.coder.abs_read_only_fnames)

    def test_load_ctx_missing_file_warns(self):
        """Loading context with a deleted file warns about missing files."""
        fake_path = os.path.join(self.tmpdir, "nonexistent.md")
        self.coder.abs_fnames = {fake_path}
        self.cmds.cmd_save("ctx")

        self.coder.abs_fnames = set()
        self.cmds.cmd_load("ctx")

        # File didn't exist, so abs_fnames should be empty
        self.assertEqual(len(self.coder.abs_fnames), 0)
        self.io.tool_warning.assert_called()

    def test_load_ctx_missing_slot_shows_error(self):
        """Loading from non-existent slot shows error."""
        self.cmds.cmd_load("ctx nosuch")
        self.io.tool_error.assert_called()

    # -- completions --

    def test_completions_save(self):
        completions = self.cmds.completions_save()
        self.assertIn("chat", completions)
        self.assertIn("ctx", completions)

    def test_completions_load(self):
        completions = self.cmds.completions_load()
        self.assertIn("chat", completions)
        self.assertIn("ctx", completions)

    # -- command registration --

    def test_save_load_in_get_commands(self):
        commands = self.cmds.get_commands()
        self.assertIn("save", commands)
        self.assertIn("load", commands)

    # -- cache directory creation --

    def test_cache_dirs_created_on_save(self):
        """Saving should create cache dirs even if they don't exist."""
        self.coder.done_messages = [{"role": "user", "content": "hi"}]
        self.cmds.cmd_save("chat")
        self.assertTrue(
            os.path.isdir(os.path.join(self.tmpdir, "cache", "chat"))
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.tmpdir, "cache", "context"))
        )


class TestSetupNovelProjectCache(unittest.TestCase):
    """Test that setup_novel_project creates cache dirs and .gitignore."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()

    def test_creates_cache_dirs(self):
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        self.assertTrue(
            os.path.isdir(os.path.join(self.tmpdir, "cache", "chat"))
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.tmpdir, "cache", "context"))
        )

    def test_creates_gitignore(self):
        from composez_core import setup_novel_project

        self.io.confirm_ask.return_value = True
        setup_novel_project(self.tmpdir, self.io)

        gitignore_path = os.path.join(self.tmpdir, ".gitignore")
        self.assertTrue(os.path.isfile(gitignore_path))
        content = Path(gitignore_path).read_text(encoding="utf-8")
        self.assertIn(".aider*", content)
        self.assertIn("cache/", content)

    def test_gitignore_no_duplicates(self):
        """Running setup twice should not duplicate .gitignore entries."""
        from composez_core import _ensure_gitignore

        _ensure_gitignore(self.tmpdir)
        _ensure_gitignore(self.tmpdir)

        gitignore_path = os.path.join(self.tmpdir, ".gitignore")
        content = Path(gitignore_path).read_text(encoding="utf-8")
        self.assertEqual(content.count(".aider*"), 1)
        self.assertEqual(content.count("cache/"), 1)

    def test_gitignore_appends_to_existing(self):
        """Should add missing entries to an existing .gitignore."""
        from composez_core import _ensure_gitignore

        gitignore_path = os.path.join(self.tmpdir, ".gitignore")
        Path(gitignore_path).write_text("*.pyc\n", encoding="utf-8")

        _ensure_gitignore(self.tmpdir)

        content = Path(gitignore_path).read_text(encoding="utf-8")
        self.assertIn("*.pyc", content)
        self.assertIn(".aider*", content)
        self.assertIn("cache/", content)

    def test_gitignore_skips_existing_entries(self):
        """Should not add entries that already exist."""
        from composez_core import _ensure_gitignore

        gitignore_path = os.path.join(self.tmpdir, ".gitignore")
        Path(gitignore_path).write_text(".aider*\n", encoding="utf-8")

        _ensure_gitignore(self.tmpdir)

        content = Path(gitignore_path).read_text(encoding="utf-8")
        self.assertEqual(content.count(".aider*"), 1)
        # cache/ should still be added
        self.assertIn("cache/", content)


class TestLocationToFiles(unittest.TestCase):
    """Test the _location_to_files helper and _get_target_files with location args."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.coder.repo = None
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    # -- _location_to_files --

    def test_scene_keyword(self):
        """act 1 chapter 1 scene 1 → SUMMARY.md + PROSE.md"""
        files = self.cmds._location_to_files("act 1 chapter 1 scene 1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.endswith("SUMMARY.md") for f in files))
        self.assertTrue(any(f.endswith("PROSE.md") for f in files))

    def test_scene_shorthand(self):
        """1 1 1 → same as act 1 chapter 1 scene 1"""
        files = self.cmds._location_to_files("1 1 1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 2)

    def test_chapter_keyword(self):
        """act 1 chapter 1 → ch SUMMARY + 2 scenes × (SUMMARY + PROSE) = 5"""
        files = self.cmds._location_to_files("act 1 chapter 1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 5)

    def test_chapter_shorthand(self):
        """1 1 → same as act 1 chapter 1"""
        files = self.cmds._location_to_files("1 1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 5)

    def test_act_keyword(self):
        """act 1 → all files under act 1 (1 + 2×5 = 11)"""
        files = self.cmds._location_to_files("act 1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 11)

    def test_act_shorthand(self):
        """1 → same as act 1"""
        files = self.cmds._location_to_files("1")
        self.assertIsNotNone(files)
        self.assertEqual(len(files), 11)

    def test_nonexistent_returns_empty(self):
        """act 99 → [] (valid syntax but node doesn't exist)"""
        files = self.cmds._location_to_files("act 99")
        self.assertEqual(files, [])

    def test_non_location_returns_none(self):
        """somefile.txt → None (not a valid location)"""
        files = self.cmds._location_to_files("somefile.txt")
        self.assertIsNone(files)

    def test_all_paths_are_absolute(self):
        files = self.cmds._location_to_files("1 1 1")
        for f in files:
            self.assertTrue(os.path.isabs(f))

    # -- _get_target_files with location args --

    def test_get_target_files_with_location(self):
        """_get_target_files should resolve location args"""
        files = self.cmds._get_target_files("act 1 chapter 1")
        self.assertEqual(len(files), 5)

    def test_get_target_files_shorthand(self):
        """_get_target_files should resolve shorthand"""
        files = self.cmds._get_target_files("1 1 1")
        self.assertEqual(len(files), 2)

    def test_get_target_files_falls_back_to_paths(self):
        """Non-location args should fall back to file paths"""
        test_file = os.path.join(self.tmpdir, "test.txt")
        Path(test_file).write_text("hello", encoding="utf-8")
        files = self.cmds._get_target_files("test.txt")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0], test_file)

    def test_get_target_files_defaults_to_chat(self):
        """No args → files currently in chat"""
        abs_path = os.path.join(self.tmpdir, "foo.md")
        self.coder.abs_fnames = {abs_path}
        files = self.cmds._get_target_files("")
        self.assertEqual(files, [abs_path])


class TestMoveNarrative(unittest.TestCase):
    """Test the /move command for narrative nodes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    # --- Basic same-parent moves ---

    def test_move_chapter_forward(self):
        """Move chapter 1 to position 2 within the same act"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_move("1 1 to 1 2")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        # Old chapter 1 should now be at position 2
        ch2 = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch2)
        self.assertIn("Chapter 1 Title", os.path.basename(ch2))

        # Old chapter 2 should now be at position 1
        ch1 = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch1)
        self.assertIn("Chapter 2 Title", os.path.basename(ch1))

        # Old chapter 3 stays at position 3
        ch3 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch3)
        self.assertIn("Chapter 3 Title", os.path.basename(ch3))

    def test_move_chapter_backward(self):
        """Move chapter 3 to position 1 within the same act"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_move("1 3 to 1 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        # Old chapter 3 should now be at position 1
        ch1 = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch1)
        self.assertIn("Chapter 3 Title", os.path.basename(ch1))

        # Old chapter 1 should now be at position 2
        ch2 = _find_dir_by_number(act_dir, "Chapter", 2)
        self.assertIsNotNone(ch2)
        self.assertIn("Chapter 1 Title", os.path.basename(ch2))

        # Old chapter 2 should now be at position 3
        ch3 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch3)
        self.assertIn("Chapter 2 Title", os.path.basename(ch3))

    def test_move_act_forward(self):
        """Move act 1 to position 3"""
        _create_novel_structure(self.tmpdir, acts=3, chapters=1, scenes=1)
        self.cmds.cmd_move("1 to 3")

        novel_root = _novel_dir(self.tmpdir)
        # Old act 1 → position 3
        act3 = _find_dir_by_number(novel_root, "Act", 3)
        self.assertIsNotNone(act3)
        self.assertIn("Act 1 Title", os.path.basename(act3))

        # Old act 2 → position 1
        act1 = _find_dir_by_number(novel_root, "Act", 1)
        self.assertIsNotNone(act1)
        self.assertIn("Act 2 Title", os.path.basename(act1))

        # Old act 3 → position 2
        act2 = _find_dir_by_number(novel_root, "Act", 2)
        self.assertIsNotNone(act2)
        self.assertIn("Act 3 Title", os.path.basename(act2))

    def test_move_scene_within_chapter(self):
        """Move scene 1 to position 2 within the same chapter"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=3)
        self.cmds.cmd_move("1 1 1 to 1 1 2")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)

        # Old scene 1 → position 2
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)
        self.assertIsNotNone(sc2)
        self.assertIn("Scene 1 Title", os.path.basename(sc2))

        # Old scene 2 → position 1
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        self.assertIsNotNone(sc1)
        self.assertIn("Scene 2 Title", os.path.basename(sc1))

    # --- Move to end of parent ---

    def test_move_chapter_to_end(self):
        """Move chapter 1 to end of act 1 (using parent-depth target)"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_move("1 1 to 1")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        # Old chapter 1 should now be at position 3
        ch3 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch3)
        self.assertIn("Chapter 1 Title", os.path.basename(ch3))

        # Old chapter 2 → position 1
        ch1 = _find_dir_by_number(act_dir, "Chapter", 1)
        self.assertIsNotNone(ch1)
        self.assertIn("Chapter 2 Title", os.path.basename(ch1))

    # --- Cross-parent moves ---

    def test_move_chapter_across_acts(self):
        """Move chapter from act 1 to act 2"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=1)
        self.cmds.cmd_move("1 1 to 2 1")

        novel_root = _novel_dir(self.tmpdir)
        act1_dir = _find_dir_by_number(novel_root, "Act", 1)
        act2_dir = _find_dir_by_number(novel_root, "Act", 2)

        # Act 1 should now have 1 chapter (old chapter 2 → chapter 1)
        ch1_in_act1 = _find_dir_by_number(act1_dir, "Chapter", 1)
        self.assertIsNotNone(ch1_in_act1)
        self.assertIn("Chapter 2 Title", os.path.basename(ch1_in_act1))

        # Act 2 should now have 3 chapters
        # New chapter 1 is the moved one (old act1 chapter 1)
        ch1_in_act2 = _find_dir_by_number(act2_dir, "Chapter", 1)
        self.assertIsNotNone(ch1_in_act2)
        self.assertIn("Chapter 1 Title", os.path.basename(ch1_in_act2))

        # Old act 2 chapters shifted
        ch2_in_act2 = _find_dir_by_number(act2_dir, "Chapter", 2)
        self.assertIsNotNone(ch2_in_act2)

        ch3_in_act2 = _find_dir_by_number(act2_dir, "Chapter", 3)
        self.assertIsNotNone(ch3_in_act2)

    # --- Keyword syntax ---

    def test_move_keyword_syntax(self):
        """Move using keyword syntax"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_move("act 1 chapter 1 to act 1 chapter 3")

        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch3 = _find_dir_by_number(act_dir, "Chapter", 3)
        self.assertIsNotNone(ch3)
        self.assertIn("Chapter 1 Title", os.path.basename(ch3))

    # --- Content preserved ---

    def test_move_preserves_content(self):
        """Move should preserve all files inside the moved directory"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1 = _find_dir_by_number(ch_dir, "Scene", 1)
        original_prose = Path(os.path.join(sc1, "PROSE.md")).read_text(encoding="utf-8")

        self.cmds.cmd_move("1 1 1 to 1 1 2")

        # Scene 1 content is now at position 2
        sc2 = _find_dir_by_number(ch_dir, "Scene", 2)
        new_prose = Path(os.path.join(sc2, "PROSE.md")).read_text(encoding="utf-8")
        self.assertEqual(original_prose, new_prose)

    # --- Edge cases ---

    def test_move_same_position(self):
        """Moving to the same position should be a no-op"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=3, scenes=1)
        self.cmds.cmd_move("1 1 to 1 1")
        self.io.tool_output.assert_called_with(
            "Source and target are the same; nothing to do."
        )

    def test_move_nonexistent_source(self):
        """Should error when source doesn't exist"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=2, scenes=1)
        self.cmds.cmd_move("1 99 to 1 1")
        self.io.tool_error.assert_called()

    def test_move_invalid_depth_mismatch(self):
        """Should error when target depth doesn't match"""
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.cmds.cmd_move("1 1 1 to 2")
        self.io.tool_error.assert_called()

    def test_move_no_args(self):
        """Should show usage with no args"""
        self.cmds.cmd_move("")
        self.io.tool_error.assert_called()

    def test_move_updates_coder_refs(self):
        """Move should update coder file references"""
        _create_novel_structure(self.tmpdir, acts=1, chapters=2, scenes=1)
        act_dir = _find_dir_by_number(_novel_dir(self.tmpdir), "Act", 1)
        ch1_dir = _find_dir_by_number(act_dir, "Chapter", 1)
        sc1_dir = _find_dir_by_number(ch1_dir, "Scene", 1)
        prose = os.path.join(sc1_dir, "PROSE.md")
        old_abs = os.path.abspath(prose)
        self.coder.abs_fnames = {old_abs}
        self.coder.abs_read_only_fnames = set()

        self.cmds.cmd_move("1 1 to 1 2")

        # Old exact path should be gone
        self.assertNotIn(old_abs, self.coder.abs_fnames)
        # A new path should be present containing "Chapter 2 -"
        self.assertTrue(any(
            "Chapter 2 -" in p for p in self.coder.abs_fnames
        ))


class TestMoveGetCommands(unittest.TestCase):
    """Verify /move is registered in get_commands()."""

    def test_move_in_commands(self):
        io = MagicMock()
        coder = MagicMock()
        coder.root = tempfile.mkdtemp()
        cmds = NovelCommands(io, coder, root=coder.root)
        commands = cmds.get_commands()
        self.assertIn("move", commands)


class TestMovePath(unittest.TestCase):
    """Test the /move command for non-narrative file moves."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_move_file_to_directory(self):
        """Move a file into a directory"""
        src = os.path.join(self.tmpdir, "test.md")
        tgt_dir = os.path.join(self.tmpdir, "dest")
        os.makedirs(tgt_dir)
        Path(src).write_text("hello", encoding="utf-8")

        self.cmds.cmd_move("test.md dest/")
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.isfile(os.path.join(tgt_dir, "test.md")))

    def test_move_nonexistent_file(self):
        """Should error when source file doesn't exist"""
        self.cmds.cmd_move("nonexist.md dest/")
        self.io.tool_error.assert_called()

    def test_move_file_already_exists(self):
        """Should error when target already exists"""
        src = os.path.join(self.tmpdir, "test.md")
        tgt = os.path.join(self.tmpdir, "dest.md")
        Path(src).write_text("hello", encoding="utf-8")
        Path(tgt).write_text("world", encoding="utf-8")

        self.cmds.cmd_move("test.md dest.md")
        self.io.tool_error.assert_called()


class TestAnalyzeStyle(unittest.TestCase):
    """Test the /analyze-style command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=1, chapters=1, scenes=2)
        _create_db_structure(self.tmpdir)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_no_args_does_nothing(self):
        """Should do nothing when called with no arguments."""
        self.cmds.cmd_analyze_style("")
        self.assertEqual(len(self.coder.abs_read_only_fnames), 0)
        self.io.tool_output.assert_not_called()

    def test_completions(self):
        """Should return level names as completions."""
        completions = self.cmds.completions_analyze_style()
        self.assertIn("act", completions)
        self.assertIn("chapter", completions)
        self.assertIn("scene", completions)

    def test_collect_style_source_scene(self):
        """Should collect PROSE.md for a single scene."""
        paths = self.cmds._collect_style_source_files("act 1 chapter 1 scene 1")
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("PROSE.md"))

    def test_collect_style_source_chapter(self):
        """Should collect all scene PROSE.md files in a chapter."""
        paths = self.cmds._collect_style_source_files("act 1 chapter 1")
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 2)
        for p in paths:
            self.assertTrue(p.endswith("PROSE.md"))

    def test_collect_style_source_act(self):
        """Should collect all scene PROSE.md files in an act."""
        paths = self.cmds._collect_style_source_files("act 1")
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 2)

    def test_collect_style_source_shorthand(self):
        """Should accept shorthand location syntax."""
        paths = self.cmds._collect_style_source_files("1 1 1")
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 1)

    def test_collect_style_source_file_path(self):
        """Should accept explicit file paths."""
        test_file = os.path.join(self.tmpdir, "sample.md")
        Path(test_file).write_text("Some prose content.", encoding="utf-8")
        paths = self.cmds._collect_style_source_files(test_file)
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0], os.path.abspath(test_file))

    def test_collect_style_source_invalid_location(self):
        """Should return None for invalid narrative location."""
        paths = self.cmds._collect_style_source_files("act 99")
        self.assertIsNone(paths)

    def test_collect_style_source_no_prose(self):
        """Should return empty list when scenes have no prose."""
        tmpdir = tempfile.mkdtemp()
        novel_root = os.path.join(tmpdir, NOVEL_DIR)
        act_dir = os.path.join(novel_root, make_titled_dir("Act", 1, "Empty"))
        ch_dir = os.path.join(act_dir, make_titled_dir("Chapter", 1, "Empty"))
        sc_dir = os.path.join(ch_dir, make_titled_dir("Scene", 1, "Empty"))
        os.makedirs(sc_dir, exist_ok=True)
        # Scene exists but has no PROSE.md
        Path(os.path.join(sc_dir, "SUMMARY.md")).write_text(
            "No prose yet.", encoding="utf-8"
        )

        cmds = NovelCommands(MagicMock(), None, root=tmpdir)
        paths = cmds._collect_style_source_files("1 1 1")
        self.assertIsNotNone(paths)
        self.assertEqual(len(paths), 0)

    def test_analyze_style_adds_prose_to_context(self):
        """Should add PROSE.md as read-only to the main coder's context."""
        self.cmds.cmd_analyze_style("act 1 chapter 1 scene 1")

        # The PROSE.md should now be in the coder's read-only set
        ro_rel = {
            os.path.relpath(p, self.tmpdir)
            for p in self.coder.abs_read_only_fnames
        }
        prose_files = [p for p in ro_rel if p.endswith("PROSE.md")]
        self.assertEqual(len(prose_files), 1)

    def test_analyze_style_adds_chapter_prose(self):
        """Should add all scene PROSE.md files for a chapter."""
        self.cmds.cmd_analyze_style("act 1 chapter 1")

        ro_rel = {
            os.path.relpath(p, self.tmpdir)
            for p in self.coder.abs_read_only_fnames
        }
        prose_files = [p for p in ro_rel if p.endswith("PROSE.md")]
        self.assertEqual(len(prose_files), 2)

    def test_analyze_style_skips_already_present(self):
        """Should not duplicate files already in context."""
        # Add a file manually first
        paths = self.cmds._collect_style_source_files("1 1 1")
        abs_path = os.path.abspath(paths[0])
        self.coder.abs_read_only_fnames.add(abs_path)

        self.cmds.cmd_analyze_style("1 1 1")

        # Should still only have 1 entry, not 2
        prose_in_ctx = [
            p for p in self.coder.abs_read_only_fnames
            if p.endswith("PROSE.md")
        ]
        self.assertEqual(len(prose_in_ctx), 1)

    def test_analyze_style_no_prose_errors(self):
        """Should error when no prose files are found."""
        tmpdir = tempfile.mkdtemp()
        novel_root = os.path.join(tmpdir, NOVEL_DIR)
        act_dir = os.path.join(novel_root, make_titled_dir("Act", 1, "Empty"))
        ch_dir = os.path.join(act_dir, make_titled_dir("Chapter", 1, "Empty"))
        sc_dir = os.path.join(ch_dir, make_titled_dir("Scene", 1, "Empty"))
        os.makedirs(sc_dir, exist_ok=True)
        Path(os.path.join(sc_dir, "SUMMARY.md")).write_text(
            "No prose.", encoding="utf-8"
        )

        io = MagicMock()
        coder = MagicMock()
        coder.root = tmpdir
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        cmds = NovelCommands(io, coder, root=tmpdir)
        cmds.cmd_analyze_style("1 1 1")
        io.tool_error.assert_called()
        msg = io.tool_error.call_args[0][0]
        self.assertIn("No prose", msg)

    def test_analyze_style_file_path(self):
        """Should add an explicit file path to context."""
        test_file = os.path.join(self.tmpdir, "sample.md")
        Path(test_file).write_text("Some prose.", encoding="utf-8")
        self.cmds.cmd_analyze_style(test_file)
        self.assertIn(os.path.abspath(test_file), self.coder.abs_read_only_fnames)


class TestGrepCommand(unittest.TestCase):
    """Tests for /grep command."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.io = MagicMock()
        self.coder = MagicMock()
        self.coder.root = self.tmpdir
        self.coder.abs_fnames = set()
        self.coder.abs_read_only_fnames = set()
        self.cmds = NovelCommands(self.io, self.coder, root=self.tmpdir)

    def test_grep_no_args(self):
        """Should show usage when called with no args."""
        self.cmds.cmd_grep("")
        self.io.tool_error.assert_called()

    def test_grep_basic_match(self):
        """Should find matches in novel files."""
        self.cmds.cmd_grep('"Summary of act 1"')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("SUMMARY.md", output)
        self.assertIn("Summary of act 1", output)

    def test_grep_no_match(self):
        """Should report no matches when pattern doesn't match."""
        self.cmds.cmd_grep("zzz_nonexistent_pattern_zzz")
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("No matches", output)

    def test_grep_case_insensitive(self):
        """Should support -i flag for case-insensitive search."""
        self.cmds.cmd_grep('-i "SUMMARY OF ACT 1"')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("SUMMARY.md", output)

    def test_grep_count_only(self):
        """Should support -c flag for count-only output."""
        self.cmds.cmd_grep('-c "Summary"')
        output = self.io.tool_output.call_args_list[0][0][0]
        # Count output has format "file: N"
        self.assertRegex(output, r"SUMMARY\.md:\s*\d+")

    def test_grep_files_only(self):
        """Should support -l flag for files-only output."""
        self.cmds.cmd_grep('-l "Summary"')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("SUMMARY.md", output)
        # Files-only output should NOT contain line numbers
        self.assertNotRegex(output, r":\d+:")

    def test_grep_narrative_location(self):
        """Should scope search to a narrative location."""
        self.cmds.cmd_grep('"Summary of act 1" act 1')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("Summary of act 1", output)

    def test_grep_narrative_location_shorthand(self):
        """Should accept shorthand numeric location."""
        self.cmds.cmd_grep('"scene 1" 1 1')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("scene 1", output.lower())

    def test_grep_file_path(self):
        """Should search a specific file path."""
        # Create a standalone test file
        test_file = os.path.join(self.tmpdir, "notes.md")
        Path(test_file).write_text("The quick brown fox.", encoding="utf-8")
        self.cmds.cmd_grep(f'"quick brown" {test_file}')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("quick brown", output)

    def test_grep_directory_path(self):
        """Should search all .md files in a directory path."""
        subdir = os.path.join(self.tmpdir, "scratch")
        os.makedirs(subdir)
        Path(os.path.join(subdir, "a.md")).write_text("hello world", encoding="utf-8")
        Path(os.path.join(subdir, "b.txt")).write_text("hello world", encoding="utf-8")
        self.cmds.cmd_grep(f"hello {subdir}")
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertIn("a.md", output)
        # .txt files should NOT be searched
        self.assertNotIn("b.txt", output)

    def test_grep_invalid_regex(self):
        """Should report invalid regex pattern."""
        self.cmds.cmd_grep("[invalid")
        self.io.tool_error.assert_called()
        msg = self.io.tool_error.call_args[0][0]
        self.assertIn("Invalid regex", msg)

    def test_grep_defaults_to_novel_dir(self):
        """With no location, should search the entire novel directory."""
        self.cmds.cmd_grep("Summary")
        output = self.io.tool_output.call_args_list[0][0][0]
        # Should find matches across multiple acts
        self.assertIn("SUMMARY.md", output)

    def test_grep_combined_flags(self):
        """Should support combined flags like -ic."""
        self.cmds.cmd_grep('-ic "SUMMARY"')
        output = self.io.tool_output.call_args_list[0][0][0]
        self.assertRegex(output, r"SUMMARY\.md:\s*\d+")

    def test_grep_registered(self):
        """The grep command should appear in get_commands()."""
        commands = self.cmds.get_commands()
        self.assertIn("grep", commands)


if __name__ == "__main__":
    unittest.main()
