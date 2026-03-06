"""
Vale-based prose linter for novel manuscripts.

Wraps the Vale CLI tool to lint .txt and .md files against
configurable prose style rules (write-good, proselint, ai-tells, etc.).
"""

import json
import os
import subprocess
from dataclasses import dataclass, field

from .config import NOVEL_DIR


@dataclass
class ValeLintResult:
    """Result of linting a prose file with Vale."""

    text: str = ""
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# Default .vale.ini content for novel projects
DEFAULT_VALE_INI = """\
StylesPath = .vale-styles
MinAlertLevel = suggestion

Packages = write-good, proselint, \
  https://github.com/tbhb/vale-ai-tells/releases/download/v1.4.0/ai-tells.zip

[*.{md,txt}]
BasedOnStyles = Vale, write-good, proselint, ai-tells

# -- Fiction overrides ------------------------------------------------
# Keep useful checks, silence rules designed for tech writing.

# Built-in spell checker is too noisy for fiction (invented names, places)
Vale.Spelling = NO
# E-Prime flags every use of "is/was/are/been" — far too noisy for prose
write-good.E-Prime = NO
# Passive voice is a valid stylistic choice; keep as a gentle nudge
write-good.Passive = suggestion

# Em-dashes and formal transitions are standard literary punctuation
ai-tells.EmDashUsage = NO
ai-tells.FormalTransitions = NO

# Ellipsis style (… vs ...) is a house-style decision, not an error
proselint.Typography = suggestion
proselint.Clichés = warning
# "Don't start with So/But" — keep as suggestion, not error
proselint.But = suggestion
write-good.So = suggestion
"""


def _vale_bin():
    """Return the path to the Vale binary, downloading it if necessary.

    Uses the ``vale`` Python package which bundles/downloads the Go binary.
    Returns None if the package is not installed.
    """
    try:
        from vale.main import download_vale_if_missing

        return download_vale_if_missing()
    except (ImportError, Exception):
        return None


def vale_available():
    """Return True if the Vale binary can be located."""
    return _vale_bin() is not None


def init_vale_config(root):
    """Create a .vale.ini in *root* if one doesn't already exist.

    Returns the path to the config file, or None if it already existed.
    """
    config_path = os.path.join(root, ".vale.ini")
    if os.path.exists(config_path):
        return None

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_VALE_INI)

    return config_path


