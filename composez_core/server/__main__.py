"""
Launch the Novel UI server.

    python -m composez_core.server [--port 8188] [-- <aider args>]

Starts the FastAPI server and opens the browser.

Normal mode:  builds the Vue frontend (npm run build) then serves
              everything from a single uvicorn process.
Dev mode:     skips the build; you run ``cd novel_ui && npm run dev``
              separately and the Vite dev server proxies /api and /ws
              to uvicorn.
"""

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

import uvicorn

_NOVEL_UI_DIR = Path(__file__).resolve().parent.parent.parent / "novel_ui"


def _ensure_frontend_built():
    """Build the Vue frontend if dist/ is missing or stale."""
    dist = _NOVEL_UI_DIR / "dist"
    pkg_json = _NOVEL_UI_DIR / "package.json"

    if not pkg_json.is_file():
        if dist.is_dir():
            return True  # pre-built dist exists, no source needed
        print(f"Warning: {_NOVEL_UI_DIR} not found — serving API only")
        return False

    # Check if dist/ already exists and is up-to-date
    needs_build = not dist.is_dir()
    if not needs_build:
        src_dir = _NOVEL_UI_DIR / "src"
        if src_dir.is_dir():
            dist_mtime = max(
                (f.stat().st_mtime for f in dist.rglob("*") if f.is_file()),
                default=0,
            )
            src_mtime = max(
                (f.stat().st_mtime for f in src_dir.rglob("*") if f.is_file()),
                default=0,
            )
            if src_mtime > dist_mtime:
                needs_build = True

    if not needs_build:
        return True  # dist/ is up-to-date, no build needed

    # Need to build — check for npm and node
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        if dist.is_dir():
            print("Warning: npm/node not found — using existing frontend build")
            return True
        missing = "npm" if not npm else "node"
        print(f"Warning: {missing} not found on PATH — cannot build frontend")
        print("Install Node.js (https://nodejs.org/) or run with --dev")
        return False

    node_modules = _NOVEL_UI_DIR / "node_modules"
    if not node_modules.is_dir():
        print("Installing frontend dependencies (npm install)...")
        subprocess.run(
            [npm, "install"],
            cwd=str(_NOVEL_UI_DIR),
            check=True,
        )

    print("Building frontend (npm run build)...")
    subprocess.run(
        [npm, "run", "build"],
        cwd=str(_NOVEL_UI_DIR),
        check=True,
    )

    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Novel UI server")
    parser.add_argument("--port", type=int, default=8188, help="Port (default: 8188)")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument(
        "--dev", action="store_true",
        help="Dev mode: skip frontend build, enable hot reload. "
             "Run 'cd novel_ui && npm run dev' separately.",
    )

    args, remaining = parser.parse_known_args(argv)

    # Pass remaining args to aider's main via sys.argv
    # Strip the leading "--" separator if present
    if remaining and remaining[0] == "--":
        remaining = remaining[1:]
    if remaining:
        sys.argv = ["aider"] + remaining

    if args.dev:
        print("Dev mode: skipping frontend build.")
        print(f"  Run the Vite dev server separately:  cd novel_ui && npm run dev")
        print(f"  Then open http://localhost:5173")
        print()
    else:
        _ensure_frontend_built()

    url = f"http://{args.host}:{args.port}"

    if not args.no_browser and not args.dev:
        # Open browser once the server is actually accepting connections
        import socket
        import threading

        def _open():
            import time

            # Poll until the server is accepting TCP connections
            for _ in range(50):  # up to ~10 seconds
                try:
                    with socket.create_connection((args.host, args.port), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.2)

            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    print(f"Novel UI starting on {url}")
    print("Press Ctrl+C to stop.")

    uvicorn.run(
        "composez_core.server.app:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
        log_level="info",
    )


if __name__ == "__main__":
    main()
