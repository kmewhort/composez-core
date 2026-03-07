"""
FastAPI application for the Novel browser UI.

Provides REST endpoints for file/narrative operations and a WebSocket
for streaming chat (aider commands) back and forth.
"""

import asyncio
import ctypes
import os
import queue
import subprocess
import threading
import traceback
from collections import OrderedDict
from pathlib import Path

import yaml
from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aider.utils import IMAGE_EXTENSIONS

from aider.coders import Coder
from aider.commands import SwitchCoder
from aider.io import InputOutput
from aider.main import main as cli_main

from ..config import CONFIG_FILE, NOVEL_DIR, load_config, save_config
from ..db import Db
from ..narrative_map import NarrativeMap, natural_sort_key

app = FastAPI(title="Novel UI", version="0.1.0")


def _narrative_sort_key(name: str) -> tuple:
    """Sort key that orders special items first:

    - ``novel/`` directory first (narrative content)
    - ``SUMMARY.md`` before ``PROSE.md`` before everything else

    Used by both the file-tree sidebar and directory-content roll-up so the
    ordering is consistent everywhere.  The "everything else" bucket uses
    natural_sort_key so that e.g. "2 - Beta" sorts before "11 - Gamma".
    """
    if name == NOVEL_DIR:
        return (-1,)
    if name == NarrativeMap.SUMMARY_FILE:
        return (0,)
    if name == NarrativeMap.PROSE_FILE:
        return (1,)
    return (2, *natural_sort_key(name))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "novel_ui" / "dist"

# ---------------------------------------------------------------------------
# Global state — initialised lazily on first WebSocket connect
# ---------------------------------------------------------------------------

_coder: Coder | None = None
_narrative_map: NarrativeMap | None = None
_db: Db | None = None


def _get_coder() -> Coder:
    global _coder, _narrative_map, _db
    if _coder is None:
        coder = cli_main(return_coder=True)
        if not isinstance(coder, Coder):
            raise RuntimeError("Failed to create coder — check CLI arguments")
        _coder = coder
        root = coder.root
        _narrative_map = NarrativeMap(os.path.join(root, NOVEL_DIR), io=coder.io)
        _db = Db(root, io=coder.io)
    return _coder


def _get_narrative_map() -> NarrativeMap:
    _get_coder()  # ensure initialised
    return _narrative_map


def _get_db() -> Db:
    _get_coder()  # ensure initialised
    return _db


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class FileToggleRequest(BaseModel):
    path: str  # relative to repo root
    read_only: bool = False


class FileContentRequest(BaseModel):
    path: str


class CommandRequest(BaseModel):
    command: str


class ModelSettingsRequest(BaseModel):
    model: str | None = None


# ---------------------------------------------------------------------------
# REST: File browser
# ---------------------------------------------------------------------------


def _entry_sort_key(entry):
    """Sort key: SUMMARY.md first, PROSE.md second, then natural order."""
    return _narrative_sort_key(entry["name"])


@app.get("/api/files/tree")
def get_file_tree():
    """Return the full directory tree as nested JSON for the file browser.

    Directory nodes that correspond to narrative elements (act, chapter, scene)
    are annotated with ``kind`` and ``wordCount`` from the NarrativeMap so the
    frontend can show icons and word-count badges without a separate endpoint.
    """
    coder = _get_coder()
    root = coder.root

    editable = {os.path.relpath(f, root) for f in coder.abs_fnames}
    read_only = {os.path.relpath(f, root) for f in coder.abs_read_only_fnames}

    # Build a path → {kind, wordCount} lookup from the narrative tree
    nm = _get_narrative_map()
    nm.refresh()
    narrative_meta = {}

    def _walk_narrative(nodes):
        for node in nodes:
            rel = os.path.relpath(node.path, root)
            narrative_meta[rel] = {
                "kind": node.kind,
                "wordCount": node.total_word_count(),
            }
            if node.children:
                _walk_narrative(node.children)

    for act in nm.get_tree():
        _walk_narrative([act])

    def _count_context(entries):
        """Return (total_files, in_context_files) for a list of tree entries."""
        total = 0
        in_ctx = 0
        for e in entries:
            if e["type"] == "file":
                total += 1
                if e.get("inContext") or e.get("readOnly"):
                    in_ctx += 1
            elif e.get("children"):
                t, c = _count_context(e["children"])
                total += t
                in_ctx += c
        return total, in_ctx

    # Dotfiles to show at root level in the file sidebar
    _VISIBLE_DOTFILES = {".composez", ".vale.ini"}

    def _root_entry_sort_key(entry):
        """Sort key for root level: novel/ first, then directories, then files,
        then visible dotfiles at the bottom."""
        name = entry["name"]
        if name == NOVEL_DIR:
            return (-1,)
        if name in _VISIBLE_DOTFILES:
            return (2, *natural_sort_key(name))
        type_order = 0 if entry["type"] == "directory" else 1
        return (type_order, *natural_sort_key(name))

    def _build_tree(base_path, rel_prefix="", is_root=False):
        entries = []
        try:
            items = sorted(os.listdir(base_path), key=_narrative_sort_key)
        except OSError:
            return entries

        for name in items:
            if name.startswith("."):
                if not (is_root and name in _VISIBLE_DOTFILES):
                    continue
            full = os.path.join(base_path, name)
            rel = os.path.join(rel_prefix, name) if rel_prefix else name

            if os.path.isdir(full):
                children = _build_tree(full, rel)
                entry = {
                    "name": name,
                    "path": rel,
                    "type": "directory",
                    "children": children,
                }
                meta = narrative_meta.get(rel)
                if meta:
                    entry["kind"] = meta["kind"]
                    entry["wordCount"] = meta["wordCount"]
                total, in_ctx = _count_context(children)
                if total > 0 and in_ctx == total:
                    entry["contextState"] = "all"
                elif in_ctx > 0:
                    entry["contextState"] = "some"
                else:
                    entry["contextState"] = "none"
                entries.append(entry)
            else:
                entries.append({
                    "name": name,
                    "path": rel,
                    "type": "file",
                    "inContext": rel in editable,
                    "readOnly": rel in read_only,
                })
        sort_key = _root_entry_sort_key if is_root else _entry_sort_key
        return sorted(entries, key=sort_key)

    return {"tree": _build_tree(root, is_root=True)}


@app.post("/api/files/toggle")
def toggle_file(req: FileToggleRequest):
    """Add or remove a file (or all files in a directory) from the chat context."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, req.path)

    # Handle directories: collect all files within, then toggle them as a group
    if os.path.isdir(abs_path):
        files = []
        for dirpath, _dirnames, filenames in os.walk(abs_path):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, coder.root)
                if not rel.startswith("."):
                    files.append((full, rel))

        if not files:
            return {"error": f"No files found in directory: {req.path}"}

        editable = {os.path.relpath(f, coder.root) for f in coder.abs_fnames}
        read_only = {os.path.relpath(f, coder.root) for f in coder.abs_read_only_fnames}

        # Determine direction: only remove when ALL files in the directory are
        # already in context.  Mixed or empty → add everything.
        if req.read_only:
            all_in_ctx = all(rel in read_only for _, rel in files)
        else:
            all_in_ctx = all(rel in editable for _, rel in files)

        toggled = []
        for full, rel in files:
            if req.read_only:
                if all_in_ctx:
                    coder.abs_read_only_fnames.discard(full)
                else:
                    coder.abs_fnames.discard(full)
                    coder.abs_read_only_fnames.add(full)
            else:
                if all_in_ctx:
                    coder.abs_fnames.discard(full)
                else:
                    coder.abs_read_only_fnames.discard(full)
                    coder.abs_fnames.add(full)
            toggled.append(rel)

        action = "removed" if all_in_ctx else "added"
        if req.read_only:
            action += "_read_only"
        return {"action": action, "path": req.path, "files": toggled}

    if not os.path.isfile(abs_path):
        return {"error": f"File not found: {req.path}"}, 404

    rel = req.path
    editable = {os.path.relpath(f, coder.root) for f in coder.abs_fnames}
    read_only = {os.path.relpath(f, coder.root) for f in coder.abs_read_only_fnames}

    if req.read_only:
        if rel in read_only:
            coder.abs_read_only_fnames.discard(abs_path)
            return {"action": "removed_read_only", "path": rel}
        else:
            coder.abs_fnames.discard(abs_path)
            coder.abs_read_only_fnames.add(abs_path)
            return {"action": "added_read_only", "path": rel}
    else:
        if rel in editable:
            coder.abs_fnames.discard(abs_path)
            return {"action": "removed", "path": rel}
        else:
            coder.abs_read_only_fnames.discard(abs_path)
            coder.abs_fnames.add(abs_path)
            return {"action": "added", "path": rel}


# ---------------------------------------------------------------------------
# REST: File content
# ---------------------------------------------------------------------------


@app.get("/api/files/content")
def get_file_content(path: str):
    """Read a single file's content."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, path)
    if not os.path.isfile(abs_path):
        return {"error": "not found"}, 404
    try:
        content = Path(abs_path).read_text(encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}, 500
    return {"path": path, "content": content}


