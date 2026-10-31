"""Entry point for the publish venv (data cards and handouts); isolated like video_workflow.py."""
from pathlib import Path
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from publish_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
