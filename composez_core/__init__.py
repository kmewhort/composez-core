import os
from pathlib import Path


def _ensure_gitignore(root):
    """Create or update .gitignore with novel-specific exclusions."""
    gitignore_path = os.path.join(root, ".gitignore")
    needed = [".aider*", "cache/", ".vale-styles/"]
    existing_lines = []

    if os.path.isfile(gitignore_path):
        existing_lines = Path(gitignore_path).read_text(encoding="utf-8").splitlines()

    missing = [entry for entry in needed if entry not in existing_lines]
    if not missing:
        return False

    with open(gitignore_path, "a", encoding="utf-8") as f:
        # Add a newline separator if the file already has content
        if existing_lines and existing_lines[-1] != "":
            f.write("\n")
        for entry in missing:
            f.write(entry + "\n")
    return True


def _generate_placeholder_cover(dest_path, title):
    """Create a simple placeholder cover JPG with the project title."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 600, 900
    img = Image.new("RGB", (width, height), color=(30, 30, 40))
    draw = ImageDraw.Draw(img)

    # Draw a subtle border
    border = 20
    draw.rectangle(
        [border, border, width - border, height - border],
        outline=(120, 100, 80),
        width=2,
    )

    # Try to find a reasonable font size that fits the width
    max_text_width = width - border * 4
    font_size = 48
    font = None
    while font_size >= 16:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", font_size)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("Times New Roman.ttf", font_size)
            except (OSError, IOError):
                font = ImageFont.load_default()
                break
        bbox = draw.textbbox((0, 0), title, font=font)
        if bbox[2] - bbox[0] <= max_text_width:
            break
        font_size -= 2

    # Centre the title vertically (upper third)
    bbox = draw.textbbox((0, 0), title, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = height // 3 - th // 2
    draw.text((x, y), title, fill=(220, 200, 160), font=font)

    # Small "A Novel" subtitle
    sub_font_size = max(font_size // 3, 14)
    try:
        sub_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", sub_font_size
        )
    except (OSError, IOError):
        try:
            sub_font = ImageFont.truetype("Times New Roman.ttf", sub_font_size)
        except (OSError, IOError):
            sub_font = ImageFont.load_default()
    sub_text = "A Novel"
    sub_bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
    sw = sub_bbox[2] - sub_bbox[0]
    draw.text(
        ((width - sw) // 2, y + th + 30),
        sub_text,
        fill=(160, 140, 120),
        font=sub_font,
    )

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    img.save(dest_path, "JPEG", quality=85)


def seed_cover_image(root, io):
    """Create a placeholder cover at ``db/cover/front.jpg`` if missing."""
    cover_path = os.path.join(root, "db", "cover", "front.jpg")
    if not os.path.isfile(cover_path):
        try:
            title = os.path.basename(root).replace("-", " ").replace("_", " ").title()
            _generate_placeholder_cover(cover_path, title)
            io.tool_output("  db/cover/front.jpg — placeholder cover image")
        except Exception as e:
            io.tool_error(f"Could not generate cover image: {e}")


def _prompt_for_levels(io):
    """Ask the user for narrative levels.

    Returns a list of level names (e.g. ``["Act", "Chapter", "Scene"]``).
    """
    from .config import DEFAULT_LEVELS

    default_str = ", ".join(DEFAULT_LEVELS)

    raw = io.prompt_ask(
        f"Choose your narrative structure (broadest to narrowest) [{default_str}]:",
    )

    # Empty input (just hit Enter) → use defaults
    if not raw or not raw.strip():
        return list(DEFAULT_LEVELS)

    # Parse and validate
    parts = [p.strip().title() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        io.tool_output("Need at least 2 levels. Using defaults.")
        return list(DEFAULT_LEVELS)

    return parts


def _scaffold_first_node(root, levels):
    """Create the first narrative node with starter files.

    Builds the full path under ``novel/`` from the root level down to the
    leaf, e.g.::

        novel/
          Act 1 - Untitled/
            Chapter 1 - Untitled/
              Scene 1 - Untitled/
                SUMMARY.md
                PROSE.md

    Only leaf nodes get SUMMARY.md and PROSE.md.  Non-leaf directories are
    created empty (users can add their own notes later).
    """
    from .config import NOVEL_DIR
    from .narrative_map import make_titled_dir

    novel_root = os.path.join(root, NOVEL_DIR)
    os.makedirs(novel_root, exist_ok=True)

    # Build nested path: Level 1 - Untitled / Level 1 - Untitled / ...
    current = novel_root
    for i, level_name in enumerate(levels):
        dirname = make_titled_dir(level_name, 1)
        current = os.path.join(current, dirname)
        os.makedirs(current, exist_ok=True)

        is_leaf = i == len(levels) - 1

        if is_leaf:
            # Full location string for commands, e.g. "act 1 chapter 1 scene 1"
            loc_parts = " ".join(f"{lv.lower()} 1" for lv in levels[: i + 1])
            # Short form, e.g. "1 1 1"
            short_form = " ".join("1" for _ in levels[: i + 1])

            # Leaf-level SUMMARY.md
            summary_path = os.path.join(current, "SUMMARY.md")
            Path(summary_path).write_text(
                f"# {level_name} 1\n"
                "\n"
                f"This SUMMARY.md maintains an outline of your {level_name.lower()}.\n"
                "\n"
                f"- Create this summary from prose:"
                f" `/summarize {loc_parts}` or `/summarize {short_form}`\n"
                f"- Generate prose from this summary:"
                f" `/write {loc_parts}` or `/write {short_form}`\n"
                "\n"
                "Don't get too worried about the commands to start out though,"
                " you can also just tell the console what you want to do!\n",
                encoding="utf-8",
            )

            # Leaf-level PROSE.md: starter text
            prose_path = os.path.join(current, "PROSE.md")
            Path(prose_path).write_text(
                "It was a dark and stormy night.\n",
                encoding="utf-8",
            )

    return current


_DEFAULT_INSTRUCTIONS = {
    "elaborate.md": (
        "Elaborate on the text by adding richer descriptions, "
        "specific details, and further background."
    ),
    "condense.md": (
        "Condense and simplify the prose while still keeping the "
        "meaning and dialogue as close as possible to the original."
    ),
}


def _seed_default_instructions(instructions_dir):
    """Write default instruction files (elaborate, condense) if they don't exist."""
    for fname, content in _DEFAULT_INSTRUCTIONS.items():
        path = os.path.join(instructions_dir, fname)
        if not os.path.isfile(path):
            Path(path).write_text(content + "\n", encoding="utf-8")


