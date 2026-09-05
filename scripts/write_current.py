"""Portable entrypoint for the project-continuity v2 CLI."""
from cli_v2 import main

if __name__ == "__main__":
    import sys
    # JSON CLI streams have a stable encoding even on legacy Windows consoles.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
