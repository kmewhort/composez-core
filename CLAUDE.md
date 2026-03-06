# CLAUDE.md — Development Guide for Composez

## What This Is

An adapter layer on to of [composez-aider-fork](https://github.com/kmewhort/composez-aider-fork) that adds structured
long-form fiction editing.
`tests/composez_core/`.

## Architecture

Novel mode is a **plugin to the base composez-aider-fork base coder**.
Do not create a `NovelCoder` class.

- **`activate_novel_mode(coder)`** in `composez_core/novel_coder.py` is the
  single integration point. Anything that should apply to all coders in all
  modes (chat, architect, etc.) goes here.
- **`NovelPromptOverlay`** wraps the coder's prompts. Content prompts
  (`main_system`, `lazy_prompt`, etc.) come from the novel layer.
  Format-specific prompts (`system_reminder`, `example_messages`) **must**
  pass through to the underlying coder so every edit format works.
- **`edit_path_validator`** and **`auto_create_fnames`** propagate
  automatically when coders are cloned via `Coder.create(from_coder=...)`.
  You do not need to manually carry them over.

### Autonomy System (`aider/coders/autonomy.py`)

Edit mode (query/edit/selection) and autonomy level (direct/compose/agent)
are two orthogonal axes. Any edit mode works with any autonomy level.

#### Edit Modes x Autonomy Levels

|                  | Direct (L1)                 | Compose (L2)                        | Agent (L3)                         |
|------------------|-----------------------------|-------------------------------------|------------------------------------|
| **Query**        | Query content, get answer   | Plan an answer, then query in depth | Multi-step research via YAML plan  |
| **Edit**         | One-shot code edit          | Plan changes, then apply with editor| Multi-step edits via YAML plan     |
| **Selection**    | Replace selected text       | Plan replacement, then apply        | Multi-step with selection context  |

- **Edit modes** control *what output* the LLM produces (question
  answering, file editing, or selected-range replacement).
- **Autonomy levels** control *how many turns* the LLM gets:
  - **Direct** — single turn, one prompt and one response.
  - **Compose** — two-phase: a planning model designs changes, then an
    editor model implements them.
  - **Agent** — multi-step: the LLM produces a YAML plan of slash
    commands that are executed sequentially.

#### Implementation details

- **`AutonomyStrategy`** (direct): no-op, single-turn.
- **`ComposeStrategy`** (compose): two-phase plan→edit via `reply_completed`.
  Phase 2 uses `"query"` format when base is query, `"selection"` when base is
  selection, otherwise the model's `editor_edit_format`.
- **`AgentStrategy`** (agent): parses a YAML plan and executes via
  `AgentRunner`. Inherits the current edit format so selection context
  flows through.

`reply_completed()` must always return `True` for compose and agent
strategies to prevent `apply_updates()` from running on the planning
response.

`Coder.create()` accepts `autonomy=` and attaches the strategy +
appropriate prompts (ArchitectPrompts for compose, AgentPrompts for
agent). Legacy `edit_format="architect"` maps to `autonomy="compose"`.

### Integration with base_coder.py

The hook in `Coder.create()` uses a soft import with `try/except ImportError`
so upstream merges stay clean. Keep changes to `base_coder.py` minimal — the
only novel-specific code there should be the `activate_novel_mode` /
`activate_novel_query_mode` / `activate_novel_agent_mode` call site.

## Narrative Structure Rules

All narrative content lives under the **`novel/`** directory (hardcoded,
not configurable). The constant `NOVEL_DIR = "novel"` is defined in
`composez_core/config.py`.

```
novel/
  Act N - Title/
    *.md                    # any user-created .md files (notes, outlines)
    Chapter N - Title/
      *.md                  # any user-created .md files
      Scene N - Title/
        SUMMARY.md          # allowed
        PROSE.md            # allowed (scenes only)
```

Level names are configured in `.composez`; the defaults are
`["Act", "Chapter", "Scene"]` (see `DEFAULT_LEVELS` in
`composez_core/config.py`).

- Any `.md` file at Act and Chapter levels (user notes, outlines, etc.).
- Only `SUMMARY.md` and `PROSE.md` at the Scene (leaf) level.
- No system-generated summaries at non-leaf levels.
- `NarrativeMap.check_narrative_file()` enforces these rules via the
  `edit_path_validator` hook.
- `SUMMARY.md` and `PROSE.md` are in `auto_create_fnames` — the LLM creates
  them without prompting the user. All other new files (db entries, etc.)
  still prompt for confirmation.
- `/summarize` on a Chapter or Act drills down to all descendant
  Scenes and generates summaries for each.
- `/write` on a Chapter or Act uses descendant Scene summaries (and any
  `.md` files from parent directories) as temporary read-only context.

## Database (`db/`)

Reference material (characters, locations, items, etc.) under `db/` is
**read-only context**. The LLM should never edit db files during normal chat.
DB files are added to the chat as read-only via `/add db`.

## Testing

- Novel tests live under `tests/composez_core/`.
- **Use the project venv to run tests.** The venv lives at the main
  repo root (e.g. `<repo>/venv/bin/pytest`), not in the worktree.
  Bare `python -m pytest` and system `pytest` won't work.
- Stub coders with `Coder.__new__(Coder)` and manually set required
  attributes rather than full initialization (see `TestAutoCreateAndGroupConfirm`
  for the pattern).
- Tests that need a git repo use `GitTemporaryDirectory`; others use
  `tempfile.TemporaryDirectory`.

## Slash Commands

Defined in `composez_core/novel_commands.py`. Commands register via
`get_commands()` and support a location argument syntax:

- Keyword: `act 1 chapter 2 scene 3`
- Shorthand: `1 2 3`
- Mixed: `act 1 chapter 2 scene`

Content-generation commands (`/write`, `/summarize`) spin up temporary coder
instances via `_run_with_files()` to keep the main chat context clean.

## Documentation

**`docs/`** — our Composez Quarto documentation site (`docs/**/*.qmd`).
This is the user-facing docs for Composez. Update these when adding or
changing novel-mode features. Navigation is configured in
`docs/_quarto.yml`.

When a feature touches both layers (e.g. the autonomy system lives in
`aider/coders/autonomy.py` but is exposed via novel commands), document the
Composez-facing behavior in `docs/` and leave `aider/website/` alone.

Key Quarto doc files:

| File | What it covers |
|------|---------------|
| `docs/guide/modes.qmd` | Edit modes × autonomy levels |
| `docs/guide/agent-mode.qmd` | Agent mode deep dive |
| `docs/guide/configuration.qmd` | `.composez`, auto-context, style guide |
| `docs/guide/writing-workflow.qmd` | Write → feedback → revise loop |
| `docs/reference/commands.qmd` | Command overview table |
| `docs/reference/writing-commands.qmd` | Full command reference |
| `docs/reference/composez-config.qmd` | `.composez` field reference |

## Common Tasks

```bash
# Run all novel tests
<repo>/venv/bin/pytest tests/composez_core/ -v

# Run a specific test class
<repo>/venv/bin/pytest tests/composez_core/test_novel_coder.py -v -k "TestAutoCreate"

# Run the full test suite
<repo>/venv/bin/pytest tests/ -x --timeout=60
```
