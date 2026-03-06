import os
import tempfile
import unittest
from pathlib import Path

from composez_core.db import CORE_CATEGORY, Db, DbEntry, DEFAULT_CATEGORIES


class TestDbEntry(unittest.TestCase):
    """Test the DbEntry data class."""

    def test_basic_entry(self):
        entry = DbEntry("characters", "sarah", "/tmp/sarah.md")
        self.assertEqual(entry.category, "characters")
        self.assertEqual(entry.name, "sarah")
        self.assertEqual(entry.path, "/tmp/sarah.md")

    def test_repr(self):
        entry = DbEntry("characters", "sarah", "/tmp/sarah.md")
        self.assertIn("characters", repr(entry))
        self.assertIn("sarah", repr(entry))

    def test_content_property_reads_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("Sarah is the protagonist.")
            f.flush()
            entry = DbEntry("characters", "sarah", f.name)
            self.assertEqual(entry.content, "Sarah is the protagonist.")
        os.unlink(f.name)

    def test_content_property_caches(self):
        entry = DbEntry("characters", "sarah", "/tmp/nonexistent.md")
        # Reading nonexistent file returns empty string
        content1 = entry.content
        self.assertEqual(content1, "")
        # Second access uses cache
        content2 = entry.content
        self.assertEqual(content1, content2)

    def test_content_with_preset(self):
        entry = DbEntry("characters", "sarah", "/tmp/x.md", content="preset")
        self.assertEqual(entry.content, "preset")

    def test_content_non_utf8_returns_empty(self):
        """Binary/non-UTF-8 files should return empty string, not raise."""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            # Write bytes that are invalid UTF-8 (UTF-16 BOM)
            f.write(b"\xff\xfe\x00\x00")
            f.flush()
            entry = DbEntry("characters", "binary", f.name)
            self.assertEqual(entry.content, "")
        os.unlink(f.name)


def _create_db_structure(root):
    """Helper to create a test db structure."""
    db_dir = os.path.join(root, "db")
    os.makedirs(os.path.join(db_dir, "characters"))
    os.makedirs(os.path.join(db_dir, "locations"))

    Path(os.path.join(db_dir, "characters", "sarah.md")).write_text(
        "# Sarah\nAge: 32\nRole: Protagonist", encoding="utf-8"
    )
    Path(os.path.join(db_dir, "characters", "tom.md")).write_text(
        "# Tom\nAge: 35\nRole: Antagonist", encoding="utf-8"
    )
    Path(os.path.join(db_dir, "locations", "apartment.md")).write_text(
        "# The Apartment\nA small studio in Brooklyn.", encoding="utf-8"
    )
    # File directly in db/
    Path(os.path.join(db_dir, "style_guide.md")).write_text(
        "Write in third person, past tense.", encoding="utf-8"
    )


class TestDb(unittest.TestCase):
    """Test the Db class."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _create_db_structure(self.tmpdir)
        self.db = Db(self.tmpdir)

    def test_db_path(self):
        self.assertEqual(self.db.db_path, os.path.join(self.tmpdir, "db"))

    def test_get_entries(self):
        entries = self.db.get_entries()
        self.assertEqual(len(entries), 4)  # sarah, tom, apartment, style_guide

    def test_get_entries_caching(self):
        entries1 = self.db.get_entries()
        entries2 = self.db.get_entries()
        self.assertEqual(len(entries1), len(entries2))

    def test_get_entries_force_refresh(self):
        entries1 = self.db.get_entries()
        entries2 = self.db.get_entries(force_refresh=True)
        self.assertEqual(len(entries1), len(entries2))

    def test_get_entry_by_name(self):
        entry = self.db.get_entry("sarah")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "sarah")
        self.assertEqual(entry.category, "characters")

    def test_get_entry_case_insensitive(self):
        entry = self.db.get_entry("Sarah")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "sarah")

    def test_get_entry_with_category(self):
        entry = self.db.get_entry("sarah", category="characters")
        self.assertIsNotNone(entry)
        entry_wrong_cat = self.db.get_entry("sarah", category="locations")
        self.assertIsNone(entry_wrong_cat)

    def test_get_entry_missing(self):
        entry = self.db.get_entry("nonexistent")
        self.assertIsNone(entry)

    def test_get_entries_by_category(self):
        chars = self.db.get_entries_by_category("characters")
        self.assertEqual(len(chars), 2)
        names = {e.name for e in chars}
        self.assertEqual(names, {"sarah", "tom"})

    def test_get_entries_by_category_case_insensitive(self):
        chars = self.db.get_entries_by_category("Characters")
        self.assertEqual(len(chars), 2)

    def test_get_entries_by_category_empty(self):
        entries = self.db.get_entries_by_category("weapons")
        self.assertEqual(len(entries), 0)

    def test_scan_skips_hidden_files(self):
        """Hidden files like .DS_Store should be ignored by _scan()."""
        db_dir = os.path.join(self.tmpdir, "db")
        # Add hidden files at both levels
        Path(os.path.join(db_dir, ".DS_Store")).write_bytes(b"\x00\x00\x00\x01")
        Path(os.path.join(db_dir, "characters", ".hidden")).write_bytes(b"\xff\xfe")
        self.db._entries = None  # invalidate cache
        entries = self.db.get_entries()
        names = [e.name for e in entries]
        self.assertNotIn(".DS_Store", names)
        self.assertNotIn(".hidden", names)
        # Original entries should still be present
        self.assertEqual(len(entries), 4)

    def test_get_categories(self):
        cats = self.db.get_categories()
        self.assertIn("characters", cats)
        self.assertIn("locations", cats)
        self.assertIn("general", cats)  # style_guide.md is in root db/

    def test_files_in_root_db_are_general(self):
        entry = self.db.get_entry("style_guide")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.category, "general")

    def test_entry_content(self):
        entry = self.db.get_entry("sarah")
        self.assertIn("Protagonist", entry.content)

    def test_get_context_files(self):
        paths = self.db.get_context_files()
        self.assertEqual(len(paths), 4)
        for path in paths:
            self.assertTrue(os.path.isfile(path))

    def test_get_context_files_filtered_by_name(self):
        paths = self.db.get_context_files(names=["sarah"])
        self.assertEqual(len(paths), 1)

    def test_get_context_files_filtered_by_category(self):
        paths = self.db.get_context_files(categories=["locations"])
        self.assertEqual(len(paths), 1)

    def test_get_all_context_paths(self):
        paths = self.db.get_all_context_paths()
        self.assertEqual(len(paths), 4)

    def test_format_summary(self):
        summary = self.db.format_summary()
        self.assertIn("characters", summary)
        self.assertIn("sarah", summary)
        self.assertIn("tom", summary)
        self.assertIn("locations", summary)
        self.assertIn("apartment", summary)


class TestDbCreate(unittest.TestCase):
    """Test db entry creation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Db(self.tmpdir)

    def test_create_entry(self):
        entry = self.db.create_entry("characters", "alice", "A new character.")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "alice")
        self.assertEqual(entry.category, "characters")
        self.assertTrue(os.path.isfile(entry.path))

    def test_create_entry_adds_md_extension(self):
        entry = self.db.create_entry("characters", "bob")
        self.assertTrue(entry.path.endswith(".md"))

    def test_create_entry_preserves_extension(self):
        entry = self.db.create_entry("characters", "carol.txt")
        self.assertTrue(entry.path.endswith(".txt"))

    def test_create_entry_content(self):
        entry = self.db.create_entry("items", "sword", "A magic sword.")
        content = Path(entry.path).read_text(encoding="utf-8")
        self.assertEqual(content, "A magic sword.")

    def test_create_entry_invalidates_cache(self):
        self.db.get_entries()  # prime the cache
        self.db.create_entry("characters", "dave")
        entries = self.db.get_entries()
        names = [e.name for e in entries]
        self.assertIn("dave", names)