def setup_novel_project(git_root, io):
    """Check for novel project structure and offer to create it.

    Called from ``main()`` after git setup.  Similar to the git-init
    prompt: if the ``db/`` and ``instructions/`` directories are missing,
    the user is asked whether to scaffold them.
    """
    try:
        root = git_root or str(Path.cwd())
    except OSError:
        return

    db_dir = os.path.join(root, "db")
    instructions_dir = os.path.join(root, "instructions")

    if os.path.isdir(db_dir) and os.path.isdir(instructions_dir):
        return  # Already set up

    # --- Step 1: resolve narrative levels FIRST ---
    from .config import CONFIG_FILE, save_config

    config_path = os.path.join(root, CONFIG_FILE)
    if not os.path.isfile(config_path):
        levels = _prompt_for_levels(io)
        save_config(root, {"levels": levels})
    else:
        from .config import load_config
        levels = load_config(root)["levels"]

    # --- Step 2: confirm full project scaffold ---
    if not io.confirm_ask(
        "No novel project structure found."
        " Set up novel/, db/, and instructions/ directories?"
    ):
        return

    os.makedirs(instructions_dir, exist_ok=True)
    _seed_default_instructions(instructions_dir)

    # Create cache directories for /save and /load
    cache_dir = os.path.join(root, "cache")
    os.makedirs(os.path.join(cache_dir, "chat"), exist_ok=True)
    os.makedirs(os.path.join(cache_dir, "context"), exist_ok=True)

    # Scaffold the first narrative node
    _scaffold_first_node(root, levels)

    try:
        from .db import Db

        db = Db(root, io=io)
        db.init_db()
    except Exception as e:
        io.tool_error(f"Error initializing db: {e}")
        return

    seed_cover_image(root, io)

    # Ensure .gitignore excludes cache/ and .aider*
    _ensure_gitignore(root)

    # Initialize Vale config if it doesn't exist
    from .vale_linter import init_vale_config, vale_available, vale_sync

    vale_config = init_vale_config(root)
    if vale_config:
        io.tool_output("  .vale.ini — prose linting configuration")
        if vale_available():
            io.tool_output("Running vale sync to download style packages...")
            vale_sync(root)
        else:
            io.tool_output(
                "Note: Install the vale package (pip install vale) "
                "to enable prose linting with /lint"
            )

    # Generate CLAUDE.md for Claude Code / SDK integration
    from .claude_md import init_claude_md

    claude_md_path = init_claude_md(root)
    if claude_md_path:
        io.tool_output("  CLAUDE.md — instructions for Claude Code")

    levels_str = " > ".join(levels)
    io.tool_output("Created novel project structure:")
    io.tool_output(f"  .composez       — project configuration (levels: {levels_str})")
    io.tool_output("  db/             — reference material (characters, locations, etc.)")
    io.tool_output("  instructions/   — reusable instructions for the chat (/instruct)")
    io.tool_output("  cache/          — saved chat history and context (/save, /load)")
    first_level = levels[0].lower()
    leaf_level = levels[-1].lower()
    io.tool_output(
        f"  novel/{levels[0]} 1/.../{levels[-1]} 1/ — your first {leaf_level}"
        " with PROSE.md and SUMMARY.md"
    )
    io.tool_output(f"Use /new to create narrative nodes (e.g. /new {first_level} Title)")

    # Auto-commit the scaffolded files
    try:
        import git as gitpython

        repo = gitpython.Repo(root)
        repo.git.add("db", "instructions", ".gitignore", ".composez")
        # Add the novel/ directory with the first narrative node
        from .config import NOVEL_DIR
        repo.git.add(NOVEL_DIR)
        if os.path.isfile(os.path.join(root, ".vale.ini")):
            repo.git.add(".vale.ini")
        if os.path.isfile(os.path.join(root, "CLAUDE.md")):
            repo.git.add("CLAUDE.md")
        # Only commit if there are staged changes (files may already be tracked)
        if repo.index.diff(repo.head.commit):
            repo.git.commit("-m", "Initial novel project structure", "--no-gpg-sign")
            io.tool_output("Committed initial project structure.")
    except (ValueError, TypeError):
        # New repo with no commits yet — commit unconditionally
        try:
            repo.git.commit("-m", "Initial novel project structure", "--no-gpg-sign")
            io.tool_output("Committed initial project structure.")
        except Exception as e:
            io.tool_error(f"Could not auto-commit: {e}")
    except Exception as e:
        io.tool_error(f"Could not auto-commit: {e}")


__all__ = [
    "Db",
    "NarrativeMap",
    "NovelCommands",
    "NovelcrafterImporter",
    "ValeLinter",
    "_seed_default_instructions",
    "activate_novel_mode",
    "activate_novel_query_mode",
    "init_claude_md",
    "load_core_context",
    "setup_novel_project",
]


_LAZY_IMPORTS = {
    "Db": ".db",
    "NarrativeMap": ".narrative_map",
    "NovelCommands": ".novel_commands",
    "NovelcrafterImporter": ".importer",
    "ValeLinter": ".vale_linter",
    "activate_novel_mode": ".novel_coder",
    "activate_novel_query_mode": ".novel_coder",
    "load_core_context": ".novel_coder",
    "init_claude_md": ".claude_md",
}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import importlib

        module = importlib.import_module(_LAZY_IMPORTS[name], __package__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
