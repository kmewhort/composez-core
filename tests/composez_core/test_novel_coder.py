import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import git

    HAS_GIT = True
except ImportError:
    HAS_GIT = False

from composez_core.novel_coder import (
    NovelPromptOverlay,
    _ensure_metadata,
    _install_file_sorting,
    _novel_file_sort_key,
    activate_novel_query_mode,
    activate_novel_mode,
    collapse_paths,
    load_core_context,
)
from composez_core.novel_prompts import NovelComposePrompts, NovelPrompts, NovelQueryPrompts


class TestNovelPrompts(unittest.TestCase):
    """Test the NovelPrompts content."""

    def setUp(self):
        self.prompts = NovelPrompts()

    def test_main_system_is_prose_oriented(self):
        self.assertIn("fiction writer", self.prompts.main_system)
        self.assertNotIn("software developer", self.prompts.main_system)

    def test_main_system_has_narrative_structure(self):
        system = self.prompts.main_system
        self.assertIn("PROSE.md", system)
        self.assertIn("SUMMARY.md", system)
        self.assertIn("narrative levels", system)

    def test_system_reminder_has_file_format(self):
        self.assertIn("PROSE.md", self.prompts.system_reminder)
        self.assertIn("{fence[0]}", self.prompts.system_reminder)

    def test_example_messages_are_prose(self):
        self.assertTrue(len(self.prompts.example_messages) > 0)
        user_msg = self.prompts.example_messages[0]
        self.assertEqual(user_msg["role"], "user")
        # Should be about prose, not code
        assistant_msg = self.prompts.example_messages[1]
        self.assertIn("scene", assistant_msg["content"].lower())

    def test_read_only_prefix_mentions_db(self):
        self.assertIn("db", self.prompts.read_only_files_prefix.lower())

    def test_repo_content_prefix_is_disabled(self):
        self.assertIsNone(self.prompts.repo_content_prefix)

    def test_lazy_prompt_is_prose_oriented(self):
        self.assertIn("scene", self.prompts.lazy_prompt.lower())

    def test_overeager_prompt_preserves_voice(self):
        self.assertIn("voice", self.prompts.overeager_prompt.lower())

    def test_main_system_has_file_constraints(self):
        system = self.prompts.main_system
        self.assertIn("any .md files", system)
        self.assertIn("PROSE.md", system)
        self.assertIn("SUMMARY.md and PROSE.md only", system)


class TestNovelQueryPrompts(unittest.TestCase):
    """Test the NovelQueryPrompts content."""

    def setUp(self):
        self.prompts = NovelQueryPrompts()

    def test_main_system_is_editorial(self):
        self.assertIn("editor", self.prompts.main_system.lower())
        self.assertIn("literary", self.prompts.main_system.lower())

    def test_mentions_analysis_topics(self):
        system = self.prompts.main_system
        self.assertIn("Character development", system)
        self.assertIn("Plot structure", system)
        self.assertIn("Dialogue", system)


class TestNovelPromptOverlay(unittest.TestCase):
    """Test that NovelPromptOverlay correctly delegates attributes."""

    def setUp(self):
        """Create a mock 'original' prompts object and wrap it."""
        self.original = MagicMock()
        self.original.system_reminder = "Use diff format: ..."
        self.original.example_messages = [{"role": "user", "content": "code example"}]
        self.original.shell_cmd_prompt = "You can run shell commands."
        self.overlay = NovelPromptOverlay(self.original)

    def test_main_system_comes_from_novel(self):
        """Content-specific prompts should come from NovelPrompts."""
        self.assertIn("fiction writer", self.overlay.main_system)

    def test_system_reminder_comes_from_original(self):
        """Format-specific system_reminder should come from the original coder."""
        self.assertEqual(self.overlay.system_reminder, "Use diff format: ...")

    def test_example_messages_come_from_original(self):
        """Format-specific example_messages should come from the original coder."""
        self.assertEqual(
            self.overlay.example_messages,
            [{"role": "user", "content": "code example"}],
        )

    def test_lazy_prompt_comes_from_novel(self):
        self.assertIn("scene", self.overlay.lazy_prompt.lower())

    def test_overeager_prompt_comes_from_novel(self):
        self.assertIn("voice", self.overlay.overeager_prompt.lower())

    def test_files_content_prefix_comes_from_novel(self):
        # Verify we get the NovelPrompts version (a string), not the MagicMock original
        self.assertIsInstance(self.overlay.files_content_prefix, str)
        self.assertIn("added these files", self.overlay.files_content_prefix.lower())

    def test_repo_content_prefix_is_disabled(self):
        self.assertIsNone(self.overlay.repo_content_prefix)

    def test_unknown_attr_falls_back_to_original(self):
        """Attributes not in NovelPrompts should fall back to the original."""
        self.assertEqual(self.overlay.shell_cmd_prompt, "You can run shell commands.")

    def test_file_constraints_in_main_system(self):
        self.assertIn("any .md files", self.overlay.main_system)
        self.assertIn("SUMMARY.md and PROSE.md only", self.overlay.main_system)


class TestNovelComposePrompts(unittest.TestCase):
    """Test that NovelComposePrompts provides planning-oriented prompts."""

    def setUp(self):
        self.prompts = NovelComposePrompts()

    def test_main_system_has_planning_language(self):
        """Compose main_system should tell the model to describe changes, not output full content."""
        self.assertIn("creative director", self.prompts.main_system)
        self.assertIn("writing assistant", self.prompts.main_system)
        self.assertIn("Describe how to modify", self.prompts.main_system)

    def test_main_system_forbids_full_reproduction(self):
        """Compose main_system should explicitly forbid reproducing full files."""
        self.assertIn("DO NOT reproduce the entire scene", self.prompts.main_system)

    def test_main_system_has_novel_structure(self):
        """Compose main_system should include novel directory structure context."""
        self.assertIn("PROSE.md", self.prompts.main_system)
        self.assertIn("SUMMARY.md", self.prompts.main_system)
        self.assertIn("read-only reference db", self.prompts.main_system)

    def test_lazy_prompt_is_planning_oriented(self):
        """Lazy prompt should encourage thoroughness but not full content reproduction."""
        self.assertIn("describe", self.prompts.lazy_prompt.lower())
        self.assertNotIn("scene continues", self.prompts.lazy_prompt.lower())

    def test_inherits_novel_file_attributes(self):
        """Should inherit file presentation attributes from NovelPrompts."""
        self.assertIn("manuscript", self.prompts.files_content_assistant_reply.lower())
        self.assertIsNone(self.prompts.repo_content_prefix)

    def test_inherits_overeager_prompt(self):
        """Should inherit overeager_prompt from NovelPrompts (scope discipline applies to planning)."""
        self.assertIn("voice", self.prompts.overeager_prompt.lower())


