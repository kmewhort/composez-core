# Composez

A terminal-based, git-integrated AI writing tool for long-form fiction. Composez adds structured narrative editing so you can write, revise, and manage novels from the command line as you write with your favourite editor.

If you're looking for a more ready-to-use, one-click solution, check out the web editing tool at https://composez.ai.

## Features

- **Structured Manuscripts** -- Organize your work into Acts, Chapters, and Scenes (with configurable level names). Summaries flow between scenes so the AI always has the right context.
- **Reference Database** -- Character sheets, locations, world-building notes, and a style guide live in `db/` and are loaded as read-only context automatically.
- **Write / Summarize / Feedback Loop** -- Generate prose from scene summaries with `/write`, get critique with `/feedback`, revise interactively, then `/summarize` to keep context current.
- **Three Autonomy Levels** -- Direct (single-turn), Compose (plan then edit), and Agent (multi-step YAML plans) work with any edit mode.
- **Prose Linting** -- Integrated Vale-based checks for passive voice, cliches, and AI-tells via `/lint`.
- **Import & Export** -- Import from Novelcrafter or Markdown; export to Markdown, DOCX, or EPUB.
- **Full Git Integration** -- Every edit is version-controlled with auto-commits and diff review.
- **Multi-Model Support** -- Works with Claude, GPT-4, Gemini, local models, or any provider Aider supports.

Full documentation is available at [docs.composez.ai](https://docs.composez.ai).

## Getting Started

### Prerequisites

- Python 3.9+
- Git
- An API key for your preferred LLM provider (Claude, OpenAI, etc.)

### Install

```bash
git clone https://github.com/kmewhort/aider.git composez
cd composez
python -m venv venv
source venv/bin/activate    # On Windows: venv\Scripts\activate
pip install -e .
```

### Configure Your API Key

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # For Claude (recommended)
# or
export OPENAI_API_KEY=sk-...          # For OpenAI
```

You can also place these in a `.env` file in your project directory.

### Create a Project and Start Writing

```bash
mkdir my-novel && cd my-novel
git init
composez
```

### Import

To import an existing manuscript from `markdown` or `novelcrafter`, first create your project, put your export in your project root, and then:

- Markdown: `/import markdown my_markdown.md`
- Novelcrafter: `/import novelcrafter novelcrafter_export.zip`


### Writing

Inside the Composez shell, build your structure and write:

```
> /new act The Awakening
> /new 1 chapter Morning Light
> /new 1 1 scene The Alarm

> /add 1 1 1
> Elena wakes to the lighthouse alarm at 3 AM. Something large on the radar.

> /write 1 1 1
> /feedback 1 1 1
> /summarize 1 1 1
```

### Optional Dependencies

```bash
pip install vale          # Prose linting via /lint
pip install python-docx   # DOCX export
pip install ebooklib      # EPUB export
```

## Documentation

See [docs.composez.ai](https://docs.composez.ai) for the full guide, including:

- [Installation](https://docs.composez.ai/guide/installation.html)
- [Quickstart](https://docs.composez.ai/guide/quickstart.html)
- [Narrative Structure](https://docs.composez.ai/guide/narrative-structure.html)
- [Writing Workflow](https://docs.composez.ai/guide/writing-workflow.html)
- [Command Reference](https://docs.composez.ai/reference/commands.html)
- [Configuration](https://docs.composez.ai/reference/composez-config.html)

## Architecture

Composez is built as a plugin on top of [Aider](https://aider.chat). This repo contains novel-specific coders, prompts, etc. More generic changes to the the origin Aider tool can be found in the [composez-aider-fork](https://github.com/kmewhort/composez-aider-fork).


## License

AGPL-3.0 license.  See `LICENSE` for furthe details.