@app.post("/api/files/save")
def save_file_content(req: FileContentRequest, content: str = ""):
    """Write content to a file (from the markdown editor)."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, req.path)
    try:
        Path(abs_path).write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}, 500
    return {"saved": req.path}


class SaveFileBody(BaseModel):
    path: str
    content: str


@app.put("/api/files/content")
def put_file_content(body: SaveFileBody):
    """Save file content from the editor."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, body.path)
    if not os.path.isfile(abs_path):
        return {"error": "not found"}
    try:
        Path(abs_path).write_text(body.content, encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    return {"saved": body.path}


# ---------------------------------------------------------------------------
# REST: Image files
# ---------------------------------------------------------------------------


def _is_image_path(path: str) -> bool:
    """Check if a path refers to an image based on its extension."""
    return any(path.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)


@app.get("/api/files/image")
def get_image(path: str):
    """Serve an image file from the repo."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, path)
    if not os.path.isfile(abs_path) or not _is_image_path(path):
        return {"error": "not found or not an image"}, 404
    return FileResponse(abs_path)


@app.post("/api/files/upload-image")
async def upload_image(path: str, file: UploadFile):
    """Replace an existing image file with an uploaded one."""
    coder = _get_coder()
    abs_path = os.path.join(coder.root, path)
    if not os.path.isfile(abs_path):
        return {"error": "target file not found"}, 404
    if not _is_image_path(path):
        return {"error": "target is not an image file"}, 400
    try:
        contents = await file.read()
        Path(abs_path).write_bytes(contents)
    except Exception as e:
        return {"error": str(e)}, 500
    return {"replaced": path}


# ---------------------------------------------------------------------------
# REST: Directory content (roll-up for acts/chapters)
# ---------------------------------------------------------------------------


@app.get("/api/files/directory-content")
def get_directory_content(path: str):
    """
    Return all markdown files under a directory, rolled up with metadata.
    Used when opening an act or chapter in the content pane.
    """
    coder = _get_coder()
    abs_path = os.path.join(coder.root, path)
    if not os.path.isdir(abs_path):
        return {"error": "not a directory"}, 404

    files = []
    for dirpath, dirnames, filenames in os.walk(abs_path):
        # Sort in-place so os.walk traverses subdirs in the same order as
        # the file-tree sidebar (both use _narrative_sort_key).
        dirnames.sort(key=_narrative_sort_key)

        for fn in sorted(filenames, key=_narrative_sort_key):
            if fn.endswith(".md"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, coder.root)
                try:
                    content = Path(full).read_text(encoding="utf-8")
                except OSError:
                    content = ""
                files.append({
                    "path": rel,
                    "name": fn,
                    "parentDir": os.path.relpath(dirpath, coder.root),
                    "content": content,
                    "isSummary": fn == NarrativeMap.SUMMARY_FILE,
                    "isProse": fn == NarrativeMap.PROSE_FILE,
                })
    return {"path": path, "files": files}


# ---------------------------------------------------------------------------
# REST: DB entries
# ---------------------------------------------------------------------------


@app.get("/api/db/entries")
def get_db_entries(category: str | None = None):
    db = _get_db()
    if category:
        entries = db.get_entries_by_category(category)
    else:
        entries = db.get_entries()
    return {
        "entries": [
            {"category": e.category, "name": e.name, "path": str(e.path)}
            for e in entries
        ]
    }


@app.get("/api/db/categories")
def get_db_categories():
    db = _get_db()
    return {"categories": db.get_categories()}


# ---------------------------------------------------------------------------
# REST: Instructions
# ---------------------------------------------------------------------------


@app.get("/api/instructions")
def get_instructions():
    """Return list of instruction files with name and content."""
    coder = _get_coder()
    instructions_dir = os.path.join(coder.root, "instructions")
    if not os.path.isdir(instructions_dir):
        return {"instructions": []}
    items = []
    for fname in sorted(os.listdir(instructions_dir)):
        fpath = os.path.join(instructions_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            content = Path(fpath).read_text(encoding="utf-8")
        except OSError:
            continue
        stem = Path(fname).stem
        items.append({"name": stem, "content": content.strip()})
    return {"instructions": items}


# ---------------------------------------------------------------------------
# REST: Model settings
# ---------------------------------------------------------------------------


@app.get("/api/settings/model")
def get_model_settings():
    coder = _get_coder()
    repo_name = os.path.basename(coder.root) if coder.root else ""
    return {
        "model": str(coder.main_model),
        "editFormat": coder.edit_format,
        "repoName": repo_name,
    }


# ---------------------------------------------------------------------------
# REST: .composez (novel-mode project config)
# ---------------------------------------------------------------------------

_COMPOSEZ_SETTINGS: list[dict] = [
    {
        "key": "levels",
        "label": "Narrative levels",
        "help": "Hierarchy of narrative structure levels (e.g. Act, Chapter, Scene). Minimum 2.",
        "type": "list",
        "default": ["Act", "Chapter", "Scene"],
    },
    {
        "key": "auto_context",
        "label": "Auto-context",
        "help": "Automatically identify relevant manuscript files before each LLM call",
        "type": "bool",
        "default": True,
    },
]

# Task-specific model settings exposed via the models sub-dict.
_COMPOSEZ_MODEL_SETTINGS: list[dict] = [
    {
        "key": "admin_model",
        "label": "Admin model",
        "help": "Model for git commits, auto-context, and other admin tasks. Ideally small and fast.",
        "type": "string",
    },
    {
        "key": "query_model",
        "label": "Query model",
        "help": "Model used by direct Query mode.",
        "type": "string",
    },
    {
        "key": "edit_model",
        "label": "Edit model",
        "help": "Model used by direct Edit mode.",
        "type": "string",
    },
    {
        "key": "selection_model",
        "label": "Selection model",
        "help": "Model used by direct Selection mode.",
        "type": "string",
    },
    {
        "key": "compose_model",
        "label": "Compose model",
        "help": "Model for the planning step in Compose mode (L2). The execution step uses the ask/edit/selection model.",
        "type": "string",
    },
    {
        "key": "agent_model",
        "label": "Agent model",
        "help": "Model for orchestration in Agent mode (L3). Sub-steps use the ask/edit/selection model.",
        "type": "string",
    },
]


def _read_composez() -> dict:
    """Read the .composez config and return its contents as a dict."""
    coder = _get_coder()
    root = coder.root or os.getcwd()
    return load_config(root)


def _write_composez(data: dict, commit: bool = True):
    """Write settings to .composez and optionally git commit."""
    coder = _get_coder()
    root = coder.root or os.getcwd()
    save_config(root, data)

    if commit:
        try:
            subprocess.run(
                ["git", "add", CONFIG_FILE],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            subprocess.run(
                ["git", "commit", "-m", "Update .composez configuration"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass  # Best-effort commit


@app.get("/api/settings/composez")
def get_composez_settings():
    """Return the .composez schema and current values."""
    current = _read_composez()
    settings = []
    for s in _COMPOSEZ_SETTINGS:
        entry = {
            "key": s["key"],
            "label": s["label"],
            "help": s["help"],
            "type": s["type"],
        }
        if "default" in s:
            entry["default"] = s["default"]
        val = current.get(s["key"])
        if val is not None:
            entry["value"] = val
        settings.append(entry)

    # Task-specific model settings live under the "models" sub-dict.
    models_dict = current.get("models") or {}
    model_settings = []
    for s in _COMPOSEZ_MODEL_SETTINGS:
        entry = {
            "key": s["key"],
            "label": s["label"],
            "help": s["help"],
            "type": s["type"],
        }
        val = models_dict.get(s["key"])
        if val:
            entry["value"] = val
        model_settings.append(entry)

    return {"settings": settings, "model_settings": model_settings}


class ComposezUpdateRequest(BaseModel):
    settings: dict  # key -> value mapping


def _apply_composez_to_coder(settings: dict):
    """Apply .composez settings to the running coder instance."""
    coder = _get_coder()
    if "auto_context" in settings:
        coder._auto_context_enabled = bool(settings["auto_context"])


@app.post("/api/settings/composez")
def update_composez_settings(req: ComposezUpdateRequest):
    """Update .composez with the provided settings, apply to running session,
    and git commit."""
    from composez_core.config import MODEL_ROLES

    current = _read_composez()

    # Separate model settings from top-level settings
    model_updates = {}
    top_updates = {}
    for key, value in req.settings.items():
        if key in MODEL_ROLES:
            model_updates[key] = value
        else:
            top_updates[key] = value

    # Merge top-level settings
    for key, value in top_updates.items():
        if value is None or value == "" or value == []:
            current.pop(key, None)
        else:
            current[key] = value

    # Merge model settings into the "models" sub-dict
    if model_updates:
        models_dict = current.get("models") or {}
        for key, value in model_updates.items():
            if value is None or value == "":
                models_dict.pop(key, None)
            else:
                models_dict[key] = value
        if models_dict:
            current["models"] = models_dict
        else:
            current.pop("models", None)

    _write_composez(current, commit=True)
    _apply_composez_to_coder(current)

    return {"saved": True, "applied": True}


# ---------------------------------------------------------------------------
# REST: Config file (.aider.conf.yml)
# ---------------------------------------------------------------------------

# Settings that are useful in the YAML config.  Each entry has its yml key,
# a human-readable label, the help text, the value type, and an optional
# default.  We exclude deprecated, debug-only, and one-shot settings.
# Model-related settings (model, weak-model, editor-model, etc.) are
# intentionally excluded — in novel mode they are configured via the
# task-specific model roles in .composez instead.
_CONFIG_SETTINGS: list[dict] = [
    # -- API keys --
    {"key": "openai-api-key", "label": "OpenAI API key", "help": "Specify the OpenAI API key", "type": "string"},
    {"key": "anthropic-api-key", "label": "Anthropic API key", "help": "Specify the Anthropic API key", "type": "string"},
    {"key": "openai-api-base", "label": "OpenAI API base URL", "help": "Specify the API base URL", "type": "string"},
    {"key": "api-key", "label": "Provider API key", "help": "Set an API key for a provider (e.g. provider=key)", "type": "list"},
    {"key": "set-env", "label": "Environment variables", "help": "Set environment variables (e.g. VAR=value)", "type": "list"},
    # -- Model settings (non-model-selection) --
    {"key": "auto-accept-architect", "label": "Auto-accept compose", "help": "Automatically accept compose/architect changes", "type": "bool", "default": True},
    {"key": "reasoning-effort", "label": "Reasoning effort", "help": "Set the reasoning_effort API parameter", "type": "string"},
    {"key": "thinking-tokens", "label": "Thinking tokens", "help": "Set the thinking token budget (0 to disable)", "type": "string"},
    {"key": "timeout", "label": "API timeout", "help": "Timeout in seconds for API calls", "type": "string"},
    {"key": "max-chat-history-tokens", "label": "Max history tokens", "help": "Soft limit on tokens for chat history before summarization", "type": "string"},
    # -- Cache --
    {"key": "cache-prompts", "label": "Cache prompts", "help": "Enable caching of prompts", "type": "bool", "default": False},
    # -- Repomap --
    {"key": "map-tokens", "label": "Map tokens", "help": "Suggested number of tokens to use for repo map (0 to disable)", "type": "string"},
    {"key": "map-refresh", "label": "Map refresh", "help": "How often to refresh the repo map (auto, always, files, manual)", "type": "string"},
    # -- Output --
    {"key": "dark-mode", "label": "Dark mode", "help": "Use colors suitable for a dark terminal background", "type": "bool", "default": False},
    {"key": "light-mode", "label": "Light mode", "help": "Use colors suitable for a light terminal background", "type": "bool", "default": False},
    {"key": "stream", "label": "Streaming", "help": "Enable/disable streaming responses", "type": "bool", "default": True},
    {"key": "show-diffs", "label": "Show diffs", "help": "Show diffs when committing changes", "type": "bool", "default": False},
    # -- Git --
    {"key": "auto-commits", "label": "Auto-commits", "help": "Auto commit LLM changes", "type": "bool", "default": True},
    {"key": "dirty-commits", "label": "Dirty commits", "help": "Commit when repo is found dirty", "type": "bool", "default": True},
    {"key": "attribute-author", "label": "Attribute author", "help": "Attribute composez changes in the git author name", "type": "bool"},
    {"key": "attribute-committer", "label": "Attribute committer", "help": "Attribute composez commits in the git committer name", "type": "bool"},
    {"key": "attribute-co-authored-by", "label": "Co-authored-by", "help": "Use Co-authored-by trailer in commit messages", "type": "bool", "default": True},
    {"key": "attribute-commit-message-author", "label": "Prefix author msgs", "help": "Prefix commit messages with 'composez: ' if composez authored changes", "type": "bool", "default": False},
    {"key": "subtree-only", "label": "Subtree only", "help": "Only consider files in the current subtree of the git repository", "type": "bool", "default": False},
    # -- Fixing and committing --
    {"key": "auto-lint", "label": "Auto-lint", "help": "Automatic linting after changes", "type": "bool", "default": True},
    {"key": "lint-cmd", "label": "Lint commands", "help": "Lint commands to run for different languages (e.g. 'python: flake8')", "type": "list"},
    {"key": "test-cmd", "label": "Test command", "help": "Command to run tests", "type": "string"},
    {"key": "auto-test", "label": "Auto-test", "help": "Automatic testing after changes", "type": "bool", "default": False},
    # -- Other --
    {"key": "chat-language", "label": "Chat language", "help": "Language to use in the chat", "type": "string"},
    {"key": "commit-language", "label": "Commit language", "help": "Language to use in commit messages", "type": "string"},
    {"key": "commit-prompt", "label": "Commit prompt", "help": "Custom prompt for generating commit messages", "type": "string"},
    {"key": "detect-urls", "label": "Detect URLs", "help": "Detect and offer to add URLs to chat", "type": "bool", "default": True},
    {"key": "suggest-shell-commands", "label": "Suggest shell cmds", "help": "Enable/disable suggesting shell commands", "type": "bool", "default": True},
    {"key": "notifications", "label": "Notifications", "help": "Enable terminal bell notifications when LLM responses are ready", "type": "bool", "default": False},
    {"key": "verify-ssl", "label": "Verify SSL", "help": "Verify the SSL cert when connecting to models", "type": "bool", "default": True},
    {"key": "encoding", "label": "Encoding", "help": "Encoding for input and output", "type": "string", "default": "utf-8"},
    {"key": "line-endings", "label": "Line endings", "help": "Line endings to use when writing files (platform, lf, crlf)", "type": "string", "default": "platform"},
]


def _find_config_path() -> Path:
    """Return the path to .aider.conf.yml (in git root or cwd)."""
    coder = _get_coder()
    root = coder.root or os.getcwd()
    return Path(root) / ".aider.conf.yml"


def _read_config_yml() -> dict:
    """Read the existing .aider.conf.yml and return its contents as a dict."""
    path = _find_config_path()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config_yml(data: dict, commit: bool = True):
    """Write settings to .aider.conf.yml and optionally git commit."""
    path = _find_config_path()

    # Build YAML content — only include non-empty values
    clean = {}
    for k, v in data.items():
        if v is None or v == "":
            continue
        clean[k] = v

    text = yaml.dump(clean, default_flow_style=False, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding="utf-8")

    if commit:
        coder = _get_coder()
        root = coder.root or os.getcwd()
        try:
            subprocess.run(
                ["git", "add", ".aider.conf.yml"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            subprocess.run(
                ["git", "commit", "-m", "Update composez configuration"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass  # Best-effort commit


def _get_effective_values() -> dict:
    """Resolve effective config values from the running coder state.

    This captures values from all sources (env vars, home dir config,
    command-line args) so we can show them as placeholders even when
    the repo-local .aider.conf.yml doesn't set them.
    """
    coder = _get_coder()
    effective = {}

    # Resolve from the coder's args (set by configargparse from all sources)
    args = getattr(coder, "original_kwargs", {})

    # Map of yml key → coder/args attribute name
    # Note: model-related keys (model, weak-model, editor-model, edit-format)
    # are excluded — model selection is handled via .composez model roles.
    attr_map = {
        "auto-commits": lambda: getattr(coder, "auto_commits", True),
        "dirty-commits": lambda: getattr(coder, "dirty_commits", True),
        "stream": lambda: getattr(coder, "stream", True),
        "show-diffs": lambda: getattr(coder, "show_diffs", False),
        "auto-lint": lambda: getattr(coder, "_novel_auto_lint", True),
        "auto-test": lambda: getattr(coder, "auto_test", False),
        "test-cmd": lambda: getattr(coder, "test_cmd", "") or "",
        "suggest-shell-commands": lambda: getattr(coder, "suggest_shell_commands", True),
        "detect-urls": lambda: getattr(coder, "detect_urls", True),
        "auto-accept-architect": lambda: getattr(coder, "auto_accept_architect", True),
        "cache-prompts": lambda: getattr(coder, "add_cache_headers", False),
        "chat-language": lambda: getattr(coder, "chat_language", "") or "",
        "map-tokens": lambda: str(getattr(coder, "repo_map_tokens", "")) if getattr(coder, "repo_map_tokens", None) else "",
        "map-refresh": lambda: getattr(coder.repo_map, "refresh", "") if getattr(coder, "repo_map", None) else "",
    }
    # Git repo attributes
    repo = getattr(coder, "repo", None)
    if repo:
        attr_map.update({
            "attribute-author": lambda: getattr(repo, "attribute_author", None),
            "attribute-committer": lambda: getattr(repo, "attribute_committer", None),
            "attribute-co-authored-by": lambda: getattr(repo, "attribute_co_authored_by", True),
            "attribute-commit-message-author": lambda: getattr(repo, "attribute_commit_message_author", False),
            "subtree-only": lambda: getattr(repo, "subtree_only", False),
        })

    for key, getter in attr_map.items():
        try:
            val = getter()
            if val is not None and val != "":
                effective[key] = val
        except Exception:
            pass

    # Also read from home directory config
    home_conf = Path.home() / ".aider.conf.yml"
    if home_conf.is_file():
        try:
            text = home_conf.read_text(encoding="utf-8")
            home_data = yaml.safe_load(text)
            if isinstance(home_data, dict):
                for key in [s["key"] for s in _CONFIG_SETTINGS]:
                    if key not in effective and key in home_data:
                        effective[key] = home_data[key]
        except Exception:
            pass

    return effective


_API_KEY_FIELDS = {"openai-api-key", "anthropic-api-key", "api-key"}


def _mask_key(value):
    """Mask an API key, showing only the last 4 characters."""
    if not value:
        return ""
    s = str(value)
    if len(s) <= 4:
        return "****"
    return "*" * (len(s) - 4) + s[-4:]


@app.get("/api/settings/config")
def get_config_settings():
    """Return the config schema, current values from .aider.conf.yml,
    and effective values from all sources (for placeholders)."""
    current = _read_config_yml()
    effective = _get_effective_values()
    settings = []
    for s in _CONFIG_SETTINGS:
        entry = {
            "key": s["key"],
            "label": s["label"],
            "help": s["help"],
            "type": s["type"],
        }
        if "default" in s:
            entry["default"] = s["default"]
        val = current.get(s["key"])
        if val is not None:
            # Mask API key values so they aren't exposed in the UI
            if s["key"] in _API_KEY_FIELDS:
                entry["value"] = _mask_key(val)
            else:
                entry["value"] = val
        # Include the effective value for placeholder display
        eff = effective.get(s["key"])
        if eff is not None:
            if s["key"] in _API_KEY_FIELDS:
                entry["effective"] = _mask_key(eff)
            else:
                entry["effective"] = eff
        settings.append(entry)
    return {"settings": settings}


class ConfigUpdateRequest(BaseModel):
    settings: dict  # key -> value mapping


def _apply_config_to_coder(settings: dict):
    """Apply config settings to the running coder instance.

    Settings that map to simple coder attributes are applied directly.
    Git attribution settings are applied to the repo object.
    Model/edit-format changes are NOT handled here — those use the
    dedicated /model and /chat-mode commands which trigger SwitchCoder.
    """
    coder = _get_coder()

    # Simple bool/string attrs on the coder instance
    _CODER_ATTR_MAP = {
        "auto-commits": "auto_commits",
        "dirty-commits": "dirty_commits",
        "stream": "stream",
        "show-diffs": "show_diffs",
        "auto-lint": "_novel_auto_lint",
        "auto-test": "auto_test",
        "test-cmd": "test_cmd",
        "suggest-shell-commands": "suggest_shell_commands",
        "detect-urls": "detect_urls",
        "chat-language": "chat_language",
        "commit-language": "commit_language",
        "auto-accept-architect": "auto_accept_architect",
        "notifications": "notifications",
    }

    # Bool attrs on coder.repo (GitRepo instance)
    _REPO_ATTR_MAP = {
        "attribute-author": "attribute_author",
        "attribute-committer": "attribute_committer",
        "attribute-commit-message-author": "attribute_commit_message_author",
        "attribute-commit-message-committer": "attribute_commit_message_committer",
        "attribute-co-authored-by": "attribute_co_authored_by",
        "subtree-only": "subtree_only",
    }

    for key, value in settings.items():
        # Apply coder-level attributes
        if key in _CODER_ATTR_MAP:
            attr = _CODER_ATTR_MAP[key]
            if hasattr(coder, attr):
                setattr(coder, attr, value)

        # Apply repo-level attributes
        if key in _REPO_ATTR_MAP and getattr(coder, "repo", None):
            attr = _REPO_ATTR_MAP[key]
            if hasattr(coder.repo, attr):
                setattr(coder.repo, attr, value)

        # Cache prompts → add_cache_headers
        if key == "cache-prompts":
            coder.add_cache_headers = bool(value)

        # Map refresh strategy
        if key == "map-refresh" and getattr(coder, "repo_map", None):
            coder.repo_map.refresh = value

        # Environment variables (set-env)
        if key == "set-env" and isinstance(value, list):
            for entry in value:
                if "=" in entry:
                    env_key, env_val = entry.split("=", 1)
                    os.environ[env_key.strip()] = env_val.strip()

        # API keys → environment
        if key == "openai-api-key" and value:
            os.environ["OPENAI_API_KEY"] = value
        if key == "anthropic-api-key" and value:
            os.environ["ANTHROPIC_API_KEY"] = value


@app.post("/api/settings/config")
def update_config_settings(req: ConfigUpdateRequest):
    """Update .aider.conf.yml with the provided settings, apply them to the
    running coder instance, and git commit."""
    current = _read_config_yml()

    # Merge incoming settings — remove keys with empty/null values
    for key, value in req.settings.items():
        if value is None or value == "" or value == []:
            current.pop(key, None)
        else:
            current[key] = value

    _write_config_yml(current, commit=True)

    # Apply the merged config to the running session
    _apply_config_to_coder(current)

    return {"saved": True, "applied": True}


@app.get("/api/settings/available-models")
def get_available_models():
    """Return models from user .aider.model.metadata.json plus aliases."""
    import json5

    from aider.models import MODEL_ALIASES

    coder = _get_coder()

    # Load only user-supplied model-metadata files (not the bundled one)
    seen = set()
    models = []
    candidates = [
        os.path.join(coder.root, ".aider.model.metadata.json"),
        os.path.join(str(Path.home()), ".aider.model.metadata.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json5.loads(f.read())
                if data:
                    for name in data:
                        if name not in seen:
                            seen.add(name)
                            models.append(name)
            except Exception:
                pass

    # Include alias targets so users can pick e.g. "sonnet" → claude-sonnet-4-5
    for alias, canonical in MODEL_ALIASES.items():
        if canonical not in seen:
            seen.add(canonical)
            models.append(canonical)

    # Always include the current model at the top
    current = str(coder.main_model)
    if current and current not in seen:
        models.insert(0, current)

    # Build reverse alias map (canonical -> alias)
    aliases = {v: k for k, v in MODEL_ALIASES.items()}

    return {
        "models": models,
        "aliases": aliases,
        "current": current,
    }


# ---------------------------------------------------------------------------
# REST: Models management (auth, listing, active models, usage)
# ---------------------------------------------------------------------------


class TestAuthRequest(BaseModel):
    provider: str  # "openai" or "anthropic"
    api_key: str
    base_url: str = ""  # OpenAI-compatible base URL (optional)


def _openai_base_url():
    """Return the OpenAI-compatible base URL from config or env."""
    config = _read_config_yml()
    base = config.get("openai-api-base") or os.environ.get("OPENAI_API_BASE", "")
    if not base:
        base = "https://api.openai.com/v1"
    # Normalise: strip trailing slash
    return base.rstrip("/")


@app.post("/api/models/test-auth")
def test_auth(req: TestAuthRequest):
    """Test an API key against the given provider.

    Makes a lightweight API call (list models for OpenAI, or a tiny
    messages request for Anthropic) to verify the key is valid.
    Uses the configured openai-api-base for OpenAI-compatible providers.
    """
    provider = req.provider.lower()
    api_key = req.api_key.strip()
    if not api_key:
        return {"ok": False, "error": "API key is empty"}

    if provider == "openai":
        try:
            import httpx

            base = req.base_url.strip().rstrip("/") if req.base_url.strip() else _openai_base_url()
            resp = httpx.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True}
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err = body.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return {"ok": False, "error": err}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    elif provider == "anthropic":
        try:
            import httpx

            resp = httpx.get(
                "https://api.anthropic.com/v1/models?limit=1",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True}
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err = body.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return {"ok": False, "error": err}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"Unknown provider: {provider}"}


@app.get("/api/models/provider-models")
def get_provider_models():
    """Fetch the list of models from the active provider using the saved key.

    Respects openai-api-base for OpenAI-compatible providers.
    """
    config = _read_config_yml()
    openai_key = config.get("openai-api-key") or os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = config.get("anthropic-api-key") or os.environ.get("ANTHROPIC_API_KEY", "")

    results = []

    if openai_key:
        try:
            import httpx

            base = _openai_base_url()
            resp = httpx.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {openai_key}"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for m in data:
                    model_id = m.get("id", "")
                    if model_id:
                        # Use the openai/ prefix so litellm routes through
                        # the OpenAI provider (and honours openai-api-base).
                        prefixed = f"openai/{model_id}" if not model_id.startswith("openai/") else model_id
                        results.append({"id": prefixed, "provider": "openai"})
        except Exception:
            pass

    if anthropic_key:
        try:
            import httpx

            # Anthropic list models endpoint (paginated)
            url = "https://api.anthropic.com/v1/models?limit=100"
            headers = {
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
            }
            resp = httpx.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for m in data:
                    model_id = m.get("id", "")
                    if model_id:
                        results.append({"id": model_id, "provider": "anthropic"})
        except Exception:
            pass

    return {"models": results}


@app.get("/api/models/active")
def get_active_models():
    """Return the list of active models stored in .composez."""
    current = _read_composez()
    return {"active_models": current.get("active_models", [])}


class ActiveModelsRequest(BaseModel):
    active_models: list


@app.post("/api/models/active")
def update_active_models(req: ActiveModelsRequest):
    """Save the list of active models to .composez."""
    current = _read_composez()
    current["active_models"] = req.active_models
    _write_composez(current, commit=True)
    return {"saved": True}


@app.get("/api/models/usage")
def get_model_usage():
    """Return task-specific model assignments from .composez."""
    from composez_core.config import MODEL_ROLES, get_models

    coder = _get_coder()
    root = coder.root or os.getcwd()
    configured = get_models(root)

    return {
        "models": {role: configured.get(role, "") for role in MODEL_ROLES},
    }


class UsageUpdateRequest(BaseModel):
    models: dict  # role -> model name mapping


@app.post("/api/models/usage")
def update_model_usage(req: UsageUpdateRequest):
    """Save task-specific model assignments to .composez."""
    from composez_core.config import MODEL_ROLES

    current = _read_composez()
    models_dict = current.get("models") or {}

    for role, model_name in req.models.items():
        if role not in MODEL_ROLES:
            continue
        if model_name:
            models_dict[role] = model_name
        else:
            models_dict.pop(role, None)

    if models_dict:
        current["models"] = models_dict
    else:
        current.pop("models", None)

    _write_composez(current, commit=True)

    # Apply edit_model change to the running coder immediately.
    edit_model_name = models_dict.get("edit_model")
    if edit_model_name:
        try:
            from aider.models import Model

            model = Model(edit_model_name)
            if not model.missing_keys:
                coder = _get_coder()
                coder.main_model = model
        except Exception:
            pass  # Best-effort — takes effect on next restart regardless

    return {"saved": True}


@app.get("/api/models/auth-status")
def get_auth_status():
    """Return which providers have keys configured (masked)."""
    config = _read_config_yml()
    openai_key = config.get("openai-api-key") or os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = config.get("anthropic-api-key") or os.environ.get("ANTHROPIC_API_KEY", "")

    def _mask(key):
        if not key:
            return ""
        if len(key) <= 4:
            return "****"
        return "*" * (len(key) - 4) + key[-4:]

    openai_base = config.get("openai-api-base") or os.environ.get("OPENAI_API_BASE", "")

    return {
        "openai": {"configured": bool(openai_key), "masked_key": _mask(openai_key), "base_url": openai_base},
        "anthropic": {"configured": bool(anthropic_key), "masked_key": _mask(anthropic_key)},
    }


@app.get("/api/settings/edit-formats")
def get_edit_formats():
    """Return available edit formats/modes."""
    from aider import coders

    formats = []

    # Edit modes (what kind of output the LLM produces)
    edit_modes = OrderedDict([
        ("query", "Query content without making changes"),
        ("edit", "Ask for changes (using the best edit format)"),
        ("selection", "Edit a selected range of text"),
    ])
    for key, desc in edit_modes.items():
        formats.append({"key": key, "label": key.capitalize(), "description": desc, "isHighLevel": True, "axis": "mode"})

    # Autonomy levels (orchestration pattern)
    autonomy_levels = OrderedDict([
        ("direct", "Single turn — one prompt, one response"),
        ("compose", "Use a compose model to design changes, and an editor to make them"),
        ("agent", "Plan and execute multi-step tasks using an orchestrating agent"),
    ])
    for key, desc in autonomy_levels.items():
        formats.append({"key": key, "label": key.capitalize(), "description": desc, "isHighLevel": True, "axis": "autonomy"})

    # Raw edit formats
    hidden = {"query", "edit", "compose", "code", "architect", "agent", "help", "selection", "context"}
    for coder_cls in coders.__all__:
        ef = getattr(coder_cls, "edit_format", None)
        if ef and ef not in hidden:
            doc = coder_cls.__doc__.strip().split("\n")[0] if coder_cls.__doc__ else ""
            formats.append({"key": ef, "label": ef, "description": doc, "isHighLevel": False, "axis": "mode"})

    coder = _get_coder()
    autonomy_name = getattr(getattr(coder, "autonomy_strategy", None), "name", "direct")
    return {"formats": formats, "current": coder.edit_format, "autonomy": autonomy_name}


# ---------------------------------------------------------------------------
# WebSocket: Chat / Command interface
# ---------------------------------------------------------------------------


class WebSocketIO(InputOutput):
    """Custom IO that routes output over a WebSocket."""

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        super().__init__(pretty=False, yes=True)
        self._ws = ws
        self._loop = loop
        self._input_queue = queue.Queue()

    def _send(self, msg_type: str, text: str):
        asyncio.run_coroutine_threadsafe(
            self._ws.send_json({"type": msg_type, "text": text}),
            self._loop,
        )

    def _send_json(self, payload: dict):
        asyncio.run_coroutine_threadsafe(
            self._ws.send_json(payload),
            self._loop,
        )

    def tool_output(self, msg="", log_only=False, bold=False):
        if not log_only:
            self._send("tool_output", str(msg))

    def tool_error(self, msg="", strip=True):
        self._send("tool_error", str(msg))

    def tool_warning(self, msg="", strip=True):
        self._send("tool_warning", str(msg))

    def prompt_ask(self, question):
        """Send a question to the browser and block until user responds.

        No timeout — the user may take as long as they need to answer.
        """
        self._send_json({"type": "agent_ask_user", "question": str(question)})
        return self._input_queue.get()

    def provide_input(self, text):
        """Called from the WS receive loop when the user responds."""
        self._input_queue.put(text)

    def agent_event(self, event_type, data=None):
        """Send a structured agent event to the browser."""
        payload = {"type": f"agent_{event_type}"}
        if data:
            payload.update(data)
        self._send_json(payload)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_event_loop()

    try:
        coder = _get_coder()
    except Exception as e:
        await ws.send_json({"type": "error", "text": str(e)})
        await ws.close()
        return

    # Send initial state
    repo_name = os.path.basename(coder.root) if coder.root else ""
    autonomy_name = getattr(getattr(coder, "autonomy_strategy", None), "name", "direct")
    auto_ctx = getattr(coder, "_auto_context_enabled", True)
    auto_lint = getattr(coder, "_novel_auto_lint", True)
    await ws.send_json({
        "type": "init",
        "model": str(coder.main_model),
        "editFormat": coder.edit_format,
        "autonomy": autonomy_name,
        "autoContext": auto_ctx,
        "autoLint": auto_lint,
        "repoName": repo_name,
    })

    ws_io = WebSocketIO(ws, loop)
    _chat_task = None  # Track the running chat asyncio task for interrupt
    _chat_thread_id = [None]  # Track the worker thread id for interrupt (mutable container)

    def _on_chat_done(task):
        nonlocal _chat_task
        _chat_task = None
        _chat_thread_id[0] = None

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            # Always read the current coder (may have been switched by a command)
            coder = _coder

            # --- Messages that are always accepted (even during chat) ---

            if msg_type == "interrupt":
                if _chat_task is not None and not _chat_task.done():
                    _chat_task.cancel()
                    # Also raise KeyboardInterrupt in the worker thread if
                    # it's running a blocking coder.run_stream() call.
                    tid = _chat_thread_id[0]
                    if tid is not None:
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(tid),
                            ctypes.py_object(KeyboardInterrupt),
                        )
                continue

            if msg_type == "user_input_response":
                text = data.get("text", "")
                ws_io.provide_input(text)
                continue

            if msg_type == "get_completions":
                partial = data.get("text", "")
                completions = _get_completions(coder, partial)
                await ws.send_json({
                    "type": "completions",
                    "items": completions,
                })
                continue

            # --- Messages only processed when no chat is running ---

            if _chat_task is not None and not _chat_task.done():
                # Chat is in progress — ignore non-urgent messages
                continue

            if msg_type == "chat":
                prompt = data.get("text", "").strip()
                if not prompt:
                    continue

                # Check if this is a slash command
                if prompt.startswith("/"):
                    await _handle_command(ws, coder, prompt)
                else:
                    _chat_task = asyncio.create_task(
                        _handle_chat(ws, coder, prompt, loop, ws_io, _chat_thread_id)
                    )
                    _chat_task.add_done_callback(_on_chat_done)

            elif msg_type == "model":
                new_model = data.get("model", "")
                if new_model:
                    await _handle_model_change(ws, coder, new_model)

            elif msg_type == "paste_response":
                response_text = data.get("text", "").strip()
                if response_text:
                    await _handle_paste_response(ws, coder, response_text)

            elif msg_type == "deselect":
                # Exit selection mode — switch back to the default edit format
                try:
                    edit_format = coder.main_model.edit_format if hasattr(coder, 'main_model') else "code"
                    _apply_switch_coder(
                        coder,
                        SwitchCoder(edit_format=edit_format, summarize_from_coder=False),
                    )
                    desel_autonomy = getattr(
                        getattr(_coder, "autonomy_strategy", None), "name", "direct"
                    )
                    await ws.send_json({
                        "type": "mode_changed",
                        "editFormat": _coder.edit_format,
                        "autonomy": desel_autonomy,
                        "autoContext": getattr(_coder, "_auto_context_enabled", True),
                        "autoLint": getattr(_coder, "_novel_auto_lint", True),
                    })
                except Exception as e:
                    await ws.send_json({"type": "error", "text": f"Deselect failed: {e}"})

            elif msg_type == "toggle_auto_context":
                new_val = not getattr(coder, "_auto_context_enabled", True)
                coder._auto_context_enabled = new_val
                await ws.send_json({
                    "type": "auto_context_changed",
                    "autoContext": new_val,
                })

            elif msg_type == "toggle_auto_lint":
                new_val = not getattr(coder, "_novel_auto_lint", True)
                coder._novel_auto_lint = new_val
                await ws.send_json({
                    "type": "auto_lint_changed",
                    "autoLint": new_val,
                })

    except WebSocketDisconnect:
        # Cancel any running chat task and interrupt its worker thread
        if _chat_task is not None and not _chat_task.done():
            _chat_task.cancel()
            tid = _chat_thread_id[0]
            if tid is not None:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(tid),
                    ctypes.py_object(KeyboardInterrupt),
                )
            # Also unblock any pending prompt_ask queue wait
            ws_io.provide_input("")
    except Exception as e:
        try:
            await ws.send_json({
                "type": "error",
                "text": f"Server error: {traceback.format_exc()}",
            })
        except Exception:
            pass


def _build_copy_context_markdown(coder: Coder, args: str = "") -> str:
    """Build a context transcript using the shared novel helpers.

    Supported *args* values:
    - ``"continue"`` — only the live portion (chat_files, cur, reminder)
    - ``"human"``    — lightweight file-only context (original upstream style)
    - anything else  — full LLM transcript, with *args* as extra instruction
    """
    from composez_core.novel_commands import build_copy_context_human, build_copy_context_markdown

    keyword = args.strip().lower()
    if keyword == "continue":
        return build_copy_context_markdown(coder, continue_only=True)
    if keyword == "human":
        return build_copy_context_human(coder)

    extra = args.strip()
    return build_copy_context_markdown(coder, continue_only=False, extra=extra)


def _apply_switch_coder(coder: Coder, switch: SwitchCoder) -> Coder:
    """Apply a SwitchCoder exception to create and install a new coder."""
    global _coder

    kwargs = dict(io=coder.io, from_coder=coder)
    kwargs.update(switch.kwargs)
    if "show_announcements" in kwargs:
        del kwargs["show_announcements"]

    # Extract selection-mode state before passing to Coder.create()
    selection_attrs = {}
    for key in ("selection_filename", "selection_range", "selection_text"):
        if key in kwargs:
            selection_attrs[key] = kwargs.pop(key)

    new_coder = Coder.create(**kwargs)

    # Apply selection state to the new coder
    for key, val in selection_attrs.items():
        setattr(new_coder, key, val)

    _coder = new_coder
    return new_coder


async def _handle_command(ws: WebSocket, coder: Coder, prompt: str):
    """Execute a slash command and send results over the WebSocket."""
    from aider.commands import SwitchCoder

    # Intercept /copy-context — build markdown and send to browser instead
    # of trying to use pyperclip (which won't work server-side for a browser).
    cmd_name = prompt.split()[0].lower() if prompt else ""
    normalised = cmd_name.replace("-", "_")
    if normalised.startswith("/copy_context") or "/copy_context".startswith(normalised):
        # Only intercept if the prefix is unambiguous (at least "/copy-" or "/copy_")
        if len(normalised) >= len("/copy_"):
            args = prompt[len(cmd_name):].strip()
            keyword = args.lower()
            markdown = _build_copy_context_markdown(coder, args)
            # Tell the UI which mode produced this markdown
            mode = "continue" if keyword == "continue" else (
                "human" if keyword == "human" else "full"
            )
            await ws.send_json({
                "type": "copy_context",
                "markdown": markdown,
                "mode": mode,
            })
            return

    await ws.send_json({"type": "command_start", "command": prompt})

    # Capture output from the command
    old_io = coder.commands.io
    lines = []

    class CaptureIO(InputOutput):
        def tool_output(self, msg="", log_only=False, bold=False):
            if not log_only:
                lines.append(("output", str(msg)))

        def tool_error(self, msg="", strip=True):
            lines.append(("error", str(msg)))

        def tool_warning(self, msg="", strip=True):
            lines.append(("warning", str(msg)))

        def ai_output(self, content):
            if content and content.strip():
                lines.append(("output", content.strip()))

        def assistant_output(self, message, pretty=None):
            if message and message.strip():
                lines.append(("output", message.strip()))

        def confirm_ask(self, question, default="y", subject=None, explicit_yes_required=False,
                        group=None, allow_never=False):
            return True

    capture_io = CaptureIO(pretty=False, yes=True)
    coder.commands.io = capture_io

    # Sync IO on cached novel commands so their self.io uses CaptureIO too
    old_novel_io = None
    nc = getattr(coder.commands, '_novel_commands', None)
    if nc:
        old_novel_io = nc.io
        nc.io = capture_io

    try:
        coder.commands.run(prompt)
    except SwitchCoder as switch:
        # Handle coder switch (mode change)
        new_coder = _apply_switch_coder(coder, switch)
        # Restore IO before sending messages
        coder.commands.io = old_io
        if old_novel_io is not None:
            nc.io = old_novel_io
        for kind, text in lines:
            await ws.send_json({"type": f"command_{kind}", "text": text})
        # Build mode_changed payload
        new_autonomy = getattr(
            getattr(new_coder, "autonomy_strategy", None), "name", "direct"
        )
        payload = {
            "type": "mode_changed",
            "editFormat": new_coder.edit_format,
            "autonomy": new_autonomy,
            "autoContext": getattr(new_coder, "_auto_context_enabled", True),
            "autoLint": getattr(new_coder, "_novel_auto_lint", True),
        }
        # Include selection info if switching to selection mode
        if hasattr(new_coder, "selection_filename") and new_coder.selection_filename:
            sel_range = getattr(new_coder, "selection_range", None)
            if sel_range:
                payload["selection"] = {
                    "filePath": new_coder.selection_filename,
                    "startLine": sel_range["start"]["line"] + 1,
                    "startCol": sel_range["start"]["character"] + 1,
                    "endLine": sel_range["end"]["line"] + 1,
                    "endCol": sel_range["end"]["character"] + 1,
                }
        await ws.send_json(payload)
        await ws.send_json({"type": "command_end", "command": prompt})
        return
    except Exception as e:
        lines.append(("error", str(e)))
    finally:
        coder.commands.io = old_io
        if old_novel_io is not None:
            nc.io = old_novel_io

    for kind, text in lines:
        await ws.send_json({"type": f"command_{kind}", "text": text})

    await ws.send_json({"type": "command_end", "command": prompt})


async def _handle_chat(ws: WebSocket, coder: Coder, prompt: str, loop,
                       ws_io: "WebSocketIO" = None, thread_id_ref: list = None):
    """Send a chat message to the LLM and stream the response."""
    await ws.send_json({"type": "chat_start"})

    try:
        # Run the coder in a thread to avoid blocking
        def _run():
            # Record the worker thread id so the interrupt handler can target it
            if thread_id_ref is not None:
                thread_id_ref[0] = threading.current_thread().ident

            coder.stream = True
            coder.yield_stream = True
            coder.pretty = False

            # Install WebSocketIO so tool_output / tool_error / tool_warning
            # messages (lint results, edit confirmations, etc.) reach the browser.
            old_io = coder.io
            if ws_io is not None:
                # Preserve existing settings on ws_io
                ws_io.yes = True
                coder.io = ws_io

            # In web context, auto-approve confirmations (e.g. agent plan
            # execution) since interactive prompts would block forever.
            old_yes = old_io.yes
            old_io.yes = True

            try:
                chunks = []
                for chunk in coder.run_stream(prompt):
                    chunks.append(chunk)
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "chat_chunk", "text": chunk}),
                        loop,
                    ).result(timeout=5)
                return "".join(chunks)
            finally:
                old_io.yes = old_yes
                coder.io = old_io
                if thread_id_ref is not None:
                    thread_id_ref[0] = None

        full_response = await asyncio.to_thread(_run)

        # Send edit info if files were changed
        if coder.aider_edited_files:
            await ws.send_json({
                "type": "files_edited",
                "files": sorted(coder.aider_edited_files),
            })

        if coder.last_aider_commit_hash:
            await ws.send_json({
                "type": "commit",
                "hash": coder.last_aider_commit_hash,
                "message": coder.last_aider_commit_message or "",
            })

        # If in selection mode and the selection was updated after replacement,
        # send the new coordinates so the client can re-highlight
        if (
            coder.edit_format == "selection"
            and coder.aider_edited_files
            and hasattr(coder, "selection_range")
            and coder.selection_range
        ):
            sel_range = coder.selection_range
            await ws.send_json({
                "type": "selection_updated",
                "selection": {
                    "filePath": coder.selection_filename,
                    "startLine": sel_range["start"]["line"] + 1,
                    "startCol": sel_range["start"]["character"] + 1,
                    "endLine": sel_range["end"]["line"] + 1,
                    "endCol": sel_range["end"]["character"] + 1,
                },
            })

    except asyncio.CancelledError:
        pass  # Interrupted — still send chat_end below
    except Exception as e:
        await ws.send_json({
            "type": "chat_error",
            "text": f"Error: {traceback.format_exc()}",
        })
    finally:
        if thread_id_ref is not None:
            thread_id_ref[0] = None

    await ws.send_json({"type": "chat_end"})


async def _handle_paste_response(ws: WebSocket, coder: Coder, response_text: str):
    """Apply a pasted LLM response as if the model returned it."""
    from composez_core.novel_commands import apply_pasted_response

    await ws.send_json({"type": "chat_start"})

    try:
        def _run():
            apply_pasted_response(coder, response_text)

        await asyncio.to_thread(_run)

        if coder.aider_edited_files:
            await ws.send_json({
                "type": "files_edited",
                "files": sorted(coder.aider_edited_files),
            })

        if coder.last_aider_commit_hash:
            await ws.send_json({
                "type": "commit",
                "hash": coder.last_aider_commit_hash,
                "message": coder.last_aider_commit_message or "",
            })

    except Exception as e:
        await ws.send_json({
            "type": "chat_error",
            "text": f"Error applying pasted response: {traceback.format_exc()}",
        })

    await ws.send_json({"type": "chat_end"})


async def _handle_model_change(ws: WebSocket, coder: Coder, new_model: str):
    """Change the active model."""
    try:
        from aider.models import Model
        model = Model(new_model)
        coder.main_model = model
        await ws.send_json({
            "type": "model_changed",
            "model": str(model),
        })
    except Exception as e:
        await ws.send_json({
            "type": "error",
            "text": f"Failed to change model: {e}",
        })


def _get_completions(coder: Coder, partial: str) -> list[str]:
    """Return tab-completion suggestions for the console."""
    completions = []

    if partial.startswith("/"):
        # Complete slash commands
        cmd_part = partial[1:]
        if hasattr(coder, "commands"):
            for name in dir(coder.commands):
                if name.startswith("cmd_"):
                    cmd_name = "/" + name[4:]
                    if cmd_name.startswith("/" + cmd_part):
                        completions.append(cmd_name)
            # Also check novel commands
            if hasattr(coder.commands, "novel_commands"):
                for name in dir(coder.commands.novel_commands):
                    if name.startswith("cmd_"):
                        cmd_name = "/" + name[4:]
                        if cmd_name.startswith("/" + cmd_part):
                            completions.append(cmd_name)
    else:
        # Complete filenames
        try:
            all_files = coder.get_all_relative_files()
            for f in all_files:
                if f.startswith(partial) or partial.lower() in f.lower():
                    completions.append(f)
        except Exception:
            pass

    return sorted(set(completions))[:20]


# ---------------------------------------------------------------------------
# REST: Git operations (for change review)
# ---------------------------------------------------------------------------


class ReviewCommitBody(BaseModel):
    message: str
    files: list[dict]  # [{path: str, content: str}]


@app.get("/api/git/log")
def get_git_log(limit: int = 50):
    """Return recent git commits for the review dropdown."""
    coder = _get_coder()
    root = coder.root
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={min(limit, 200)}",
             "--pretty=format:%H%x00%h%x00%s%x00%ai%x00%an"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "commits": []}

        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) >= 5:
                commits.append({
                    "hash": parts[0],
                    "short": parts[1],
                    "message": parts[2],
                    "date": parts[3],
                    "author": parts[4],
                })
        return {"commits": commits}
    except Exception as e:
        return {"error": str(e), "commits": []}


@app.get("/api/git/file-at-commit")
def get_file_at_commit(path: str, commit: str):
    """Return a file's content at a specific commit."""
    coder = _get_coder()
    root = coder.root
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            # File may not exist at that commit
            return {"path": path, "content": "", "exists": False}
        return {"path": path, "content": result.stdout, "exists": True}
    except Exception as e:
        return {"error": str(e), "content": "", "exists": False}


@app.get("/api/git/diff-files")
def get_diff_files(commit: str, path_prefix: str = ""):
    """Return list of files changed between a commit and HEAD,
    optionally filtered by path prefix (for reviewing a single act/chapter/scene)."""
    coder = _get_coder()
    root = coder.root
    try:
        cmd = ["git", "diff", "--name-only", commit, "HEAD"]
        if path_prefix:
            cmd.append("--")
            cmd.append(path_prefix)
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "files": []}

        files = [f for f in result.stdout.strip().split("\n") if f]
        return {"files": files, "commit": commit}
    except Exception as e:
        return {"error": str(e), "files": []}


