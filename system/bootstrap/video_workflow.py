"""Entry point for .venv/python -I -B; no global packages or PATH changes."""
from pathlib import Path
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
