"""Tests for task-specific model configuration from .composez.

Validates that model roles (admin_model, query_model, edit_model, etc.) are
correctly read from .composez and resolved during coder creation.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestConfigModelFunctions(unittest.TestCase):
    """Tests for config.py model helpers."""

    def _write_composez(self, tmpdir, data):
        import yaml

        path = os.path.join(tmpdir, ".composez")
        Path(path).write_text(
            yaml.dump(data, default_flow_style=False),
            encoding="utf-8",
        )

    def test_get_models_empty(self):
        from composez_core.config import get_models

        with tempfile.TemporaryDirectory() as tmpdir:
            result = get_models(tmpdir)
            self.assertEqual(result, {})

    def test_get_models_no_models_key(self):
        from composez_core.config import get_models

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"levels": ["Act", "Chapter", "Scene"]})
            result = get_models(tmpdir)
            self.assertEqual(result, {})

    def test_get_models_returns_configured(self):
        from composez_core.config import get_models

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {
                "levels": ["Act", "Chapter", "Scene"],
                "models": {
                    "admin_model": "claude-haiku-4-5-20251001",
                    "query_model": "claude-sonnet-4-6",
                    "edit_model": "claude-sonnet-4-6",
                },
            })
            result = get_models(tmpdir)
            self.assertEqual(result, {
                "admin_model": "claude-haiku-4-5-20251001",
                "query_model": "claude-sonnet-4-6",
                "edit_model": "claude-sonnet-4-6",
            })

    def test_get_models_filters_unknown_keys(self):
        from composez_core.config import get_models

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {
                "models": {
                    "query_model": "claude-sonnet-4-6",
                    "bogus_model": "should-be-dropped",
                },
            })
            result = get_models(tmpdir)
            self.assertNotIn("bogus_model", result)
            self.assertEqual(result["query_model"], "claude-sonnet-4-6")

    def test_get_models_filters_empty_values(self):
        from composez_core.config import get_models

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {
                "models": {
                    "query_model": "",
                    "edit_model": "claude-sonnet-4-6",
                },
            })
            result = get_models(tmpdir)
            self.assertNotIn("query_model", result)
            self.assertEqual(result["edit_model"], "claude-sonnet-4-6")

    def test_resolve_model_for_role_found(self):
        from composez_core.config import resolve_model_for_role

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {
                "models": {"query_model": "claude-sonnet-4-6"},
            })
            result = resolve_model_for_role(tmpdir, "query_model")
            self.assertEqual(result, "claude-sonnet-4-6")

    def test_resolve_model_for_role_missing_returns_fallback(self):
        from composez_core.config import resolve_model_for_role

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"models": {}})
            result = resolve_model_for_role(tmpdir, "query_model", fallback="default-model")
            self.assertEqual(result, "default-model")

    def test_resolve_model_for_role_no_config_returns_none(self):
        from composez_core.config import resolve_model_for_role

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_model_for_role(tmpdir, "query_model")
            self.assertIsNone(result)


class TestResolveComposezModel(unittest.TestCase):
    """Tests for Coder._resolve_composez_model()."""

    def _write_composez(self, tmpdir, models_dict):
        import yaml

        path = os.path.join(tmpdir, ".composez")
        Path(path).write_text(
            yaml.dump({"models": models_dict}, default_flow_style=False),
            encoding="utf-8",
        )

    def test_query_format_resolves_query_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"query_model": "test-query-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-query-model")
                result = Coder._resolve_composez_model("query", "direct", None, tmpdir)
                MockModel.assert_called_with("test-query-model")
                self.assertIsNotNone(result)

    def test_edit_format_resolves_edit_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"edit_model": "test-edit-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-edit-model")
                result = Coder._resolve_composez_model("diff", "direct", None, tmpdir)
                MockModel.assert_called_with("test-edit-model")
                self.assertIsNotNone(result)

    def test_selection_format_resolves_selection_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"selection_model": "test-sel-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-sel-model")
                result = Coder._resolve_composez_model("selection", "direct", None, tmpdir)
                MockModel.assert_called_with("test-sel-model")
                self.assertIsNotNone(result)

    def test_compose_autonomy_resolves_compose_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"compose_model": "test-compose-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-compose-model")
                result = Coder._resolve_composez_model("diff", "compose", None, tmpdir)
                MockModel.assert_called_with("test-compose-model")
                self.assertIsNotNone(result)

    def test_agent_autonomy_resolves_agent_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"agent_model": "test-agent-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-agent-model")
                result = Coder._resolve_composez_model("query", "agent", None, tmpdir)
                MockModel.assert_called_with("test-agent-model")
                self.assertIsNotNone(result)

    def test_context_format_resolves_admin_model(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"admin_model": "test-admin-model"})
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="test-admin-model")
                result = Coder._resolve_composez_model("context", "direct", None, tmpdir)
                MockModel.assert_called_with("test-admin-model")
                self.assertIsNotNone(result)

    def test_returns_none_when_role_not_configured(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"admin_model": "test-admin"})
            # query_model is not configured
            result = Coder._resolve_composez_model("query", "direct", None, tmpdir)
            self.assertIsNone(result)

    def test_returns_none_when_no_root(self):
        from aider.coders.base_coder import Coder

        result = Coder._resolve_composez_model("query", "direct", None, None)
        self.assertIsNone(result)

    def test_uses_from_coder_root(self):
        from aider.coders.base_coder import Coder

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_composez(tmpdir, {"query_model": "from-coder-model"})
            from_coder = MagicMock()
            from_coder.root = tmpdir
            with patch("aider.models.Model") as MockModel:
                MockModel.return_value = MagicMock(name="from-coder-model")
                result = Coder._resolve_composez_model("query", "direct", from_coder, None)
                MockModel.assert_called_with("from-coder-model")
                self.assertIsNotNone(result)


class TestComposePhase2ModelResolution(unittest.TestCase):
    """ComposeStrategy uses task-specific models for phase 2."""

    def test_phase2_uses_composez_model_when_configured(self):
        """When .composez has an edit_model, compose phase 2 should use it."""
        from aider.coders.autonomy import ComposeStrategy
        from aider.coders.base_coder import Coder

        strategy = ComposeStrategy()
        coder = MagicMock()
        coder.edit_format = "diff"
        coder.auto_accept_architect = True
        coder.partial_response_content = "Plan: make changes"
        coder.verbose = False
        coder.total_cost = 0.5
        coder.root = "/tmp/test"
        coder.main_model = MagicMock()
        coder.main_model.editor_model = MagicMock()
        coder.main_model.editor_edit_format = "editor-diff"

        resolved_model = MagicMock()
        with patch.object(Coder, "_resolve_composez_model", return_value=resolved_model):
            with patch.object(Coder, "create") as mock_create:
                mock_create.return_value = MagicMock()
                strategy.reply_completed(coder)
                call_kwargs = mock_create.call_args[1]
                self.assertIs(call_kwargs["main_model"], resolved_model)

    def test_phase2_falls_back_to_editor_model(self):
        """When .composez has no model, fall back to editor_model."""
        from aider.coders.autonomy import ComposeStrategy
        from aider.coders.base_coder import Coder

        strategy = ComposeStrategy()
        coder = MagicMock()
        coder.edit_format = "diff"
        coder.auto_accept_architect = True
        coder.partial_response_content = "Plan: make changes"
        coder.verbose = False
        coder.total_cost = 0.5
        coder.root = "/tmp/test"
        coder.main_model = MagicMock()
        editor_model = MagicMock()
        coder.main_model.editor_model = editor_model
        coder.main_model.editor_edit_format = "editor-diff"

        with patch.object(Coder, "_resolve_composez_model", return_value=None):
            with patch.object(Coder, "create") as mock_create:
                mock_create.return_value = MagicMock()
                strategy.reply_completed(coder)
                call_kwargs = mock_create.call_args[1]
                self.assertIs(call_kwargs["main_model"], editor_model)


class TestModelRolesConstant(unittest.TestCase):
    """MODEL_ROLES constant is complete."""

    def test_all_roles_present(self):
        from composez_core.config import MODEL_ROLES

        expected = {
            "admin_model", "query_model", "edit_model",
            "selection_model", "compose_model", "agent_model",
        }
        self.assertEqual(set(MODEL_ROLES), expected)


if __name__ == "__main__":
    unittest.main()