@app.post("/api/git/review-commit")
def create_review_commit(body: ReviewCommitBody):
    """Write reviewed file contents and create a new commit."""
    coder = _get_coder()
    root = coder.root
    try:
        # Write each file
        for f in body.files:
            abs_path = os.path.join(root, f["path"])
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_text(f["content"], encoding="utf-8")

        # Stage the files
        paths = [f["path"] for f in body.files]
        subprocess.run(
            ["git", "add"] + paths,
            cwd=root, capture_output=True, text=True, timeout=10,
        )

        # Check if there are staged changes
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True, timeout=10,
        )
        if diff_result.returncode == 0:
            return {"committed": False, "message": "No changes to commit"}

        # Create the commit
        result = subprocess.run(
            ["git", "commit", "-m", body.message],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "committed": False}

        # Get the new commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""

        return {
            "committed": True,
            "hash": commit_hash,
            "message": body.message,
        }
    except Exception as e:
        return {"error": str(e), "committed": False}


# ---------------------------------------------------------------------------
# REST: Git operations (extended — for the Git tab)
# ---------------------------------------------------------------------------


@app.get("/api/git/branches")
def get_git_branches():
    """Return all local and remote branches plus the current branch."""
    coder = _get_coder()
    root = coder.root
    try:
        # Current branch
        cur = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        current = cur.stdout.strip() if cur.returncode == 0 else ""

        # Local branches
        local = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        local_branches = [b for b in local.stdout.strip().split("\n") if b]

        # Remote branches
        remote = subprocess.run(
            ["git", "branch", "-r", "--format=%(refname:short)"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        remote_branches = [b for b in remote.stdout.strip().split("\n") if b]

        return {
            "current": current,
            "local": local_branches,
            "remote": remote_branches,
        }
    except Exception as e:
        return {"error": str(e), "current": "", "local": [], "remote": []}


@app.get("/api/git/status")
def get_git_status():
    """Return working tree status: staged, modified, and untracked files."""
    coder = _get_coder()
    root = coder.root
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "files": []}

        files = []
        # --porcelain -z uses NUL separator
        entries = result.stdout.split("\x00")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if not entry:
                i += 1
                continue
            index_status = entry[0]
            worktree_status = entry[1]
            filepath = entry[3:]

            # Renames have a second path entry
            old_path = None
            if index_status == "R":
                i += 1
                if i < len(entries):
                    old_path = entries[i]

            status = "unknown"
            area = "unknown"
            if index_status == "?" and worktree_status == "?":
                status = "untracked"
                area = "untracked"
            elif index_status == "A":
                status = "added"
                area = "staged"
            elif index_status == "D":
                status = "deleted"
                area = "staged"
            elif index_status == "R":
                status = "renamed"
                area = "staged"
            elif index_status == "M":
                status = "modified"
                area = "staged"
            elif index_status == " " and worktree_status == "M":
                status = "modified"
                area = "unstaged"
            elif index_status == " " and worktree_status == "D":
                status = "deleted"
                area = "unstaged"
            elif index_status == "M" and worktree_status == "M":
                # Modified in both index and worktree — report both
                files.append({
                    "path": filepath,
                    "status": "modified",
                    "area": "staged",
                })
                files.append({
                    "path": filepath,
                    "status": "modified",
                    "area": "unstaged",
                })
                i += 1
                continue

            files.append({
                "path": filepath,
                "status": status,
                "area": area,
                **({"oldPath": old_path} if old_path else {}),
            })
            i += 1

        return {"files": files}
    except Exception as e:
        return {"error": str(e), "files": []}


@app.get("/api/git/graph")
def get_git_graph(limit: int = 80, all_branches: bool = False):
    """Return commit graph data with parent relationships for visualization.

    Each commit includes its hash, short hash, message, author, date,
    parent hashes, and any branch/tag refs pointing to it.
    """
    coder = _get_coder()
    root = coder.root
    try:
        cmd = [
            "git", "log",
            f"--max-count={min(limit, 300)}",
            "--pretty=format:%H%x00%h%x00%s%x00%ai%x00%an%x00%P%x00%D",
        ]
        if all_branches:
            cmd.append("--all")
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "commits": []}

        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) < 7:
                continue
            parents = parts[5].split() if parts[5] else []
            refs_raw = parts[6].strip() if parts[6] else ""
            refs = [r.strip() for r in refs_raw.split(",") if r.strip()] if refs_raw else []
            commits.append({
                "hash": parts[0],
                "short": parts[1],
                "message": parts[2],
                "date": parts[3],
                "author": parts[4],
                "parents": parents,
                "refs": refs,
            })
        return {"commits": commits}
    except Exception as e:
        return {"error": str(e), "commits": []}