class TestNovelComposeOverlay(unittest.TestCase):
    """Test NovelPromptOverlay with NovelComposePrompts for compose mode."""

    def setUp(self):
        from aider.coders.architect_prompts import ArchitectPrompts

        self.original = ArchitectPrompts()
        self.overlay = NovelPromptOverlay(self.original, NovelComposePrompts())

    def test_main_system_is_compose_planning(self):
        """Should use compose planning language, not direct-edit language."""
        self.assertIn("creative director", self.overlay.main_system)
        self.assertNotIn("Output the complete updated content", self.overlay.main_system)

    def test_system_reminder_passes_through_empty(self):
        """ArchitectPrompts system_reminder is empty — should pass through."""
        self.assertEqual(self.overlay.system_reminder, "")

    def test_example_messages_passes_through_empty(self):
        """ArchitectPrompts example_messages is empty — should pass through."""
        self.assertEqual(self.overlay.example_messages, [])

    def test_lazy_prompt_is_compose_version(self):
        """Should get the compose lazy_prompt, not the direct-edit version."""
        self.assertIn("describe", self.overlay.lazy_prompt.lower())
        self.assertNotIn("scene continues", self.overlay.lazy_prompt.lower())

    def test_files_content_prefix_from_novel(self):
        """File presentation should come from the novel layer, not ArchitectPrompts."""
        self.assertIn("added these files", self.overlay.files_content_prefix.lower())

    def test_repo_content_prefix_is_disabled(self):
        """Repo map is disabled in novel mode — prefix should be None."""
        self.assertIsNone(self.overlay.repo_content_prefix)

    def test_unknown_attr_falls_back_to_original(self):
        """Attributes not in any novel class should fall back to ArchitectPrompts."""
        # files_no_full_files_with_repo_map_reply is defined on ArchitectPrompts
        # but not on NovelComposePrompts or NovelPrompts (it's inherited from CoderPrompts)
        self.assertIsInstance(self.overlay.files_no_full_files_with_repo_map_reply, str)


class TestNarrativeFileValidation(unittest.TestCase):
    """Test that NarrativeMap.check_narrative_file validates paths correctly."""

    def setUp(self):
        from composez_core.narrative_map import NarrativeMap
        self.nmap = NarrativeMap("/tmp")

    def test_scene_prose_allowed(self):
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md"
        )
        self.assertIsNone(result)

    def test_scene_summary_allowed(self):
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/SUMMARY.md"
        )
        self.assertIsNone(result)

    def test_scene_content_rejected(self):
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/CONTENT.md"
        )
        self.assertIsNotNone(result)
        self.assertIn("Scene", result)

    def test_chapter_summary_allowed(self):
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/SUMMARY.md"
        )
        self.assertIsNone(result)

    def test_chapter_any_md_allowed(self):
        """Non-leaf levels allow any .md file."""
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/CONTENT.md"
        )
        self.assertIsNone(result)

    def test_chapter_prose_allowed(self):
        """Non-leaf levels allow any .md file, including PROSE.md."""
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/PROSE.md"
        )
        self.assertIsNone(result)

    def test_chapter_non_md_rejected(self):
        """Non-leaf levels reject non-.md files."""
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/Chapter 1 - Title/notes.txt"
        )
        self.assertIsNotNone(result)
        self.assertIn("Chapter", result)

    def test_act_summary_allowed(self):
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/SUMMARY.md"
        )
        self.assertIsNone(result)

    def test_act_any_md_allowed(self):
        """Act level (non-leaf) allows any .md file."""
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/CONTENT.md"
        )
        self.assertIsNone(result)

    def test_act_non_md_rejected(self):
        """Act level rejects non-.md files."""
        result = self.nmap.check_narrative_file(
            "Act 1 - Title/notes.txt"
        )
        self.assertIsNotNone(result)
        self.assertIn("Act", result)

    def test_non_act_path_ignored(self):
        result = self.nmap.check_narrative_file("db/characters/alice.md")
        self.assertIsNone(result)

    def test_short_path_rejected(self):
        """Bare filenames in the root should be rejected (likely from split paths)."""
        result = self.nmap.check_narrative_file("somefile.md")
        self.assertIsNotNone(result)
        self.assertIn("project root", result)


