"""Reconcile the work folders into the index; nothing is ever deleted."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from workspace_paths import DATACARD_TASKS, HANDOUT_TASKS, OUTPUTS, ROOT, RUNS, resolve
from catalog.db import connect, log, mark_missing, now, one, rows, upsert

SOURCES = Path("工作資料/原檔")
MEDIA = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".flv",
         ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
OUTPUT_KINDS = {"逐字稿": "transcript", "資料卡": "datacard", "講義": "handout"}
TASK_KINDS = {"datacard": DATACARD_TASKS, "handout": HANDOUT_TASKS}
_SUFFIX = re.compile(r"_(?:\d{3,4}p|\d+k)$", re.I)


def display_name(name):
    """Video title without hashtags and resolution suffix, for matching and display."""
    stem = Path(name).stem
    stem = stem.split("#", 1)[0].strip()
    return _SUFFIX.sub("", stem).strip() or Path(name).stem


def rel(path, root=ROOT):
    path = Path(path)
    return path.relative_to(root).as_posix() if path.is_relative_to(Path(root)) else path.as_posix()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stamp(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _scan_sources(connection, root):
    seen = []
    folder = root / SOURCES
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA) if folder.is_dir() else []:
        key = rel(path, root)
        seen.append(key)
        upsert(connection, "sources", ["path"],
               {"path": key, "name": display_name(path.name), "bytes": path.stat().st_size,
                "mtime": _stamp(path), "status": "present"},
               "source", key, watch=("bytes", "mtime", "status"))
    mark_missing(connection, "sources", "source", "path", set(seen))
    return seen


def _scan_runs(connection, root):
    seen = []
    base = root / RUNS
    for folder in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        task = _json(folder / "task.json") or _json(folder / "state.json")
        if not task:
            continue
        run_id = task.get("task_id") or task.get("run_id") or folder.name
        primary = task.get("primary_transcript")
        primary_path = resolve(primary, root) if primary else None
        has_primary = bool(primary_path and primary_path.is_file())
        seen.append(run_id)
        upsert(connection, "runs", ["run_id"],
               {"run_id": run_id, "workflow": task.get("workflow", "video-transcribe"), "run_dir": rel(folder, root),
                "source_path": rel(resolve(task["source"], root), root) if task.get("source") else None,
                "status": task.get("status"), "primary_path": primary if has_primary else None,
                "primary_sha256": _sha(primary_path) if has_primary else None,
                "updated_at": task.get("updated_at"), "state": "active"},
               "run", run_id, watch=("status", "primary_sha256", "state"))
    mark_missing(connection, "runs", "run", "run_id", set(seen), state_field="state")
    return seen


def _scan_tasks(connection, root):
    seen = []
    for kind, folder in TASK_KINDS.items():
        base = root / folder
        for path in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            task = _json(path / "task.json")
            if not task:
                continue
            key = f"{kind}:{path.name}"
            seen.append(key)
            upsert(connection, "tasks", ["kind", "run_id"],
                   {"kind": kind, "run_id": task.get("task_id", path.name), "folder": rel(path, root),
                    "status": task.get("status"), "output": task.get("output"),
                    "primary_sha256": task.get("primary_sha256"), "updated_at": task.get("updated_at"),
                    "parts": json.dumps({"title": task.get("title"), "parts": task["parts"]}, ensure_ascii=False)
                             if task.get("parts") else None,
                    "state": "active"},
                   "task", key, watch=("status", "output", "state"))
    present = {k.split(":", 1)[1] for k in seen}
    for row in rows(connection, "select * from tasks where state != 'missing'"):
        if f"{row['kind']}:{row['run_id']}" in seen or row["run_id"] in present:
            continue
        connection.execute("update tasks set state='missing' where kind=? and run_id=?", (row["kind"], row["run_id"]))
        log(connection, "task", f"{row['kind']}:{row['run_id']}", "missing", previous=row["state"])
    return seen


def _owner(connection, kind, path):
    if kind == "transcript":
        row = one(connection, "select run_id from runs where primary_path=?", path)
    else:
        row = one(connection, "select run_id from tasks where kind=? and output=?", kind, path)
    return row["run_id"] if row else None


def _scan_artifacts(connection, root):
    seen_paths = []
    for folder, kind in OUTPUT_KINDS.items():
        base = root / OUTPUTS / folder
        for path in sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else []:
            key, digest = rel(path, root), _sha(path)
            seen_paths.append(key)
            current = one(connection, "select * from artifacts where path=? and status='current'", key)
            if current and current["sha256"] != digest:
                connection.execute("update artifacts set status='superseded' where id=?", (current["id"],))
                log(connection, "artifact", key, "superseded", sha256=current["sha256"])
            upsert(connection, "artifacts", ["path", "sha256"],
                   {"kind": kind, "path": key, "run_id": _owner(connection, kind, key), "sha256": digest,
                    "bytes": path.stat().st_size, "status": "current"},
                   "artifact", key, watch=("status",))
    for row in rows(connection, "select * from artifacts where status='current'"):
        if row["path"] not in seen_paths:
            connection.execute("update artifacts set status='missing' where id=?", (row["id"],))
            log(connection, "artifact", row["path"], "missing", sha256=row["sha256"])
    return seen_paths


def _documents(connection, root):
    """Text for search: transcripts, data cards and the handout source (DOCX text is not read here)."""
    wanted = {}
    for row in rows(connection, "select * from artifacts where status='current' and kind in ('transcript','datacard')"):
        wanted[row["path"]] = (row["kind"], row["run_id"], row["sha256"])
    for row in rows(connection, "select * from tasks where kind='handout' and state='active'"):
        source = root / Path(row["folder"]) / "handout.md"
        if source.is_file():
            wanted[rel(source, root)] = ("handout", row["run_id"], _sha(source))
    for path, (kind, run_id, digest) in wanted.items():
        known = one(connection, "select * from documents where path=?", path)
        if known and known["sha256"] == digest:
            continue
        try:
            body = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        connection.execute("delete from search where path=?", (path,))
        connection.execute("insert into search(path, kind, run_id, body) values(?,?,?,?)", (path, kind, run_id, body))
        connection.execute("insert into documents(path, kind, run_id, sha256) values(?,?,?,?) "
                           "on conflict(path) do update set kind=excluded.kind, run_id=excluded.run_id, "
                           "sha256=excluded.sha256", (path, kind, run_id, digest))
    for row in rows(connection, "select path from documents"):
        if row["path"] not in wanted:
            connection.execute("delete from search where path=?", (row["path"],))
            connection.execute("delete from documents where path=?", (row["path"],))
    return len(wanted)


def scan(root=ROOT):
    root = Path(root)
    connection = connect(root)
    with connection:
        sources = _scan_sources(connection, root)
        runs = _scan_runs(connection, root)
        tasks = _scan_tasks(connection, root)
        artifacts = _scan_artifacts(connection, root)
        documents = _documents(connection, root)
        connection.execute("insert into meta(key, value) values('last_scan', ?) "
                           "on conflict(key) do update set value=excluded.value", (now(),))
    return {"sources": len(sources), "runs": len(runs), "tasks": len(tasks),
            "artifacts": len(artifacts), "documents": documents}
