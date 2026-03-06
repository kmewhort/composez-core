"""Tests for the file-tree sorting in the Novel UI server."""

import unittest

try:
    from composez_core.server.app import _entry_sort_key

    HAS_DEPS = True
except (ImportError, ModuleNotFoundError):
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "Missing server dependencies")
class TestEntrySortKey(unittest.TestCase):
    """Verify _entry_sort_key puts SUMMARY.md first, PROSE.md second,
    then everything else alphabetically (case-insensitive)."""

    def _make(self, name, typ="file"):
        return {"name": name, "type": typ}

    def test_summary_before_prose(self):
        entries = [self._make("PROSE.md"), self._make("SUMMARY.md")]
        result = sorted(entries, key=_entry_sort_key)
        self.assertEqual([e["name"] for e in result], ["SUMMARY.md", "PROSE.md"])

    def test_special_files_before_others(self):
        entries = [
            self._make("notes.txt"),
            self._make("PROSE.md"),
            self._make("SUMMARY.md"),
            self._make("alpha.md"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(names, ["SUMMARY.md", "PROSE.md", "alpha.md", "notes.txt"])

    def test_directories_sort_alphabetically(self):
        entries = [
            self._make("3 - Gamma", "directory"),
            self._make("1 - Alpha", "directory"),
            self._make("2 - Beta", "directory"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(names, ["1 - Alpha", "2 - Beta", "3 - Gamma"])

    def test_natural_number_sort(self):
        """Directories with leading numbers sort numerically, not lexicographically."""
        entries = [
            self._make("10 - Ten", "directory"),
            self._make("2 - Two", "directory"),
            self._make("1 - One", "directory"),
            self._make("11 - Eleven", "directory"),
            self._make("3 - Three", "directory"),
            self._make("20 - Twenty", "directory"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(
            names,
            ["1 - One", "2 - Two", "3 - Three", "10 - Ten", "11 - Eleven", "20 - Twenty"],
        )

    def test_special_files_before_directories(self):
        entries = [
            self._make("1 - First Scene", "directory"),
            self._make("PROSE.md"),
            self._make("SUMMARY.md"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(names, ["SUMMARY.md", "PROSE.md", "1 - First Scene"])

    def test_case_insensitive_alphabetical(self):
        entries = [
            self._make("Zebra.md"),
            self._make("alpha.md"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(names, ["alpha.md", "Zebra.md"])

    def test_mixed_files_and_dirs(self):
        """Full realistic scene directory listing."""
        entries = [
            self._make("notes.txt"),
            self._make("PROSE.md"),
            self._make("SUMMARY.md"),
            self._make("drafts", "directory"),
        ]
        result = sorted(entries, key=_entry_sort_key)
        names = [e["name"] for e in result]
        self.assertEqual(names, ["SUMMARY.md", "PROSE.md", "drafts", "notes.txt"])