class TestActivateNovelMode(unittest.TestCase):
    """Test that activate_novel_mode applies prompts and validator correctly."""

    def _make_coder(self):
        """Create a minimal coder-like object for testing."""
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        # Provide the minimal attributes that activate_novel_mode touches
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.system_reminder = "original system reminder"
        coder.gpt_prompts.example_messages = [{"role": "user", "content": "original"}]
        coder.edit_path_validator = None
        return coder

    def test_sets_validator(self):
        from composez_core.narrative_map import NarrativeMap

        coder = self._make_coder()
        activate_novel_mode(coder)
        # The validator is a bound method on a NarrativeMap instance
        self.assertEqual(coder.edit_path_validator.__func__, NarrativeMap.check_narrative_file)

    def test_overlays_prompts(self):
        coder = self._make_coder()
        activate_novel_mode(coder)
        self.assertIsInstance(coder.gpt_prompts, NovelPromptOverlay)

    def test_main_system_is_novel(self):
        coder = self._make_coder()
        activate_novel_mode(coder)
        self.assertIn("fiction writer", coder.gpt_prompts.main_system)

    def test_preserves_format_prompts(self):
        coder = self._make_coder()
        activate_novel_mode(coder)
        self.assertEqual(coder.gpt_prompts.system_reminder, "original system reminder")
        self.assertEqual(
            coder.gpt_prompts.example_messages,
            [{"role": "user", "content": "original"}],
        )

    def test_compose_mode_uses_compose_prompts(self):
        """When autonomy_strategy is compose, should use planning-oriented prompts."""
        coder = self._make_coder()
        # Simulate compose strategy being attached before activate_novel_mode
        strategy = MagicMock()
        strategy.name = "compose"
        coder.autonomy_strategy = strategy
        activate_novel_mode(coder)
        self.assertIsInstance(coder.gpt_prompts, NovelPromptOverlay)
        self.assertIn("creative director", coder.gpt_prompts.main_system)
        self.assertNotIn("Output the complete updated content", coder.gpt_prompts.main_system)

    def test_direct_mode_uses_default_prompts(self):
        """When autonomy_strategy is direct, should use default NovelPrompts."""
        coder = self._make_coder()
        strategy = MagicMock()
        strategy.name = "direct"
        coder.autonomy_strategy = strategy
        activate_novel_mode(coder)
        self.assertIn("fiction writer", coder.gpt_prompts.main_system)
        self.assertNotIn("creative director", coder.gpt_prompts.main_system)


class TestActivateNovelQueryMode(unittest.TestCase):
    """Test that activate_novel_query_mode replaces prompts entirely."""

    def test_replaces_prompts(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.gpt_prompts = MagicMock()
        activate_novel_query_mode(coder)
        self.assertIsInstance(coder.gpt_prompts, NovelQueryPrompts)


class TestValidationViaPrepareToEdit(unittest.TestCase):
    """Test that edit_path_validator rejects disallowed files via prepare_to_edit."""

    def _make_coder_with_validator(self):
        """Create a minimal coder with the validator wired up."""
        from aider.coders.base_coder import Coder
        from composez_core.narrative_map import NarrativeMap

        coder = Coder.__new__(Coder)
        nmap = NarrativeMap("/tmp")
        coder.edit_path_validator = nmap.check_narrative_file
        coder.abs_fnames = set()
        coder.need_commit_before_edits = set()
        return coder

    def test_rejects_content_md_in_scene(self):
        coder = self._make_coder_with_validator()
        edits = [(
            "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/CONTENT.md",
            "block",
            ["Some content\n"],
        )]
        with self.assertRaises(ValueError) as ctx:
            coder.prepare_to_edit(edits)
        self.assertIn("Scene", str(ctx.exception))

    def test_allows_md_in_chapter(self):
        """Non-leaf directories allow any .md file."""
        coder = self._make_coder_with_validator()
        # CONTENT.md is now allowed at chapter level (any .md is fine)
        result = coder.edit_path_validator(
            "Act 1 - Title/Chapter 1 - Title/CONTENT.md"
        )
        self.assertIsNone(result)

    def test_allows_prose_in_scene(self):
        coder = self._make_coder_with_validator()
        edits = [(
            "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md",
            "block",
            ["Some prose\n"],
        )]
        with patch.object(type(coder), "allowed_to_edit", return_value=True), \
             patch.object(type(coder), "dirty_commit"):
            result = coder.prepare_to_edit(edits)
            self.assertEqual(len(result), 1)

    def test_no_validator_allows_anything(self):
        coder = self._make_coder_with_validator()
        coder.edit_path_validator = None
        edits = [(
            "Act 1 - Title/Chapter 1 - Title/CONTENT.md",
            "block",
            ["Some content\n"],
        )]
        with patch.object(type(coder), "allowed_to_edit", return_value=True), \
             patch.object(type(coder), "dirty_commit"):
            result = coder.prepare_to_edit(edits)
            self.assertEqual(len(result), 1)

    def test_rejects_bare_filename_in_root(self):
        """Bare filenames in the root (e.g. from whitespace-split paths) should be rejected."""
        coder = self._make_coder_with_validator()
        for bare_name in ("1", "Act", "Title", "PROSE.md", "foo.txt"):
            with self.subTest(bare_name=bare_name):
                edits = [(bare_name, "block", ["content\n"])]
                with self.assertRaises(ValueError) as ctx:
                    coder.prepare_to_edit(edits)
                self.assertIn("project root", str(ctx.exception))

    def test_rejects_bare_filename_via_validator_directly(self):
        """check_narrative_file should return an error for root-level paths."""
        from composez_core.narrative_map import NarrativeMap

        nmap = NarrativeMap("/tmp")
        for bare_name in ("1", "Act", "SUMMARY.md"):
            with self.subTest(bare_name=bare_name):
                result = nmap.check_narrative_file(bare_name)
                self.assertIsNotNone(result)
                self.assertIn("project root", result)

    def test_allows_novel_subdir_files(self):
        """Files inside novel/ subdirectories should still be allowed."""
        from composez_core.narrative_map import NarrativeMap

        nmap = NarrativeMap("/tmp")
        result = nmap.check_narrative_file(
            "novel/Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md"
        )
        self.assertIsNone(result)


class TestAutoCreateAndGroupConfirm(unittest.TestCase):
    """Test that SUMMARY.md/PROSE.md auto-create without prompting
    and other files get a ConfirmGroup with (A)ll support."""

    def _make_coder(self, tmp_path):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.edit_path_validator = None
        coder.abs_fnames = set()
        coder.auto_create_fnames = {"SUMMARY.md", "PROSE.md"}
        coder.need_commit_before_edits = set()
        coder.root = str(tmp_path)
        coder.abs_root_path_cache = {}
        coder.repo = None
        coder.dry_run = False
        coder.io = MagicMock()
        coder.io.confirm_ask = MagicMock(return_value=True)
        return coder

    def test_auto_create_skips_prompt_for_act_summary(self):
        """Act SUMMARY.md should be auto-created without prompting."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            result = coder.allowed_to_edit("Act 1 - The Shadow Falls/SUMMARY.md")
            self.assertTrue(result)
            coder.io.confirm_ask.assert_not_called()

    def test_auto_create_skips_prompt_for_chapter_summary(self):
        """Chapter SUMMARY.md should be auto-created without prompting."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            result = coder.allowed_to_edit(
                "Act 1 - Title/Chapter 1 - Death at Dinner/SUMMARY.md"
            )
            self.assertTrue(result)
            coder.io.confirm_ask.assert_not_called()

    def test_auto_create_skips_prompt_for_scene_summary(self):
        """Scene SUMMARY.md should be auto-created without prompting."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            result = coder.allowed_to_edit(
                "Act 1 - Title/Chapter 1 - Title/Scene 1 - The Collapse/SUMMARY.md"
            )
            self.assertTrue(result)
            coder.io.confirm_ask.assert_not_called()

    def test_auto_create_skips_prompt_for_scene_prose(self):
        """Scene PROSE.md should be auto-created without prompting."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            result = coder.allowed_to_edit(
                "Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md"
            )
            self.assertTrue(result)
            coder.io.confirm_ask.assert_not_called()

    def test_db_file_still_prompts(self):
        """DB files should still trigger confirm_ask."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            result = coder.allowed_to_edit("db/characters.json")
            self.assertTrue(result)
            coder.io.confirm_ask.assert_called_once()

    def test_prepare_to_edit_passes_group_for_db_files(self):
        """prepare_to_edit should pass a ConfirmGroup with (A)ll support
        when multiple db files are created."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            coder = self._make_coder(Path(tmp))
            edits = [
                ("db/characters.json", "block", ["content\n"]),
                ("db/locations.json", "block", ["content\n"]),
            ]
            with patch.object(type(coder), "allowed_to_edit", return_value=True) as mock_edit, \
                 patch.object(type(coder), "dirty_commit"):
                coder.prepare_to_edit(edits)
                # Both calls should have received a group keyword argument
                for call in mock_edit.call_args_list:
                    self.assertIn("group", call.kwargs)
                    group = call.kwargs["group"]
                    # Group with 2 paths should show group options
                    self.assertTrue(group.show_group)

    def test_activate_novel_mode_sets_auto_create(self):
        """activate_novel_mode should set auto_create_fnames on the coder."""
        from aider.coders.base_coder import Coder
        from composez_core.novel_coder import activate_novel_mode

        coder = Coder.__new__(Coder)
        coder.auto_create_fnames = set()
        coder.gpt_prompts = MagicMock()
        activate_novel_mode(coder)
        self.assertIn("SUMMARY.md", coder.auto_create_fnames)
        self.assertIn("PROSE.md", coder.auto_create_fnames)


