import os
import tempfile
import unittest
from pathlib import Path

from composez_core.narrative_map import (
    NarrativeMap,
    NarrativeNode,
    _NUM_TITLE_RE,
    make_titled_dir,
)


class TestNumTitleRegex(unittest.TestCase):
    """Test the directory-name matching regex."""

    def test_matches_number_with_title(self):
        for name in (
            "1 - The Beginning",
            "2 - Untitled",
            "10 - A Long Title Here",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    _NUM_TITLE_RE.match(name), f"Should match: {name}"
                )

    def test_matches_bare_number(self):
        for name in ("1", "2", "10", "99"):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    _NUM_TITLE_RE.match(name), f"Should match: {name}"
                )

    def test_rejects_non_matching(self):
        for name in ("act_1", "chapter_1", "abc", "scene_1_-_untitled", ""):
            with self.subTest(name=name):
                self.assertIsNone(
                    _NUM_TITLE_RE.match(name), f"Should not match: {name}"
                )

    def test_captures_title(self):
        m = _NUM_TITLE_RE.match("1 - The Beginning")
        self.assertEqual(m.group(1), "1")
        self.assertEqual(m.group(2), "The Beginning")

    def test_captures_no_title(self):
        m = _NUM_TITLE_RE.match("3")
        self.assertEqual(m.group(1), "3")
        self.assertIsNone(m.group(2))


class TestMakeTitledDir(unittest.TestCase):
    """Test the make_titled_dir helper."""

    def test_with_title(self):
        self.assertEqual(
            make_titled_dir("Scene", 2, "The Lake Encounter"),
            "Scene 2 - The Lake Encounter",
        )

    def test_without_title(self):
        self.assertEqual(make_titled_dir("Act", 1), "Act 1 - Untitled")

    def test_none_title(self):
        self.assertEqual(make_titled_dir("Chapter", 3, None), "Chapter 3 - Untitled")

    def test_preserves_spaces(self):
        self.assertEqual(
            make_titled_dir("Act", 1, "The Rising Action"),
            "Act 1 - The Rising Action",
        )


class TestNarrativeNode(unittest.TestCase):
    """Test the NarrativeNode data class."""

    def test_basic_node(self):
        node = NarrativeNode("Act", 1, "/tmp/1 - The Beginning", title="The Beginning")
        self.assertEqual(node.kind, "Act")
        self.assertEqual(node.number, 1)
        self.assertEqual(node.title, "The Beginning")
        self.assertEqual(node.word_count, 0)
        self.assertEqual(node.children, [])

    def test_total_word_count_leaf(self):
        node = NarrativeNode("scene", 1, "/tmp/1", word_count=500)
        self.assertEqual(node.total_word_count(), 500)

    def test_total_word_count_recursive(self):
        act = NarrativeNode("act", 1, "/tmp/1", word_count=100)
        ch = NarrativeNode("chapter", 1, "/tmp/ch", word_count=50)
        sc1 = NarrativeNode("scene", 1, "/tmp/sc1", word_count=200)
        sc2 = NarrativeNode("scene", 2, "/tmp/sc2", word_count=300)

        ch.children = [sc1, sc2]
        act.children = [ch]

        # act(100) + ch(50) + sc1(200) + sc2(300) = 650
        self.assertEqual(act.total_word_count(), 650)

    def test_repr(self):
        node = NarrativeNode("scene", 3, "/tmp/3", word_count=42)
        self.assertIn("scene", repr(node))
        self.assertIn("3", repr(node))


def _create_novel_structure(root, acts=2, chapters=2, scenes=2):
    """Helper to create a test novel directory structure."""
    for a in range(1, acts + 1):
        act_dir_name = make_titled_dir("Act", a, f"Act {a} Title")
        act_dir = os.path.join(root, act_dir_name)
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
                # Create prose with a known word count
                words = " ".join([f"word{i}" for i in range(50 * s)])
                Path(os.path.join(sc_dir, "PROSE.md")).write_text(
                    words, encoding="utf-8"
                )
                Path(os.path.join(sc_dir, "SUMMARY.md")).write_text(
                    f"Scene {s} Title\nBrief summary of scene {s}.",
                    encoding="utf-8",
                )


