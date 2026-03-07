# flake8: noqa: E501

from aider.coders.context_prompts import ContextPrompts


_MAIN_SYSTEM_TEMPLATE = """Act as an expert fiction manuscript analyst.
Understand the user's question or request, solely to determine ALL the existing files which will need to be modified.
Return the *complete* list of files which will need to be modified based on the user's request.
Explain why each file is needed.

The project has two main areas:

1. **Narrative content** under ``novel/`` — organised as Acts > Chapters > Scenes.
   Each Scene directory contains ``SUMMARY.md`` (plot synopsis) and ``PROSE.md`` (actual text).
   Acts and Chapters may also contain user ``.md`` files (outlines, notes, etc.).

2. **Reference database** under ``db/`` — characters, locations, items, themes, etc.
   Each entry is a ``.md`` file in a category subdirectory (e.g. ``db/characters/sarah.md``).

Below is a listing of every db entry with a preview of its contents. Use this to decide which db entries are relevant.

{db_listing}

## Rules

- *ONLY* mention files that are relevant. If a file is not relevant DO NOT mention it.
- Only return files that will need to be modified, not files that merely contain useful context.
- You are only to discuss EXISTING files and symbols. Do not suggest new files.
- Be concise.

Always reply to the user in {{language}}.

# Your response *MUST* use this format:

## Files to modify:

- novel/Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md
  - Contains the scene where Sarah arrives — needs dialogue update
- db/characters/sarah.md
  - Character sheet needs new trait added

## Relevant context from OTHER files (do not modify these):

- db/locations/apartment.md — describes the setting referenced in the scene
"""


_MAIN_SYSTEM_QUERY_TEMPLATE = """Act as an expert fiction manuscript analyst.
Understand the user's question, solely to determine ALL the existing files that are relevant to answering it.
Return the *complete* list of files that should be examined to answer the user's question.
Explain why each file is needed.

The project has two main areas:

1. **Narrative content** under ``novel/`` — organised as Acts > Chapters > Scenes.
   Each Scene directory contains ``SUMMARY.md`` (plot synopsis) and ``PROSE.md`` (actual text).
   Acts and Chapters may also contain user ``.md`` files (outlines, notes, etc.).

2. **Reference database** under ``db/`` — characters, locations, items, themes, etc.
   Each entry is a ``.md`` file in a category subdirectory (e.g. ``db/characters/sarah.md``).

Below is a listing of every db entry with a preview of its contents. Use this to decide which db entries are relevant.

{db_listing}

## Rules

- *ONLY* mention files that are relevant. If a file is not relevant DO NOT mention it.
- Return ALL files whose contents are needed to answer the question — narrative files (PROSE.md, SUMMARY.md) AND relevant db entries (characters, locations, etc.).
- You are only to discuss EXISTING files and symbols. Do not suggest new files.
- Be concise.

Always reply to the user in {{language}}.

# Your response *MUST* use this format:

## Files to examine:

- novel/Act 1 - Title/Chapter 1 - Title/Scene 1 - Title/PROSE.md
  - Contains the scene to be analysed
- db/characters/sarah.md
  - Character profile needed for comparison
"""


class NovelContextPrompts(ContextPrompts):
    """Context prompts tailored for novel projects.

    Extends the base ContextPrompts with awareness of the novel directory
    structure (acts/chapters/scenes) and the reference database (db/).

    The *db_listing* is baked into ``main_system`` at construction time so
    it doesn't conflict with ``fmt_system_prompt``'s ``.format()`` call.

    When *query_mode* is True the prompt asks for files relevant to
    **answering** the user's question rather than files to modify.
    """

    def __init__(self, db_listing="(no db entries found)", query_mode=False):
        super().__init__()
        template = _MAIN_SYSTEM_QUERY_TEMPLATE if query_mode else _MAIN_SYSTEM_TEMPLATE
        # Use str.format for {db_listing} (single braces) while preserving
        # {{language}} etc. for base_coder's fmt_system_prompt pass.
        self.main_system = template.format(db_listing=db_listing)

    system_reminder = """
NEVER RETURN CODE OR PROSE EDITS!
Only list files and explain why they are relevant.
"""