class TestValidatorPropagation(unittest.TestCase):
    """Test that edit_path_validator propagates across coder switches."""

    @unittest.skipUnless(HAS_GIT, "gitpython not installed")
    @patch("aider.models.Model.validate_environment", return_value={"missing_keys": [], "keys_in_environment": []})
    def test_validator_carries_over_on_format_switch(self, _mock_validate):
        """Switching edit format via Coder.create(from_coder=...) preserves the validator."""
        from aider.coders import Coder
        from aider.models import Model
        from composez_core.narrative_map import NarrativeMap
        from aider.utils import GitTemporaryDirectory

        with GitTemporaryDirectory():
            repo = git.Repo()
            repo.config_writer().set_value("user", "name", "Test").release()
            repo.config_writer().set_value("user", "email", "test@test.com").release()
            repo.config_writer().set_value("commit", "gpgsign", "false").release()

            fname = Path("test.txt")
            fname.write_text("test content", encoding="utf-8")
            repo.git.add(str(fname))
            repo.git.commit("-m", "init", "--no-gpg-sign")

            io = MagicMock()
            model = Model("gpt-3.5-turbo")

            # Create a whole-file coder and activate novel mode
            whole_coder = Coder.create(
                main_model=model,
                io=io,
                edit_format="whole",
                fnames=["test.txt"],
            )
            activate_novel_mode(whole_coder)
            self.assertIsNotNone(whole_coder.edit_path_validator)

            # Switch to diff edit format — validator should carry over
            diff_coder = Coder.create(
                main_model=model,
                io=io,
                edit_format="diff",
                from_coder=whole_coder,
            )
            self.assertIsNotNone(diff_coder.edit_path_validator)
            # The validator is a bound method on a NarrativeMap instance
            self.assertEqual(
                diff_coder.edit_path_validator.__func__,
                NarrativeMap.check_narrative_file,
            )

    @unittest.skipUnless(HAS_GIT, "gitpython not installed")
    @patch("aider.models.Model.validate_environment", return_value={"missing_keys": [], "keys_in_environment": []})
    def test_all_coders_get_novel_mode_automatically(self, _mock_validate):
        """Every coder created via Coder.create() should have novel mode active."""
        from aider.coders import Coder
        from aider.models import Model
        from composez_core.narrative_map import NarrativeMap
        from aider.utils import GitTemporaryDirectory

        with GitTemporaryDirectory():
            repo = git.Repo()
            repo.config_writer().set_value("user", "name", "Test").release()
            repo.config_writer().set_value("user", "email", "test@test.com").release()
            repo.config_writer().set_value("commit", "gpgsign", "false").release()

            fname = Path("test.txt")
            fname.write_text("test content", encoding="utf-8")
            repo.git.add(str(fname))
            repo.git.commit("-m", "init", "--no-gpg-sign")

            io = MagicMock()
            model = Model("gpt-3.5-turbo")

            coder = Coder.create(
                main_model=model,
                io=io,
                edit_format="whole",
                fnames=["test.txt"],
            )
            # Novel mode is automatically activated for all coders
            self.assertIsNotNone(coder.edit_path_validator)
            # The validator is a bound method on a NarrativeMap instance
            self.assertEqual(
                coder.edit_path_validator.__func__,
                NarrativeMap.check_narrative_file,
            )
            self.assertIn("fiction writer", coder.gpt_prompts.main_system)

    @unittest.skipUnless(HAS_GIT, "gitpython not installed")
    @patch("aider.models.Model.validate_environment", return_value={"missing_keys": [], "keys_in_environment": []})
    def test_novel_mode_works_with_diff_format(self, _mock_validate):
        """activate_novel_mode should work with diff-format coders."""
        from aider.coders import Coder
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.models import Model
        from aider.utils import GitTemporaryDirectory

        with GitTemporaryDirectory():
            repo = git.Repo()
            repo.config_writer().set_value("user", "name", "Test").release()
            repo.config_writer().set_value("user", "email", "test@test.com").release()
            repo.config_writer().set_value("commit", "gpgsign", "false").release()

            fname = Path("test.txt")
            fname.write_text("test content", encoding="utf-8")
            repo.git.add(str(fname))
            repo.git.commit("-m", "init", "--no-gpg-sign")

            io = MagicMock()
            model = Model("gpt-3.5-turbo")

            coder = Coder.create(
                main_model=model,
                io=io,
                edit_format="diff",
                fnames=["test.txt"],
            )
            self.assertIsInstance(coder, EditBlockCoder)

            # Activate novel mode — should overlay prompts without changing format
            original_reminder = coder.gpt_prompts.system_reminder
            activate_novel_mode(coder)

            self.assertIn("fiction writer", coder.gpt_prompts.main_system)
            self.assertEqual(coder.gpt_prompts.system_reminder, original_reminder)
            self.assertIsNotNone(coder.edit_path_validator)


