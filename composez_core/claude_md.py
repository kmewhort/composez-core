"""
Generate a CLAUDE.md file for novel projects.

Builds the content programmatically from the same prompt constants used by
novel mode, so the instructions stay in sync with what the aider system
prompts enforce.
"""

import os
import textwrap
from pathlib import Path

from .config import DEFAULT_LEVELS, get_levels
from .novel_prompts import NovelPrompts, NovelQueryPrompts

# Filename placed at the project root.
CLAUDE_MD_FILE = "CLAUDE.md"


def _build_structure_example(levels):
    """Build the directory-structure example from the project's levels."""
    # e.g. ["Act", "Chapter", "Scene"]
    indent = "  "
    lines = ["novel/"]

    for i, level in enumerate(levels):
        prefix = indent * (i + 1)
        is_leaf = i == len(levels) - 1

        lines.append(f"{prefix}{level} N - Title/")

        if not is_leaf:
            # Non-leaf levels can have user .md files
            lines.append(f"{prefix}  *.md")
        else:
            # Leaf level: only SUMMARY.md and PROSE.md
            lines.append(f"{prefix}  SUMMARY.md")
            lines.append(f"{prefix}  PROSE.md")

    return "\n".join(lines)


def _extract_file_rules(main_system):
    """Pull the file-constraint paragraphs from NovelPrompts.main_system.

    Extracts the block starting with "IMPORTANT" up to (but not including)
    the "Do NOT add top-level headings" paragraph, so the CLAUDE.md always
    mirrors the system prompt.
    """
    lines = main_system.strip().splitlines()
    collecting = False
    collected = []
    for line in lines:
        if line.startswith("IMPORTANT"):
            collecting = True
        if collecting:
            # Stop before the heading rule or the focus rule
            if line.startswith("Do NOT add top-level") or line.startswith(
                "Focus each response"
            ):
                break
            collected.append(line)
    return "\n".join(collected).strip()


def _extract_focus_rule(main_system):
    """Pull the 'focus each response on one file type' block."""
    lines = main_system.strip().splitlines()
    collecting = False
    collected = []
    for line in lines:
        if line.startswith("Focus each response"):
            collecting = True
        if collecting:
            if line.strip() == "" and collected:
                break
            collected.append(line)
    return "\n".join(collected).strip()


def _prompt_to_bullets(prompt_text):
    """Convert a multi-line prompt string into markdown bullet points.

    Each non-empty line becomes a ``- `` bullet.
    """
    lines = textwrap.dedent(prompt_text).strip().splitlines()
    return "\n".join(f"- {line.strip()}" for line in lines if line.strip())


def _extract_heading_rule(main_system):
    """Pull the 'no top-level headings' paragraph."""
    lines = main_system.strip().splitlines()
    collected = []
    for line in lines:
        if line.startswith("Do NOT add top-level headings"):
            collected.append(line)
        elif collected:
            if line.strip() == "" or line.startswith("Focus"):
                break
            collected.append(line)
    return "\n".join(collected).strip()