@app.get("/api/git/stash-list")
def get_git_stash_list():
    """Return list of stash entries."""
    coder = _get_coder()
    root = coder.root
    try:
        result = subprocess.run(
            ["git", "stash", "list", "--pretty=format:%gd%x00%s%x00%ai"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "stashes": []}

        stashes = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) >= 3:
                stashes.append({
                    "ref": parts[0],
                    "message": parts[1],
                    "date": parts[2],
                })
        return {"stashes": stashes}
    except Exception as e:
        return {"error": str(e), "stashes": []}


@app.get("/api/git/diff-detail")
def get_git_diff_detail(path: str, staged: bool = False):
    """Return the diff for a single file (staged or unstaged)."""
    coder = _get_coder()
    root = coder.root
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        cmd.append("--")
        cmd.append(path)
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=10,
        )
        return {"path": path, "diff": result.stdout, "staged": staged}
    except Exception as e:
        return {"error": str(e), "path": path, "diff": ""}


@app.get("/api/git/remotes")
def get_git_remotes():
    """Return list of configured git remotes."""
    coder = _get_coder()
    root = coder.root
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "remotes": []}

        remotes = {}
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                url = parts[1]
                if name not in remotes:
                    remotes[name] = url
        return {"remotes": [{"name": k, "url": v} for k, v in remotes.items()]}
    except Exception as e:
        return {"error": str(e), "remotes": []}


