"""CLI for the data-card and handout workflows (run through .\\run.ps1)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from workspace_paths import ROOT


def _doctor():
    from derived_common import mermaid
    result = {"python": sys.version.split()[0], "venv": sys.prefix != sys.base_prefix}
    try:
        result["mermaid"] = mermaid.asset_path().relative_to(ROOT).as_posix()
    except RuntimeError as exc:
        result["mermaid"] = str(exc)
    try:
        with mermaid.Mermaid() as renderer:
            result["browser"] = renderer._browser.version
            result["mermaid_render"] = renderer.parse("flowchart TD\n  A-->B") is None
    except Exception as exc:  # report, do not install anything
        result["browser"] = f"unavailable: {str(exc).splitlines()[0]}"
    import docx  # noqa: F401  (import check)
    result["python_docx"] = True
    result["ready"] = result.get("mermaid_render") is True
    return result


def _status(kind, run):
    from derived_common import source, tasks
    if kind == "datacard":
        from datacard import build as card
        doc = card.target(run)
    else:
        doc = source.load_run(run)
    folder = tasks.task_dir(kind, doc["run_id"], create=False)
    task = tasks.read_task(folder)
    check = folder / "check.json"
    report = json.loads(check.read_text(encoding="utf-8")) if check.is_file() else {}
    stale = bool(task.get("primary_sha256")) and task["primary_sha256"] != doc["primary_sha256"]
    return {"task": source.rel(folder), "run": doc["run_dir"], "title": doc["title"],
            **({"series": doc["run_id"], "parts": [{"part": p["part"], "title": p["title"]} for p in doc["parts"]]}
               if doc.get("parts") else {}),
            "status": "stale" if stale else (task.get("status") or "not_started"),
            "output": task.get("output"), "errors": report.get("errors", []), "warnings": report.get("warnings", []),
            "message": "逐字稿已更新，需重新 prepare／build" if stale else task.get("message")}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.ps1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("publish-doctor", help="檢查資料卡／講義環境")
    commands.add_parser("derived-list", help="列出資料卡與講義任務")
    for kind in ("datacard", "handout"):
        route = commands.add_parser(kind).add_subparsers(dest="action", required=True)
        prepare = route.add_parser("prepare")
        route.add_parser("status").add_argument("run", nargs="+" if kind == "datacard" else None)
        build = route.add_parser("build")
        if kind == "datacard":
            # Several videos make one series card; afterwards the series ID addresses it.
            prepare.add_argument("run", nargs="+")
            prepare.add_argument("--name", help="系列名稱（預設取各部標題的共同開頭）")
            prepare.add_argument("--allow-edited-primary", action="store_true", help="保留段落順序的少量手動校字，沿用原段落時間")
            build.add_argument("run", nargs="+")
            build.add_argument("--name")
            build.add_argument("--plan")
        else:
            prepare.add_argument("run")
            build.add_argument("run")
            build.add_argument("--source")
            build.add_argument("--preview", action="store_true", help="用 Word 產生低解析度頁面預覽")
    args = parser.parse_args(argv)
    try:
        if args.command == "publish-doctor":
            result = _doctor()
        elif args.command == "derived-list":
            from derived_common import tasks
            result = tasks.list_tasks()
        elif args.action == "status":
            result = _status(args.command, args.run)
        elif args.command == "datacard":
            from datacard import build
            result = (build.prepare(args.run, args.name, allow_edited_primary=args.allow_edited_primary) if args.action == "prepare"
                      else build.build(args.run, args.plan, args.name))
        else:
            from handout import build
            result = build.prepare(args.run) if args.action == "prepare" else build.build(args.run, args.source, args.preview)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("status") == "needs_fix":
        return 1
    return 0 if not isinstance(result, dict) or result.get("ready", True) else 1