def generate_claude_md(root):
    """Generate the CLAUDE.md content for a novel project.

    Pulls from ``NovelPrompts`` and ``NovelQueryPrompts`` so the instructions
    track the system prompts used by aider's novel mode.

    Parameters
    ----------
    root : str
        Project root directory (used to read levels from ``.composez``).

    Returns
    -------
    str
        The full CLAUDE.md content.
    """
    try:
        levels = get_levels(root)
    except Exception:
        levels = list(DEFAULT_LEVELS)

    prompts = NovelPrompts()
    query_prompts = NovelQueryPrompts()
    main_system = prompts.main_system.replace("{final_reminders}\n", "")

    structure = _build_structure_example(levels)
    file_rules = _extract_file_rules(main_system)
    focus_rule = _extract_focus_rule(main_system)
    heading_rule = _extract_heading_rule(main_system)

    leaf = levels[-1]
    non_leaf_names = ", ".join(levels[:-1])

    # Build the analysis criteria from NovelQueryPrompts
    query_system = query_prompts.main_system.replace("{language}", "the user's language")
    analysis_lines = []
    in_list = False
    for line in query_system.splitlines():
        if line.startswith("- ") and in_list:
            analysis_lines.append(line)
        elif "consider:" in line.lower():
            in_list = True
        elif in_list and not line.startswith("- "):
            break
    analysis_block = "\n".join(analysis_lines)

    # Assemble the document
    sections = []

    sections.append("# CLAUDE.md — Novel Project\n")

    # ── Project structure ──
    sections.append("## Project Structure\n")
    sections.append(f"""\
```
{structure}

db/
  core/                               # style guide, metadata (always read first)
  characters/                         # character sheets
  locations/                          # setting descriptions
  items/                              # important objects
  ...                                 # other reference categories
```
""")

    # ── File rules — drawn from NovelPrompts.main_system ──
    sections.append("## File Rules\n")
    sections.append(f"""\
{file_rules}

{heading_rule}

Do NOT create files outside `novel/` and `db/` unless explicitly asked.
""")

    # ── Reference database ──
    sections.append("## Reference Database (`db/`)\n")
    sections.append(f"""\
{prompts.read_only_files_prefix.strip()}

Always read `db/core/` first — it contains the style guide and project metadata.

When editing db entries:
- Maintain the existing format and structure of each entry.
- Do not reorganize db categories without being asked.
- Keep entries factual and concise — these are reference docs, not prose.
""")

    # ── Working on summaries ──
    sections.append("## Working on Summaries\n")
    sections.append(f"""\
`SUMMARY.md` files are {leaf.lower()} synopses. Each summary should:

- Start directly with the summary, no title required (titles are captured in the directory names)
- Capture key events, character dynamics, and emotional beats.
- Note important revelations, decisions, or turning points.

When updating summaries, only change `SUMMARY.md` files — do not touch
`PROSE.md` in the same pass unless explicitly asked.
""")

    # ── Working on prose — draws from lazy_prompt and overeager_prompt ──
    sections.append("## Working on Prose\n")
    sections.append(f"""\
`PROSE.md` files are the actual narrative text. When writing or editing prose:

- Read the {leaf.lower()}'s `SUMMARY.md` first to understand what should happen.
- Read sibling {leaf.lower()} summaries for context on what comes before and after.
- Read `db/core/` and follow its conventions for voice and tone.
{_prompt_to_bullets(prompts.lazy_prompt)}
{_prompt_to_bullets(prompts.overeager_prompt)}
- When writing multiple {leaf.lower()}s (e.g. an entire {levels[-2].lower() if len(levels) > 1 else "section"}), ensure smooth
  transitions so each {leaf.lower()} flows naturally from the previous one.

When editing prose, only change `PROSE.md` files — do not touch `SUMMARY.md`
in the same pass unless explicitly asked.
""")

    # ── Analyzing prose — draws from NovelQueryPrompts ──
    sections.append("## Analyzing Prose\n")
    sections.append(f"""\
When reviewing or analyzing the manuscript, consider:
{analysis_block}
Suggest changes briefly — do not rewrite full text unless asked.
""")

    # ── Workflow ──
    sections.append("## Workflow\n")
    sections.append(f"""\
For multi-{leaf.lower()} tasks, work in order:

1. **Read context first.** Read `db/core/` files, then relevant summaries,
   then the prose you will be working on.
2. **One file type at a time.** {focus_rule}
3. **Process {leaf.lower()}s in numerical order** within a {levels[-2].lower() if len(levels) > 1 else "section"}.
4. **Commit after meaningful units** — e.g. after finishing a {levels[-2].lower() if len(levels) > 1 else "section"}'s
   worth of edits, not after every sentence.
""")

    # ── Extracting text ──
    sections.append("## Extracting Text\n")
    # Build find examples using configured level names
    extract_lines = [
        f"Prose lives in `PROSE.md` files at the {leaf.lower()} (leaf) level.",
        f"Directory names embed the level name and ordering number",
        f"(e.g. `{levels[0]} 1 - Title/{levels[1]} 2 - Title/` …),",
        "so **`sort -V` (version sort)** on the full paths reproduces the",
        "correct narrative order.",
        "",
        "```bash",
        f"# Full novel — all {leaf.lower()}s in narrative order",
        "find novel/ -name PROSE.md -print0 | sort -zV | xargs -0 cat",
    ]
    if len(levels) >= 2:
        l0 = levels[0]
        extract_lines += [
            "",
            f"# A single {l0.lower()} (e.g. {l0} 2)",
            f"find novel/{l0}\\ 2*/ -name PROSE.md -print0 | sort -zV | xargs -0 cat",
        ]
    if len(levels) >= 3:
        l1 = levels[1]
        extract_lines += [
            "",
            f"# A single {l1.lower()} (e.g. {l0} 1, {l1} 3)",
            f"find novel/{l0}\\ 1*/{l1}\\ 3*/ -name PROSE.md -print0 | sort -zV | xargs -0 cat",
        ]
    extract_lines += [
        "```",
        "",
        "`-print0` / `-z` / `-0` handle spaces in directory names safely.",
        "`sort -V` is a GNU coreutils flag (Linux, or `brew install coreutils` →",
        "`gsort -V` on macOS).",
    ]
    sections.append("\n".join(extract_lines) + "\n")

    # ── Narrative hierarchy note ──
    levels_str = " > ".join(levels)
    sections.append("## Narrative Hierarchy\n")
    sections.append(f"""\
This project uses: **{levels_str}**

Non-leaf levels ({non_leaf_names}) can contain any `.md` files (notes, outlines).
Leaf level ({leaf}) directories contain only `SUMMARY.md` and `PROSE.md`.
""")

    return "\n".join(sections)


def init_claude_md(root):
    """Create ``CLAUDE.md`` in *root* if one doesn't already exist.

    Returns the path if created, or ``None`` if it already exists.
    """
    path = os.path.join(root, CLAUDE_MD_FILE)
    if os.path.isfile(path):
        return None

    content = generate_claude_md(root)
    Path(path).write_text(content, encoding="utf-8")
    return path
