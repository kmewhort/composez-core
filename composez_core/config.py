"""Configuration for novel mode.

Reads and writes the ``.composez`` file at the project root, which stores
configurable settings such as the narrative hierarchy levels and
task-specific model assignments.

An optional *model file override* (set via :func:`set_model_file`) takes
precedence over the project's ``.composez`` for model role assignments.
This allows the server to inject pre-configured model tiers (Low / Medium /
High Power) without modifying the user's project config.
"""

import os
from pathlib import Path

import yaml

CONFIG_FILE = ".composez"
DEFAULT_LEVELS = ["Act", "Chapter", "Scene"]
NOVEL_DIR = "novel"

# Valid keys inside the ``models:`` section of ``.composez``.
MODEL_ROLES = (
    "admin_model",
    "query_model",
    "edit_model",
    "selection_model",
    "compose_model",
    "agent_model",
)

# ---------------------------------------------------------------------------
# Model file override — set by ``--composez-model-file`` CLI flag
# ---------------------------------------------------------------------------

_model_file_override: str | None = None


def set_model_file(path: str | None):
    """Set a model override file whose values take precedence over ``.composez``.

    The file uses the same YAML format as ``.composez`` (a ``models:`` key
    mapping role names to model strings).  Pass ``None`` to clear.
    """
    global _model_file_override
    _model_file_override = path


def get_model_file() -> str | None:
    """Return the current model override file path, or ``None``."""
    return _model_file_override


def _config_path(root):
    """Return the absolute path to the ``.composez`` file."""
    return os.path.join(root, CONFIG_FILE)


def load_config(root):
    """Load the ``.composez`` config, returning a dict.

    Returns the default config if the file doesn't exist or is invalid.
    """
    path = _config_path(root)
    if not os.path.isfile(path):
        return {"levels": list(DEFAULT_LEVELS)}
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            return {"levels": list(DEFAULT_LEVELS)}
        levels = data.get("levels", DEFAULT_LEVELS)
        if (
            not isinstance(levels, list)
            or len(levels) < 2
            or not all(isinstance(l, str) and l.strip() for l in levels)
        ):
            levels = list(DEFAULT_LEVELS)
        else:
            # Normalize: title-case each level name
            levels = [l.strip().title() for l in levels]
        data["levels"] = levels
        return data
    except Exception:
        return {"levels": list(DEFAULT_LEVELS)}


def save_config(root, config=None):
    """Write the ``.composez`` config file.

    If *config* is ``None``, writes the default config.
    """
    if config is None:
        config = {"levels": list(DEFAULT_LEVELS)}
    path = _config_path(root)
    Path(path).write_text(
        yaml.dump(config, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


def ensure_config(root):
    """Create ``.composez`` if it doesn't exist, then return the config dict."""
    path = _config_path(root)
    if not os.path.isfile(path):
        save_config(root)
    return load_config(root)


def get_levels(root):
    """Convenience: return just the levels list from the config."""
    return load_config(root)["levels"]


def get_auto_context(root):
    """Return the ``auto_context`` setting (default ``True``)."""
    return load_config(root).get("auto_context", True)


def get_auto_lint(root):
    """Return the ``auto_lint`` setting (default ``True``)."""
    return load_config(root).get("auto_lint", True)


def get_models(root):
    """Return the ``models`` dict, merging any model file override on top.

    Base values come from ``.composez``.  If a model override file has been
    set via :func:`set_model_file`, its entries take precedence.

    Only keys listed in :data:`MODEL_ROLES` are returned; unknown keys
    are silently dropped.
    """
    raw = load_config(root).get("models")
    base = (
        {k: v for k, v in raw.items() if k in MODEL_ROLES and isinstance(v, str) and v}
        if isinstance(raw, dict)
        else {}
    )

    # Merge override file (highest priority)
    if _model_file_override and os.path.isfile(_model_file_override):
        try:
            text = Path(_model_file_override).read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                # Accept both ``models:`` nested and top-level role keys
                override = data.get("models", data)
                if isinstance(override, dict):
                    for k, v in override.items():
                        if k in MODEL_ROLES and isinstance(v, str) and v:
                            base[k] = v
        except Exception:
            pass  # bad file — fall through to base config

    return base


def resolve_model_for_role(root, role, fallback=None):
    """Return the model name for *role*, or *fallback* if not configured.

    *role* must be one of :data:`MODEL_ROLES`.
    """
    models = get_models(root)
    return models.get(role) or fallback