class TestLoadCoreContext(unittest.TestCase):
    """Test that load_core_context adds db/core/ files to read-only context."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from composez_core.db import Db

        self.db = Db(self.tmpdir)
        self.db.init_db()

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = self.tmpdir
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.gpt_prompts = MagicMock()
        coder.edit_path_validator = None
        coder.auto_create_fnames = set()
        coder.io = MagicMock()
        return coder

    def test_loads_core_files(self):
        coder = self._make_coder()
        load_core_context(coder)
        self.assertEqual(len(coder.abs_read_only_fnames), 1)
        path = list(coder.abs_read_only_fnames)[0]
        self.assertTrue(path.endswith("style.md"))

    def test_loads_multiple_core_files(self):
        self.db.create_entry("core", "voice", "Voice guide content.")
        coder = self._make_coder()
        load_core_context(coder)
        self.assertEqual(len(coder.abs_read_only_fnames), 2)

    def test_idempotent(self):
        """Calling load_core_context twice doesn't duplicate entries."""
        coder = self._make_coder()
        load_core_context(coder)
        load_core_context(coder)
        self.assertEqual(len(coder.abs_read_only_fnames), 1)

    def test_no_root_does_nothing(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = None
        coder.abs_read_only_fnames = set()
        load_core_context(coder)
        self.assertEqual(len(coder.abs_read_only_fnames), 0)

    def test_no_db_dir_does_nothing(self):
        from aider.coders.base_coder import Coder

        empty_dir = tempfile.mkdtemp()
        coder = Coder.__new__(Coder)
        coder.root = empty_dir
        coder.abs_read_only_fnames = set()
        load_core_context(coder)
        self.assertEqual(len(coder.abs_read_only_fnames), 0)

    def test_activate_novel_mode_loads_core(self):
        """activate_novel_mode should auto-load core context."""
        coder = self._make_coder()
        activate_novel_mode(coder)
        # Should have loaded style.md and the auto-created metadata.yml
        core_paths = [
            p for p in coder.abs_read_only_fnames if "core" in p
        ]
        self.assertEqual(len(core_paths), 2)


class TestEnsureMetadata(unittest.TestCase):
    """Test that _ensure_metadata creates db/core/metadata.yml on first run."""

    def _make_coder(self, root, repo=None):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = root
        coder.repo = repo
        return coder

    def test_creates_metadata_file(self):
        """Should create db/core/metadata.yml with repo name and Unknown author."""
        import yaml

        with tempfile.TemporaryDirectory() as root:
            coder = self._make_coder(root)
            _ensure_metadata(coder)
            metadata_path = os.path.join(root, "db", "core", "metadata.yml")
            self.assertTrue(os.path.isfile(metadata_path))
            content = Path(metadata_path).read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            self.assertEqual(data["title"], os.path.basename(root))
            self.assertEqual(data["author"], "Unknown")

    def test_does_not_overwrite_existing(self):
        """Should not overwrite an existing metadata.yml."""
        import yaml

        with tempfile.TemporaryDirectory() as root:
            core_dir = os.path.join(root, "db", "core")
            os.makedirs(core_dir)
            metadata_path = os.path.join(core_dir, "metadata.yml")
            Path(metadata_path).write_text("title: My Novel\nauthor: Jane\n", encoding="utf-8")

            coder = self._make_coder(root)
            _ensure_metadata(coder)

            data = yaml.safe_load(Path(metadata_path).read_text(encoding="utf-8"))
            self.assertEqual(data["title"], "My Novel")
            self.assertEqual(data["author"], "Jane")

    def test_uses_git_user_name(self):
        """Should use git user.name if available."""
        import yaml

        with tempfile.TemporaryDirectory() as root:
            mock_repo = MagicMock()
            mock_repo.repo.git.config.return_value = "Alice Smith"
            coder = self._make_coder(root, repo=mock_repo)
            _ensure_metadata(coder)

            metadata_path = os.path.join(root, "db", "core", "metadata.yml")
            data = yaml.safe_load(Path(metadata_path).read_text(encoding="utf-8"))
            self.assertEqual(data["author"], "Alice Smith")

    def test_no_root_does_nothing(self):
        """Should do nothing if coder has no root."""
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = None
        _ensure_metadata(coder)
        # No crash, no file created


class TestCollapsePaths(unittest.TestCase):
    """Test the standalone collapse_paths function."""

    def test_collapses_full_directory(self):
        """When all children of a dir are present, collapse to the dir."""
        with tempfile.TemporaryDirectory() as root:
            # Create dir with two files
            d = os.path.join(root, "mydir")
            os.makedirs(d)
            Path(os.path.join(d, "a.md")).write_text("hello", encoding="utf-8")
            Path(os.path.join(d, "b.md")).write_text("world", encoding="utf-8")

            result = collapse_paths(root, ["mydir/a.md", "mydir/b.md"])
            self.assertEqual(result, ["mydir/"])

    def test_no_collapse_partial(self):
        """When only some children are present, don't collapse."""
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "mydir")
            os.makedirs(d)
            Path(os.path.join(d, "a.md")).write_text("hello", encoding="utf-8")
            Path(os.path.join(d, "b.md")).write_text("world", encoding="utf-8")

            result = collapse_paths(root, ["mydir/a.md"])
            self.assertEqual(result, ["mydir/a.md"])

    def test_recursive_collapse(self):
        """Collapsing should work recursively up the tree."""
        with tempfile.TemporaryDirectory() as root:
            # act/1/SUMMARY.md, act/1/chapter/1/SUMMARY.md
            scene = os.path.join(root, "act", "1", "chapter", "1")
            os.makedirs(scene)
            Path(os.path.join(root, "act", "1", "SUMMARY.md")).write_text("s", encoding="utf-8")
            Path(os.path.join(scene, "SUMMARY.md")).write_text("s", encoding="utf-8")

            paths = [
                "act/1/SUMMARY.md",
                "act/1/chapter/1/SUMMARY.md",
            ]
            result = collapse_paths(root, paths)
            # Collapses all the way: chapter/1 → act/1/chapter → act/1 → act
            self.assertEqual(result, ["act/"])

    def test_empty_input(self):
        with tempfile.TemporaryDirectory() as root:
            result = collapse_paths(root, [])
            self.assertEqual(result, [])

    def test_skips_hidden_files(self):
        """Hidden files on disk should not prevent collapsing."""
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "mydir")
            os.makedirs(d)
            Path(os.path.join(d, "a.md")).write_text("hello", encoding="utf-8")
            Path(os.path.join(d, ".hidden")).write_text("secret", encoding="utf-8")

            result = collapse_paths(root, ["mydir/a.md"])
            self.assertEqual(result, ["mydir/"])