class TestNarrativeMap(unittest.TestCase):
    """Test the NarrativeMap class."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_novel_structure(self.tmpdir, acts=2, chapters=2, scenes=2)
        self.nmap = NarrativeMap(self.tmpdir)

    def test_get_tree_returns_acts(self):
        tree = self.nmap.get_tree()
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0].kind, "Act")
        self.assertEqual(tree[0].number, 1)
        self.assertEqual(tree[1].number, 2)

    def test_tree_has_chapters(self):
        tree = self.nmap.get_tree()
        act1 = tree[0]
        self.assertEqual(len(act1.children), 2)
        self.assertEqual(act1.children[0].kind, "Chapter")
        self.assertEqual(act1.children[0].number, 1)

    def test_tree_has_scenes(self):
        tree = self.nmap.get_tree()
        ch1 = tree[0].children[0]
        self.assertEqual(len(ch1.children), 2)
        self.assertEqual(ch1.children[0].kind, "Scene")

    def test_summaries_loaded(self):
        tree = self.nmap.get_tree()
        self.assertEqual(tree[0].title, "Act 1 Title")
        self.assertIn("Summary of act 1", tree[0].summary)

    def test_word_counts(self):
        tree = self.nmap.get_tree()
        sc1 = tree[0].children[0].children[0]
        sc2 = tree[0].children[0].children[1]
        # scene_1 has 50 words, scene_2 has 100 words
        self.assertEqual(sc1.word_count, 50)
        self.assertEqual(sc2.word_count, 100)

    def test_caching(self):
        tree1 = self.nmap.get_tree()
        tree2 = self.nmap.get_tree()
        self.assertIs(tree1, tree2)  # Same object (cached)

    def test_refresh_clears_cache(self):
        tree1 = self.nmap.get_tree()
        tree2 = self.nmap.refresh()
        self.assertIsNot(tree1, tree2)  # Different object

    def test_get_repo_map_returns_string(self):
        result = self.nmap.get_repo_map()
        self.assertIsInstance(result, str)
        self.assertIn("Narrative Structure", result)
        self.assertIn("Act 1", result)

    def test_get_repo_map_force_refresh(self):
        result1 = self.nmap.get_repo_map()
        result2 = self.nmap.get_repo_map(force_refresh=True)
        self.assertIsInstance(result2, str)
        self.assertEqual(result1, result2)

    def test_render_marks_editing_files(self):
        tree = self.nmap.get_tree()
        sc1_path = os.path.join(
            tree[0].children[0].children[0].path, "PROSE.md"
        )
        result = self.nmap.render(tree, chat_files={sc1_path})
        self.assertIn("**(editing)**", result)

    def test_render_empty_tree(self):
        result = self.nmap.render([])
        self.assertIsNone(result)

    def test_get_outline(self):
        outline = self.nmap.get_outline()
        self.assertIn("Act 1", outline)
        self.assertIn("Chapter 1", outline)
        self.assertIn("Scene 1", outline)

    def test_get_outline_no_summaries(self):
        outline = self.nmap.get_outline(include_summaries=False)
        self.assertIn("Act 1", outline)
        self.assertNotIn("Summary of", outline)

    def test_get_word_counts(self):
        counts = self.nmap.get_word_counts()
        self.assertGreater(counts["total"], 0)
        self.assertEqual(len(counts["acts"]), 2)
        self.assertEqual(len(counts["acts"][0]["chapters"]), 2)

    def test_find_node_act(self):
        node = self.nmap.find_node(1)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, "Act")
        self.assertEqual(node.number, 1)

    def test_find_node_chapter(self):
        node = self.nmap.find_node(1, 2)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, "Chapter")
        self.assertEqual(node.number, 2)

    def test_find_node_scene(self):
        node = self.nmap.find_node(2, 1, 1)
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, "Scene")

    def test_find_node_missing(self):
        node = self.nmap.find_node(99)
        self.assertIsNone(node)

    def test_find_node_missing_scene(self):
        node = self.nmap.find_node(1, 1, 99)
        self.assertIsNone(node)


class TestNarrativeMapEmpty(unittest.TestCase):
    """Test NarrativeMap with no novel structure."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.nmap = NarrativeMap(self.tmpdir)

    def test_empty_tree(self):
        tree = self.nmap.get_tree()
        self.assertEqual(tree, [])

    def test_empty_repo_map(self):
        result = self.nmap.get_repo_map()
        self.assertIsNone(result)

    def test_empty_outline(self):
        outline = self.nmap.get_outline()
        self.assertIn("No narrative structure found", outline)

    def test_empty_word_counts(self):
        counts = self.nmap.get_word_counts()
        self.assertEqual(counts["total"], 0)