def _styles_dir(root):
    """Return the path to the styles directory for *root*.

    Reads ``StylesPath`` from ``.vale.ini`` if present, otherwise
    falls back to ``.vale-styles``.
    """
    config_path = os.path.join(root, ".vale.ini")
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith("stylespath"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return os.path.join(root, parts[1].strip())
    return os.path.join(root, ".vale-styles")


def _sync_timestamp(root):
    """Return the path to the sync-timestamp sentinel file."""
    return os.path.join(_styles_dir(root), "sync-timestamp")


def vale_sync(root):
    """Run ``vale sync`` to download configured packages.

    This is needed after creating or modifying .vale.ini so that
    the style packages referenced by ``Packages`` are fetched.

    On success, writes a ``sync-timestamp`` sentinel inside the
    styles directory so that future lints can detect stale configs.
    """
    bin_path = _vale_bin()
    if not bin_path:
        return False

    try:
        subprocess.run(
            [bin_path, "sync"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Write a timestamp sentinel so we can detect config changes later
        ts_path = _sync_timestamp(root)
        os.makedirs(os.path.dirname(ts_path), exist_ok=True)
        with open(ts_path, "w") as f:
            pass  # empty file — only its mtime matters
        return True
    except (subprocess.TimeoutExpired, OSError):
        return False


class ValeLinter:
    """Runs Vale on prose files and parses the JSON output."""

    def __init__(self, root=None, encoding="utf-8"):
        self.root = root
        self.encoding = encoding
        self._synced = False

    def _ensure_synced(self):
        """Run ``vale sync`` if styles are missing or config has changed."""
        if self._synced or not self.root:
            return

        needs_sync = False
        ts_path = _sync_timestamp(self.root)

        if not os.path.isfile(ts_path):
            # Never synced (or styles dir missing)
            needs_sync = True
        else:
            # Re-sync if .vale.ini is newer than the last sync
            config_path = os.path.join(self.root, ".vale.ini")
            if os.path.isfile(config_path):
                config_mtime = os.path.getmtime(config_path)
                sync_mtime = os.path.getmtime(ts_path)
                if config_mtime > sync_mtime:
                    needs_sync = True

        if needs_sync:
            vale_sync(self.root)

        self._synced = True

    def lint(self, fname):
        """Lint a single file with Vale.

        Returns a ``ValeLintResult`` if there are issues, or None if clean.
        Returns a result with an error message if Vale is not installed.
        """
        if not vale_available():
            return ValeLintResult(
                text="Vale is not installed. Install it from https://vale.sh/docs/install/",
            )

        self._ensure_synced()

        rel_fname = self._rel(fname)
        output = self._run_vale(fname)

        if output is None:
            return None

        warnings = self._parse_json(output)
        if not warnings:
            return None

        lines = sorted({w["line"] for w in warnings})
        text = self._format_warnings(rel_fname, warnings)

        return ValeLintResult(text=text, lines=lines, warnings=warnings)

    def lint_files(self, fnames):
        """Lint multiple files in a single Vale invocation.

        Returns a dict mapping each filename to its ``ValeLintResult``
        (or ``None`` when clean).  Files that produce no warnings are
        omitted from the result.
        """
        if not fnames:
            return {}
        if not vale_available():
            return {}

        self._ensure_synced()

        output = self._run_vale(*fnames)
        if output is None:
            return {}

        raw = output.strip()
        if not raw:
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        results = {}
        for file_path, issues in data.items():
            warnings = []
            for issue in issues:
                warnings.append({
                    "line": issue.get("Line", 0),
                    "check": issue.get("Check", ""),
                    "message": issue.get("Message", ""),
                    "severity": issue.get("Severity", "suggestion"),
                    "match": issue.get("Match", ""),
                })
            if not warnings:
                continue
            # Normalise the key to an absolute path so callers can
            # look results up by the same paths they passed in.
            abs_path = (
                os.path.join(self.root, file_path)
                if self.root and not os.path.isabs(file_path)
                else file_path
            )
            abs_path = os.path.normpath(abs_path)
            rel = self._rel(abs_path)
            lines_list = sorted({w["line"] for w in warnings})
            text = self._format_warnings(rel, warnings)
            results[abs_path] = ValeLintResult(
                text=text, lines=lines_list, warnings=warnings
            )

        return results

    def _run_vale(self, *fnames):
        """Execute vale --output=JSON on one or more files. Returns raw stdout or None."""
        bin_path = _vale_bin()
        if not bin_path:
            return None

        cmd = [bin_path, "--output=JSON"]

        config_path = os.path.join(self.root, ".vale.ini") if self.root else ".vale.ini"
        if os.path.exists(config_path):
            cmd.append(f"--config={config_path}")

        cmd.extend(fnames)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.root,
                encoding=self.encoding,
                errors="replace",
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        # Vale returns exit code 0 for clean, 1 for warnings/errors, 2 for fatal
        if result.returncode == 2:
            return None

        return result.stdout

    def _parse_json(self, raw):
        """Parse Vale's JSON output into a list of warning dicts.

        Vale JSON format::

            {
              "path/to/file.md": [
                {
                  "Action": {...},
                  "Check": "write-good.Passive",
                  "Description": "",
                  "Line": 5,
                  "Link": "...",
                  "Message": "\"was killed\" may be passive voice...",
                  "Severity": "suggestion",
                  "Span": [1, 10],
                  "Match": "was killed"
                },
                ...
              ]
            }
        """
        if not raw or not raw.strip():
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        warnings = []
        for _fname, issues in data.items():
            for issue in issues:
                warnings.append({
                    "line": issue.get("Line", 0),
                    "check": issue.get("Check", ""),
                    "message": issue.get("Message", ""),
                    "severity": issue.get("Severity", "suggestion"),
                    "match": issue.get("Match", ""),
                })

        return warnings

    def _format_warnings(self, fname, warnings):
        """Format warnings into human-readable text."""
        lines = [f"## Vale lint: {fname}\n"]

        # Group by severity
        by_sev = {}
        for w in warnings:
            by_sev.setdefault(w["severity"], []).append(w)

        sev_order = ["error", "warning", "suggestion"]
        for sev in sev_order:
            items = by_sev.get(sev, [])
            if not items:
                continue
            lines.append(f"### {sev} ({len(items)})")
            for item in items:
                check = item["check"]
                msg = item["message"]
                line_num = item["line"]
                lines.append(f"  Line {line_num}: [{check}] {msg}")
            lines.append("")

        return "\n".join(lines)

    def _rel(self, fname):
        """Return a path relative to root."""
        if self.root:
            try:
                return os.path.relpath(fname, self.root)
            except ValueError:
                return fname
        return fname


def make_markdown_linter(root, coder=None):
    """Return a callable for aider's ``Linter.set_linter('markdown', ...)``.

    The returned function has the signature
    ``(fname, rel_fname, code) -> LintResult | None`` expected by
    :class:`aider.linter.Linter`.  It runs Vale on the file, filters by the
    current lint level (read from *coder._novel_lint_level*), and converts
    the result into a :class:`~aider.linter.LintResult` with 0-indexed lines.
    """
    from aider.linter import LintResult

    vale = ValeLinter(root=root)

    _LINT_LEVELS = {
        "error": {"error"},
        "warning": {"error", "warning"},
        "suggestion": {"error", "warning", "suggestion"},
    }

    def markdown_lint(fname, rel_fname, code):
        # Only lint files under novel/ — db/ files (style guides, etc.)
        # contain quoted prose that triggers false positives and causes
        # reflection loops when the LLM tries to "fix" them.
        if root:
            try:
                rel = os.path.relpath(fname, root)
            except ValueError:
                rel = rel_fname or ""
        else:
            rel = rel_fname or ""
        parts = rel.replace(os.sep, "/").split("/")
        if not parts or parts[0] != NOVEL_DIR:
            return None

        result = vale.lint(fname)
        if not result or not result.warnings:
            return None

        level = (
            getattr(coder, "_novel_lint_level", "warning") if coder else "warning"
        )
        severities = _LINT_LEVELS.get(level, {"error", "warning"})

        filtered = [w for w in result.warnings if w["severity"] in severities]
        if not filtered:
            return None

        text = vale._format_warnings(rel_fname, filtered)
        lines = [w["line"] - 1 for w in filtered]  # 0-indexed for tree_context

        return LintResult(text=text, lines=lines)

    return markdown_lint