class TestDisplayFnamesFormatter(unittest.TestCase):
    """Test that activate_novel_mode installs the display formatter on io."""

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.system_reminder = "original"
        coder.gpt_prompts.example_messages = []
        coder.edit_path_validator = None
        coder.auto_create_fnames = set()
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.io = MagicMock()
        coder.root = None
        return coder

    def test_formatter_installed_when_root_set(self):
        """activate_novel_mode should set display_fnames_formatter on io."""
        with tempfile.TemporaryDirectory() as root:
            coder = self._make_coder()
            coder.root = root
            activate_novel_mode(coder)
            self.assertTrue(hasattr(coder.io, "display_fnames_formatter"))
            self.assertIsNotNone(coder.io.display_fnames_formatter)

    def test_formatter_not_installed_without_root(self):
        """Without root, no formatter should be installed."""
        coder = self._make_coder()
        coder.root = None
        # Use a simple namespace instead of MagicMock (which auto-creates attrs)
        coder.io = type("IO", (), {})()
        activate_novel_mode(coder)
        self.assertFalse(hasattr(coder.io, "display_fnames_formatter"))

    def test_formatter_collapses_editable_files(self):
        """The installed formatter should collapse fully-covered directories."""
        with tempfile.TemporaryDirectory() as root:
            # Create a scene directory with SUMMARY.md and PROSE.md
            scene = os.path.join(root, "act", "1 - Title", "chapter", "1 - Ch", "scene", "1 - Sc")
            os.makedirs(scene)
            Path(os.path.join(scene, "SUMMARY.md")).write_text("sum", encoding="utf-8")
            Path(os.path.join(scene, "PROSE.md")).write_text("prose", encoding="utf-8")
            # Add a second scene so that only scene/1 collapses, not the whole tree
            scene2 = os.path.join(root, "act", "1 - Title", "chapter", "1 - Ch", "scene", "2 - Sc")
            os.makedirs(scene2)
            Path(os.path.join(scene2, "PROSE.md")).write_text("more", encoding="utf-8")

            coder = self._make_coder()
            coder.root = root
            activate_novel_mode(coder)

            formatter = coder.io.display_fnames_formatter
            # Only add files from scene 1 (not scene 2)
            all_fnames = [
                "act/1 - Title/chapter/1 - Ch/scene/1 - Sc/SUMMARY.md",
                "act/1 - Title/chapter/1 - Ch/scene/1 - Sc/PROSE.md",
            ]
            new_all, new_ro = formatter(all_fnames, [])
            # Scene 1 should collapse, but not further (scene 2 exists)
            self.assertEqual(len(new_all), 1)
            self.assertIn("scene/1 - Sc/", new_all[0])
            self.assertEqual(new_ro, [])

    def test_formatter_keeps_readonly_separate(self):
        """Read-only and editable files should be collapsed independently."""
        with tempfile.TemporaryDirectory() as root:
            # Editable: act/1 has SUMMARY.md only; act/2 exists to prevent
            # collapsing past act/1
            os.makedirs(os.path.join(root, "act", "1"))
            os.makedirs(os.path.join(root, "act", "2"))
            Path(os.path.join(root, "act", "1", "SUMMARY.md")).write_text("s", encoding="utf-8")
            Path(os.path.join(root, "act", "2", "SUMMARY.md")).write_text("s2", encoding="utf-8")

            # Read-only: db/core with style.md and metadata.yml (auto-created
            # by _ensure_metadata); db/characters exists to prevent
            # collapsing past db/core
            os.makedirs(os.path.join(root, "db", "core"), exist_ok=True)
            os.makedirs(os.path.join(root, "db", "characters"))
            Path(os.path.join(root, "db", "core", "style.md")).write_text("style", encoding="utf-8")
            Path(os.path.join(root, "db", "characters", "alice.md")).write_text("a", encoding="utf-8")

            coder = self._make_coder()
            coder.root = root
            activate_novel_mode(coder)
            # _ensure_metadata creates metadata.yml in db/core/

            formatter = coder.io.display_fnames_formatter
            all_fnames = [
                "act/1/SUMMARY.md",
                "db/core/style.md",
                "db/core/metadata.yml",
            ]
            ro_fnames = ["db/core/style.md", "db/core/metadata.yml"]
            new_all, new_ro = formatter(all_fnames, ro_fnames)

            # act/1 collapses (SUMMARY.md is only child), db/core collapses
            self.assertIn("act/1/", new_all)
            self.assertIn("db/core/", new_all)
            self.assertIn("db/core/", new_ro)
            self.assertNotIn("act/1/", new_ro)