class TestDbInit(unittest.TestCase):
    """Test db initialization."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Db(self.tmpdir)

    def test_init_db(self):
        created = self.db.init_db()
        self.assertEqual(set(created), set(DEFAULT_CATEGORIES))

        for cat in DEFAULT_CATEGORIES:
            cat_dir = os.path.join(self.db.db_path, cat)
            self.assertTrue(os.path.isdir(cat_dir))

    def test_init_db_custom_categories(self):
        created = self.db.init_db(categories=["magic", "creatures"])
        self.assertEqual(set(created), {"magic", "creatures"})

    def test_init_db_idempotent(self):
        self.db.init_db()
        self.db.init_db()  # should not error


class TestDbEmpty(unittest.TestCase):
    """Test db with no db/ directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Db(self.tmpdir)

    def test_empty_entries(self):
        entries = self.db.get_entries()
        self.assertEqual(entries, [])

    def test_empty_summary(self):
        summary = self.db.format_summary()
        self.assertIn("No db entries found", summary)

    def test_get_entry_returns_none(self):
        self.assertIsNone(self.db.get_entry("anything"))


class TestCoreCategory(unittest.TestCase):
    """Test the core category and its special behaviour."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Db(self.tmpdir)

    def test_core_in_default_categories(self):
        self.assertIn(CORE_CATEGORY, DEFAULT_CATEGORIES)

    def test_init_db_creates_core_dir(self):
        self.db.init_db()
        core_dir = os.path.join(self.db.db_path, CORE_CATEGORY)
        self.assertTrue(os.path.isdir(core_dir))

    def test_init_db_seeds_style_md(self):
        self.db.init_db()
        style_path = os.path.join(self.db.db_path, CORE_CATEGORY, "style.md")
        self.assertTrue(os.path.isfile(style_path))
        content = Path(style_path).read_text(encoding="utf-8")
        self.assertIn("Style Guide", content)

    def test_init_core_defaults_idempotent(self):
        """Calling init_core_defaults twice should not overwrite an edited style.md."""
        self.db.init_db()
        style_path = os.path.join(self.db.db_path, CORE_CATEGORY, "style.md")
        Path(style_path).write_text("Custom style.", encoding="utf-8")

        self.db.init_core_defaults()

        content = Path(style_path).read_text(encoding="utf-8")
        self.assertEqual(content, "Custom style.")

    def test_get_core_context_paths(self):
        self.db.init_db()
        paths = self.db.get_core_context_paths()
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("style.md"))

    def test_get_core_context_paths_multiple_files(self):
        self.db.init_db()
        # Add another core file
        self.db.create_entry(CORE_CATEGORY, "voice", "Voice notes.")
        paths = self.db.get_core_context_paths()
        self.assertEqual(len(paths), 2)

    def test_get_core_context_paths_empty(self):
        """No core files if db/ doesn't exist."""
        paths = self.db.get_core_context_paths()
        self.assertEqual(len(paths), 0)


if __name__ == "__main__":
    unittest.main()