# ---------------------------------------------------------------------------
# REST: Import
# ---------------------------------------------------------------------------


@app.post("/api/import")
async def import_project(file: UploadFile, format: str = "novelcrafter"):
    """Import a novel from an uploaded file.

    Parameters
    ----------
    file : UploadFile
        The uploaded file — a zip for Novelcrafter or a .md for markdown.
    format : str
        ``"novelcrafter"`` (default) or ``"markdown"``.
    """
    import shutil
    import tempfile

    from ..importer import MarkdownImporter, NovelcrafterImporter

    coder = _get_coder()
    root = coder.root

    # Save upload to a temp file
    suffix = Path(file.filename).suffix if file.filename else ""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        contents = await file.read()
        tmp.write(contents)
        tmp.close()

        # Remove old narrative and db directories before importing
        novel_dir = os.path.join(root, NOVEL_DIR)
        if os.path.isdir(novel_dir):
            shutil.rmtree(novel_dir)
        if format != "markdown":
            db_dir = os.path.join(root, "db")
            if os.path.isdir(db_dir):
                shutil.rmtree(db_dir)

        if format == "markdown":
            importer = MarkdownImporter(source=tmp.name, dest=root)
        else:
            importer = NovelcrafterImporter(source=tmp.name, dest=root)

        summary = importer.run()

        # Git add + commit the imported files
        try:
            subprocess.run(
                ["git", "add", "-A", "--", NOVEL_DIR, "db"],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Import novel ({format})"],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass  # Best-effort commit

        # Refresh narrative map
        global _narrative_map
        _narrative_map = NarrativeMap(
            os.path.join(root, NOVEL_DIR), io=coder.io
        )

        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@app.get("/api/export")
async def export_project(format: str = "markdown"):
    """Export the narrative as a downloadable file.

    Parameters
    ----------
    format : str
        ``"markdown"``, ``"docx"``, or ``"epub"``.
    """
    import tempfile

    from ..exporter import export_docx, export_epub, export_markdown

    coder = _get_coder()
    root = coder.root

    nmap = _get_narrative_map()
    tree = nmap.get_tree()
    if not tree:
        return {"ok": False, "error": "Nothing to export — no narrative structure found."}

    # Read title/author from db/core/metadata.yml
    title, author = "Untitled", "Unknown"
    meta_path = os.path.join(root, "db", "core", "metadata.yml")
    if os.path.isfile(meta_path):
        data = yaml.safe_load(Path(meta_path).read_text(encoding="utf-8")) or {}
        title = data.get("title", title)
        author = data.get("author", author)

    ext_map = {"markdown": ".md", "docx": ".docx", "epub": ".epub"}
    media_map = {
        "markdown": "text/markdown",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "epub": "application/epub+zip",
    }

    if format not in ext_map:
        return {"ok": False, "error": f"Unknown format: {format}. Use markdown, docx, or epub."}

    suffix = ext_map[format]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()

    try:
        if format == "markdown":
            export_markdown(tree, tmp.name)
        elif format == "docx":
            export_docx(tree, tmp.name)
        elif format == "epub":
            export_epub(tree, tmp.name, title=title, author=author)
    except ImportError as e:
        os.unlink(tmp.name)
        pkg = "python-docx" if format == "docx" else "ebooklib"
        return {"ok": False, "error": f"Missing dependency: {e}. Install with: pip install {pkg}"}
    except Exception as e:
        os.unlink(tmp.name)
        return {"ok": False, "error": str(e)}

    import re
    from datetime import datetime

    # Build filename: sanitized title + datetime
    safe_title = re.sub(r'[^\w\s-]', '', title).strip() or "export"
    safe_title = re.sub(r'\s+', '_', safe_title)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_title}_{timestamp}{suffix}"

    return FileResponse(
        tmp.name,
        media_type=media_map[format],
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Static file serving (MUST be after all API routes)
# ---------------------------------------------------------------------------
# In production, serve the built Vue frontend from novel_ui/dist.
# In development, Vite's dev server proxies /api and /ws to us.

if _STATIC_DIR.is_dir():
    @app.get("/")
    async def _serve_index():
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="static-assets")

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        file = _STATIC_DIR / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(_STATIC_DIR / "index.html")