class TestAutoGitMv(unittest.TestCase):
    """Test that activate_novel_mode auto-runs git mv commands."""

    def _make_coder(self):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.system_reminder = "original"
        coder.gpt_prompts.example_messages = []
        coder.edit_path_validator = None
        coder.auto_create_fnames = set()
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.io = MagicMock()
        coder.root = None
        coder.shell_commands = []
        coder.suggest_shell_commands = True
        coder.commands = MagicMock()
        return coder

    def test_wraps_run_shell_commands(self):
        """activate_novel_mode should replace run_shell_commands on the instance."""
        from aider.coders.base_coder import Coder

        coder = self._make_coder()
        activate_novel_mode(coder)
        # Should be a bound method on the instance, not the class method
        self.assertIsNot(
            coder.run_shell_commands.__func__,
            Coder.run_shell_commands,
        )

    def test_git_mv_auto_runs(self):
        """A shell block with only git mv commands should auto-run via cmd_git."""
        coder = self._make_coder()
        activate_novel_mode(coder)

        coder.shell_commands = [
            'git mv "act/1 - Old Title" "act/1 - New Title"\n'
        ]
        coder.run_shell_commands()

        coder.commands.cmd_git.assert_called_once_with(
            'mv "act/1 - Old Title" "act/1 - New Title"'
        )
        # Should not have prompted the user
        coder.io.confirm_ask.assert_not_called()
        # shell_commands should be empty after auto-run
        self.assertEqual(coder.shell_commands, [])

    def test_multi_git_mv_auto_runs(self):
        """A block with multiple git mv lines should all auto-run."""
        coder = self._make_coder()
        activate_novel_mode(coder)

        coder.shell_commands = [
            'git mv "act/1 - Old" "act/1 - New"\ngit mv "act/2 - Old" "act/2 - New"\n'
        ]
        coder.run_shell_commands()

        self.assertEqual(coder.commands.cmd_git.call_count, 2)
        coder.commands.cmd_git.assert_any_call('mv "act/1 - Old" "act/1 - New"')
        coder.commands.cmd_git.assert_any_call('mv "act/2 - Old" "act/2 - New"')
        coder.io.confirm_ask.assert_not_called()

    def test_non_git_mv_not_auto_run(self):
        """Non git-mv commands should NOT auto-run."""
        coder = self._make_coder()
        activate_novel_mode(coder)

        coder.shell_commands = ["rm -rf something\n"]
        # The base run_shell_commands will prompt; since confirm_ask returns
        # MagicMock (truthy), it would try to run. We just check that
        # our wrapper didn't auto-run it.
        coder.run_shell_commands()

        # cmd_git should NOT have been called
        coder.commands.cmd_git.assert_not_called()
        # The base method prompts for non-auto commands
        coder.io.confirm_ask.assert_called()

    def test_mixed_block_not_auto_run(self):
        """A block mixing git mv with other commands should NOT auto-run."""
        coder = self._make_coder()
        activate_novel_mode(coder)

        coder.shell_commands = [
            'git mv "old" "new"\necho "done"\n'
        ]
        coder.run_shell_commands()

        # Mixed block stays in remaining, goes through normal confirmation
        coder.commands.cmd_git.assert_not_called()
        coder.io.confirm_ask.assert_called()

    def test_git_mv_with_comments_auto_runs(self):
        """Comments in a git mv block should be ignored (still auto-runs)."""
        coder = self._make_coder()
        activate_novel_mode(coder)

        coder.shell_commands = [
            '# Rename the act\ngit mv "act/1 - Old" "act/1 - New"\n'
        ]
        coder.run_shell_commands()

        coder.commands.cmd_git.assert_called_once_with(
            'mv "act/1 - Old" "act/1 - New"'
        )
        coder.io.confirm_ask.assert_not_called()


