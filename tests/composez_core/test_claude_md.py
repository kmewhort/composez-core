import os
import tempfile
import unittest
from pathlib import Path

from composez_core.claude_md import (
    CLAUDE_MD_FILE,
    generate_claude_md,
    init_claude_md,
)
from composez_core.config import DEFAULT_LEVELS, save_config


class TestGenerateClaudeMd(unittest.TestCase):
    """Test generate_claude_md() with various level configurations."""

    def test_default_levels(self):
        with tempfile.TemporaryDirectory() as root:
            content = generate_claude_md(root)

            # Should mention all default levels
            for level in DEFAULT_LEVELS:
                self.assertIn(level, content)

            # Should have the expected sections
            self.assertIn("## Project Structure", content)
            self.assertIn("## File Rules", content)
            self.assertIn("## Reference Database", content)
            self.assertIn("## Working on Summaries", content)
            self.assertIn("## Working on Prose", content)
            self.assertIn("## Analyzing Prose", content)
            self.assertIn("## Workflow", content)
            self.assertIn("## Narrative Hierarchy", content)

    def test_custom_levels(self):
        with tempfile.TemporaryDirectory() as root:
            save_config(root, {"levels": ["Part", "Chapter", "Section"]})
            content = generate_claude_md(root)

            self.assertIn("Part", content)
            self.assertIn("Chapter", content)
            self.assertIn("Section", content)
            # Leaf level should appear in the summary/prose sections
            self.assertIn("section", content.lower())

    def test_two_levels(self):
        with tempfile.TemporaryDirectory() as root:
            save_config(root, {"levels": ["Book", "Episode"]})
            content = generate_claude_md(root)

            self.assertIn("Book", content)
            self.assertIn("Episode", content)
            self.assertIn("Book > Episode", content)

    def test_reuses_novel_prompts_content(self):
        """CLAUDE.md content should be derived from NovelPrompts, not hardcoded."""
        from composez_core.novel_prompts import NovelPrompts

        prompts = NovelPrompts()
        with tempfile.TemporaryDirectory() as root:
            content = generate_claude_md(root)

            # lazy_prompt content should appear
            self.assertIn("NEVER leave placeholder text", content)

            # overeager_prompt content should appear
            self.assertIn("Preserve the author's voice", content)

            # read_only_files_prefix content should appear
            self.assertIn("READ ONLY", content)

    def test_reuses_query_prompts_analysis(self):
        """Analysis section should include criteria from NovelQueryPrompts."""
        with tempfile.TemporaryDirectory() as root:
            content = generate_claude_md(root)

            self.assertIn("Character development", content)
            self.assertIn("Plot structure and pacing", content)
            self.assertIn("Show vs. tell", content)

    def test_file_rules_from_prompts(self):
        """File rules should mirror NovelPrompts.main_system constraints."""
        with tempfile.TemporaryDirectory() as root:
            content = generate_claude_md(root)

            self.assertIn("SUMMARY.md and PROSE.md only", content)
            self.assertIn("Do NOT create files outside", content)
            self.assertIn("top-level headings", content)


class TestInitClaudeMd(unittest.TestCase):
    """Test init_claude_md() file creation."""

    def test_creates_file(self):
        with tempfile.TemporaryDirectory() as root:
            result = init_claude_md(root)

            self.assertIsNotNone(result)
            self.assertTrue(os.path.isfile(result))

            content = Path(result).read_text(encoding="utf-8")
            self.assertIn("# CLAUDE.md", content)

    def test_skips_existing(self):
        with tempfile.TemporaryDirectory() as root:
            # Create an existing CLAUDE.md with custom content
            path = os.path.join(root, CLAUDE_MD_FILE)
            Path(path).write_text("custom content", encoding="utf-8")

            result = init_claude_md(root)
            self.assertIsNone(result)

            # Original content should be preserved
            content = Path(path).read_text(encoding="utf-8")
            self.assertEqual(content, "custom content")

    def test_file_location(self):
        with tempfile.TemporaryDirectory() as root:
            result = init_claude_md(root)

            expected = os.path.join(root, CLAUDE_MD_FILE)
            self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