class TestNarrativeMapPartial(unittest.TestCase):
    """Test NarrativeMap with partial structure (missing summaries/content)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create structure without summaries
        act_dir = os.path.join(self.tmpdir, "Act 1 - Untitled")
        os.makedirs(act_dir)
        ch_dir = os.path.join(act_dir, "Chapter 1 - Untitled")
        os.makedirs(ch_dir)
        sc_dir = os.path.join(ch_dir, "Scene 1 - Untitled")
        os.makedirs(sc_dir)
        # Only create PROSE.md for scene, no SUMMARY.md anywhere
        Path(os.path.join(sc_dir, "PROSE.md")).write_text(
            "Hello world this is content.", encoding="utf-8"
        )
        self.nmap = NarrativeMap(self.tmpdir)

    def test_tree_without_summaries(self):
        tree = self.nmap.get_tree()
        self.assertEqual(len(tree), 1)
        act = tree[0]
        self.assertEqual(act.title, "Untitled")  # title from directory name
        self.assertIsNone(act.summary)

    def test_word_count_without_content(self):
        tree = self.nmap.get_tree()
        act = tree[0]
        self.assertEqual(act.word_count, 0)  # act has no CONTENT.md or PROSE.md

    def test_scene_has_words(self):
        tree = self.nmap.get_tree()
        scene = tree[0].children[0].children[0]
        self.assertEqual(scene.word_count, 5)  # "Hello world this is content."

    def test_outline_titles_from_dirname(self):
        outline = self.nmap.get_outline()
        self.assertIn("Act 1: Untitled", outline)


class TestNarrativeMapBareNumbers(unittest.TestCase):
    """Test that bare-number directory names (no title) are recognized."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_bare_number_naming(self):
        """Test Act 1/Chapter 1/Scene 1 naming."""
        sc_dir = os.path.join(
            self.tmpdir, "Act 1", "Chapter 1", "Scene 1"
        )
        os.makedirs(sc_dir)
        Path(os.path.join(sc_dir, "PROSE.md")).write_text(
            "Test content here.", encoding="utf-8"
        )

        nmap = NarrativeMap(self.tmpdir)
        tree = nmap.get_tree()
        self.assertEqual(len(tree), 1)
        self.assertEqual(len(tree[0].children), 1)
        self.assertEqual(len(tree[0].children[0].children), 1)

    def test_titled_naming(self):
        """Test Act 1 - The Beginning/Chapter 1 - Arrival/Scene 1 - Lake naming."""
        sc_dir = os.path.join(
            self.tmpdir, "Act 1 - The Beginning",
            "Chapter 1 - Arrival",
            "Scene 1 - The Lake Encounter",
        )
        os.makedirs(sc_dir)
        Path(os.path.join(sc_dir, "PROSE.md")).write_text(
            "Titled content here.", encoding="utf-8"
        )

        nmap = NarrativeMap(self.tmpdir)
        tree = nmap.get_tree()
        self.assertEqual(len(tree), 1)
        self.assertEqual(len(tree[0].children), 1)
        self.assertEqual(len(tree[0].children[0].children), 1)


if __name__ == "__main__":
    unittest.main()