class TestFileSorting(unittest.TestCase):
    """Test that novel mode sorts files: non-act → summaries → prose."""

    def test_sort_key_non_act_before_summary(self):
        root = "/project"
        key_db = _novel_file_sort_key("/project/db/characters/alice.md", root)
        key_sum = _novel_file_sort_key(
            "/project/act/1 - Title/chapter/1 - Ch/scene/1 - Sc/SUMMARY.md", root
        )
        self.assertLess(key_db, key_sum)

    def test_sort_key_summary_before_prose(self):
        root = "/project"
        key_sum = _novel_file_sort_key(
            "/project/act/1 - Title/SUMMARY.md", root
        )
        key_prose = _novel_file_sort_key(
            "/project/act/1 - Title/chapter/1 - Ch/scene/1 - Sc/PROSE.md", root
        )
        self.assertLess(key_sum, key_prose)

    def test_sort_key_summaries_sorted_by_path(self):
        root = "/project"
        key_a1 = _novel_file_sort_key(
            "/project/act/1 - Title/SUMMARY.md", root
        )
        key_a2 = _novel_file_sort_key(
            "/project/act/2 - Title/SUMMARY.md", root
        )
        self.assertLess(key_a1, key_a2)

    def _make_coder(self, root):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.root = root
        coder.abs_fnames = set()
        coder.abs_read_only_fnames = set()
        coder.io = MagicMock()
        coder.fence = ("```", "```")
        return coder

    def test_get_abs_fnames_content_order(self):
        """Patched get_abs_fnames_content yields non-act → summaries → prose."""
        with tempfile.TemporaryDirectory() as root:
            # Create files
            paths = {
                "act/1 - A/chapter/1 - B/scene/1 - C/PROSE.md": "prose1",
                "act/1 - A/SUMMARY.md": "act summary",
                "act/1 - A/chapter/1 - B/scene/1 - C/SUMMARY.md": "scene summary",
                "db/characters/alice.md": "alice",
            }
            for rel, content in paths.items():
                abs_path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                Path(abs_path).write_text(content, encoding="utf-8")

            coder = self._make_coder(root)
            for rel in paths:
                coder.abs_fnames.add(os.path.join(root, rel))

            # Install sorting
            _install_file_sorting(coder)

            # Collect yielded filenames
            yielded = [
                os.path.relpath(fname, root)
                for fname, _content in coder.get_abs_fnames_content()
            ]

            # Non-act first, then summaries, then prose
            self.assertEqual(yielded[0], "db/characters/alice.md")
            self.assertEqual(yielded[1], "act/1 - A/SUMMARY.md")
            self.assertEqual(
                yielded[2],
                "act/1 - A/chapter/1 - B/scene/1 - C/SUMMARY.md",
            )
            self.assertEqual(
                yielded[3],
                "act/1 - A/chapter/1 - B/scene/1 - C/PROSE.md",
            )

    def test_get_read_only_files_content_order(self):
        """Patched get_read_only_files_content orders non-act → summaries → prose."""
        with tempfile.TemporaryDirectory() as root:
            paths = {
                "act/1 - A/chapter/1 - B/scene/1 - C/PROSE.md": "prose",
                "act/1 - A/SUMMARY.md": "summary",
                "db/core/style.md": "style guide",
            }
            for rel, content in paths.items():
                abs_path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                Path(abs_path).write_text(content, encoding="utf-8")

            coder = self._make_coder(root)
            # Make io.read_text return actual file content
            coder.io.read_text = lambda fname: Path(fname).read_text(encoding="utf-8")
            for rel in paths:
                coder.abs_read_only_fnames.add(os.path.join(root, rel))

            _install_file_sorting(coder)

            prompt = coder.get_read_only_files_content()

            # Check that style.md appears before SUMMARY.md before PROSE.md
            pos_style = prompt.index("db/core/style.md")
            pos_summary = prompt.index("act/1 - A/SUMMARY.md")
            pos_prose = prompt.index("act/1 - A/chapter/1 - B/scene/1 - C/PROSE.md")
            self.assertLess(pos_style, pos_summary)
            self.assertLess(pos_summary, pos_prose)

    def test_activate_novel_mode_installs_sorting(self):
        """activate_novel_mode should install file sorting when root is set."""
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as root:
            coder = self._make_coder(root)
            coder.gpt_prompts = MagicMock()
            coder.gpt_prompts.system_reminder = "original"
            coder.gpt_prompts.example_messages = []
            coder.edit_path_validator = None
            coder.auto_create_fnames = set()

            activate_novel_mode(coder)

            # The method should be a bound method on the instance
            self.assertIsNot(
                coder.get_abs_fnames_content,
                Coder.get_abs_fnames_content,
            )


class TestAutoContextCliOverride(unittest.TestCase):
    """Test that --auto-context CLI flag overrides .composez config."""

    def _make_coder(self, auto_context=None):
        from aider.coders.base_coder import Coder

        coder = Coder.__new__(Coder)
        coder.io = MagicMock()
        coder.io.tool_output = MagicMock()
        coder.io.tool_error = MagicMock()
        coder.io.tool_warning = MagicMock()
        coder.root = "/tmp/fake"
        coder.auto_context = auto_context
        coder.edit_format = "diff"
        coder.commands = MagicMock()
        return coder

    @patch("composez_core.novel_coder.get_auto_context", return_value=False)
    def test_cli_true_overrides_config_false(self, mock_config):
        """CLI --auto-context overrides .composez auto_context: false."""
        from composez_core.novel_coder import _install_auto_context

        coder = self._make_coder(auto_context=True)
        _install_auto_context(coder)
        self.assertTrue(coder._auto_context_enabled)

    @patch("composez_core.novel_coder.get_auto_context", return_value=True)
    def test_cli_false_overrides_config_true(self, mock_config):
        """CLI --no-auto-context overrides .composez auto_context: true."""
        from composez_core.novel_coder import _install_auto_context

        coder = self._make_coder(auto_context=False)
        _install_auto_context(coder)
        self.assertFalse(coder._auto_context_enabled)

    @patch("composez_core.novel_coder.get_auto_context", return_value=True)
    def test_no_cli_flag_uses_config(self, mock_config):
        """No CLI flag (None) falls back to .composez."""
        from composez_core.novel_coder import _install_auto_context

        coder = self._make_coder(auto_context=None)
        _install_auto_context(coder)
        self.assertTrue(coder._auto_context_enabled)
        mock_config.assert_called_once()

    @patch("composez_core.novel_coder.get_auto_context", return_value=False)
    def test_no_cli_flag_uses_config_false(self, mock_config):
        """No CLI flag respects .composez auto_context: false."""
        from composez_core.novel_coder import _install_auto_context

        coder = self._make_coder(auto_context=None)
        _install_auto_context(coder)
        self.assertFalse(coder._auto_context_enabled)


if __name__ == "__main__":
    unittest.main()
