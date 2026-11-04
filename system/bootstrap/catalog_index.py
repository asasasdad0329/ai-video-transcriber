"""Entry point for the local work index; runs in whichever project venv exists."""
from pathlib import Path
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catalog import main

if __name__ == "__main__":
    raise SystemExit(main())
