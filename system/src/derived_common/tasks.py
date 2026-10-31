"""Task folders and summaries for workflows derived from a transcription run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from workspace_paths import DATACARD_TASKS, HANDOUT_TASKS, OUTPUTS, ROOT
from derived_common.source import rel

KINDS = {
    "datacard": {"tasks": DATACARD_TASKS, "output": OUTPUTS / "資料卡", "suffix": ".md", "workflow": "datacard"},
    "handout": {"tasks": HANDOUT_TASKS, "output": OUTPUTS / "講義", "suffix": ".docx", "workflow": "handout"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def task_dir(kind, run_id, create=True):
    path = ROOT / KINDS[kind]["tasks"] / run_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def output_path(kind, doc):
    """Same stem as the primary transcript, so the three deliverables line up.

    A series card has no single transcript, so it is named after the series."""
    stem = doc.get("stem") or Path(doc["primary"]).stem
    return ROOT / KINDS[kind]["output"] / (stem + KINDS[kind]["suffix"])


def read_task(folder):
    path = Path(folder) / "task.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_task(kind, folder, doc, **fields):
    data = read_task(folder)
    data.update({"task_id": doc["run_id"], "workflow": KINDS[kind]["workflow"], "source_run": doc["run_dir"],
                 "primary_transcript": doc["primary"], "primary_sha256": doc["primary_sha256"],
                 "updated_at": now(), **fields})
    if kind == "datacard":
        data.update(allow_edited_primary=doc.get("allow_edited_primary", False),
                    primary_edits=doc.get("primary_edits", []),
                    reading_map_sha256=doc.get("reading_map_sha256"))
    if doc.get("parts"):
        # A series task is addressed by its own ID; the parts record which runs it was built from.
        data["title"] = doc["title"]
        data["parts"] = [{"part": p["part"], "run_id": p["run_id"], "title": p["title"],
                          "primary_transcript": p["primary"], "primary_sha256": p["primary_sha256"],
                          "primary_edits": p.get("primary_edits", []), "reading_map_sha256": p.get("reading_map_sha256")}
                         for p in doc["parts"]]
    (Path(folder) / "task.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_tasks():
    rows = []
    for kind, spec in KINDS.items():
        base = ROOT / spec["tasks"]
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            task = read_task(folder)
            rows.append({"workflow": kind, "task_id": folder.name, "status": task.get("status"),
                         "output": task.get("output"), "folder": rel(folder)})
    return rows
