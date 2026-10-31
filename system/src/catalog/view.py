"""Queries over the index: overview, search, history, and the Markdown view."""
from __future__ import annotations

import json
from pathlib import Path

from workspace_paths import ROOT
from catalog.db import one, rows
from catalog.scan import display_name

FINISHED = ("completed", "completed_with_unresolved")
TRANSCRIPT_LABELS = {"completed": "完成", "completed_with_unresolved": "完成（有〔？〕）", "missing": "任務資料夾不見了",
                     "primary_structure_changed": "主稿段落結構被改動"}
DERIVED_LABELS = {"completed": "完成", "needs_fix": "需修正", "awaiting_plan": "待撰寫", "awaiting_source": "待撰寫",
                  "stale": "過期（逐字稿已更新）", "missing_output": "成品遺失", "missing": "任務資料夾不見了"}


def variants(text):
    forms = {text}
    try:  # the transcribe venv has OpenCC; titles are usually Simplified
        from opencc import OpenCC
        forms |= {OpenCC("t2s").convert(text), OpenCC("s2t").convert(text)}
    except Exception:
        pass
    return {f.lower() for f in forms if f}


def _derived(connection, kind, run_id, primary_sha):
    task = one(connection, "select * from tasks where kind=? and run_id=?", kind, run_id)
    if not task:
        return None
    status = task["status"]
    if task["state"] == "missing":
        status = "missing"
    elif status == "completed" and task["primary_sha256"] and primary_sha and task["primary_sha256"] != primary_sha:
        status = "stale"
    elif status == "completed" and task["output"]:
        current = one(connection, "select * from artifacts where path=? and status='current'", task["output"])
        if not current:
            status = "missing_output"
    versions = rows(connection, "select sha256, status from artifacts where path=? order by id",
                    task["output"]) if task["output"] else []
    return {"status": status, "label": DERIVED_LABELS.get(status, status), "output": task["output"],
            "updated_at": task["updated_at"], "versions": len(versions), "folder": task["folder"]}


def _transcript(connection, run):
    status = "missing" if run["state"] == "missing" else run["status"]
    versions = rows(connection, "select sha256 from artifacts where path=?", run["primary_path"]) if run["primary_path"] else []
    return {"status": status, "label": TRANSCRIPT_LABELS.get(status, f"進行中（{status}）"),
            "output": run["primary_path"], "versions": len(versions)}


def _best_runs(connection):
    """Latest finished run per source; unfinished ones only when nothing better exists."""
    by_source, by_name = {}, {}
    ranked = sorted(rows(connection, "select * from runs"),
                    key=lambda r: (r["state"] == "active", r["status"] in FINISHED, r["updated_at"] or ""))
    for run in ranked:
        if run["source_path"]:
            by_source[run["source_path"]] = run
            by_name.setdefault(display_name(Path(run["source_path"]).name), run)
            by_name[display_name(Path(run["source_path"]).name)] = run
    return by_source, by_name


def series(connection):
    """Data cards built from several videos at once; each part keeps its own run."""
    listed = []
    for task in rows(connection, "select * from tasks where kind='datacard' and parts is not null order by updated_at"):
        record = json.loads(task["parts"])
        parts, stale = [], False
        for part in record["parts"]:
            run = one(connection, "select * from runs where run_id=?", part["run_id"])
            current = run["primary_sha256"] if run else None
            if current and part.get("primary_sha256") and current != part["primary_sha256"]:
                stale = True
            parts.append({"part": part["part"], "title": part["title"], "run_id": part["run_id"],
                          "transcript": part.get("primary_transcript")})
        status = task["status"]
        if task["state"] == "missing":
            status = "missing"
        elif status == "completed" and stale:
            status = "stale"
        elif status == "completed" and task["output"] and not one(
                connection, "select * from artifacts where path=? and status='current'", task["output"]):
            status = "missing_output"
        versions = rows(connection, "select sha256 from artifacts where path=? order by id",
                        task["output"]) if task["output"] else []
        listed.append({"name": record.get("title") or task["run_id"], "series_id": task["run_id"],
                       "status": status, "label": DERIVED_LABELS.get(status, status), "output": task["output"],
                       "updated_at": task["updated_at"], "versions": len(versions), "folder": task["folder"],
                       "parts": parts})
    return listed


def overview(connection):
    by_source, by_name = _best_runs(connection)
    videos, used = [], set()
    for source in rows(connection, "select * from sources order by name"):
        run = by_source.get(source["path"]) or by_name.get(source["name"])
        entry = {"name": source["name"], "source": source["path"], "source_status": source["status"]}
        if run:
            used.add(run["run_id"])
            entry.update(run_id=run["run_id"], run_dir=run["run_dir"], transcript=_transcript(connection, run),
                         datacard=_derived(connection, "datacard", run["run_id"], run["primary_sha256"]),
                         handout=_derived(connection, "handout", run["run_id"], run["primary_sha256"]))
            if run["source_path"] != source["path"]:
                entry["note"] = f"同名影片已轉錄（{Path(run['source_path']).name}），可直接沿用"
        else:
            entry["transcript"] = {"status": None, "label": "未轉錄"}
        videos.append(entry)
    orphans = []
    for run in by_source.values():
        if run["run_id"] in used:
            continue
        name = display_name(Path(run["source_path"]).name) if run["source_path"] else run["run_id"]
        orphans.append({"name": name, "source": run["source_path"], "run_id": run["run_id"], "run_dir": run["run_dir"],
                        "transcript": _transcript(connection, run),
                        "datacard": _derived(connection, "datacard", run["run_id"], run["primary_sha256"]),
                        "handout": _derived(connection, "handout", run["run_id"], run["primary_sha256"]),
                        "note": "原檔已不在原檔資料夾"})
    listed = series(connection)
    for entry in videos + orphans:
        joined = [{"name": s["name"], "series_id": s["series_id"], "status": s["status"], "label": s["label"],
                   "output": s["output"]} for s in listed
                  if any(p["run_id"] == entry.get("run_id") for p in s["parts"])]
        if joined:
            entry["series"] = joined
    scanned = one(connection, "select value from meta where key='last_scan'")
    return {"scanned_at": scanned["value"] if scanned else None, "videos": videos, "orphans": orphans,
            "series": listed, "removed": removed(connection)}


def removed(connection):
    return {
        "sources": rows(connection, "select path, name, last_seen from sources where status='missing' order by name"),
        "outputs": rows(connection, "select path, kind, sha256, last_seen from artifacts where status='missing' "
                                    "order by last_seen desc"),
        "superseded": rows(connection, "select path, kind, count(*) as versions from artifacts "
                                       "where status='superseded' group by path, kind"),
        "runs": rows(connection, "select run_id, run_dir, source_path from runs where state='missing'"),
    }


def search(connection, query, limit=20):
    forms = variants(query)
    names, seen = [], set()
    whole = overview(connection)
    # Videos whose source file is gone stay searchable; that history is the point of the index.
    index = {v["name"] + " " + (v.get("source") or ""): v for v in whole["videos"] + whole["orphans"]}
    for text, video in index.items():
        if any(form in text.lower() for form in forms) and video["name"] not in seen:
            seen.add(video["name"])
            names.append(video)
    hits = []
    for form in sorted(forms):
        pattern = '"' + form.replace('"', '""') + '"'
        for row in rows(connection, "select path, kind, run_id, snippet(search, 3, '《', '》', '…', 14) as snippet "
                                    "from search where body match ? limit ?", pattern, limit):
            if not any(h["path"] == row["path"] and h["snippet"] == row["snippet"] for h in hits):
                hits.append(row)
    return {"query": query, "videos": names, "content_hits": hits[:limit]}


def show(connection, value):
    matches = search(connection, value)["videos"]
    if not matches:
        raise ValueError(f"索引中找不到：{value}")
    video = matches[0]
    run_id = video.get("run_id")
    detail = {"video": video, "runs": rows(connection, "select * from runs where source_path=? or run_id=?",
                                           video.get("source"), run_id or "")}
    detail["tasks"] = rows(connection, "select * from tasks where run_id=?", run_id or "")
    detail["series"] = [s for s in series(connection) if any(p["run_id"] == run_id for p in s["parts"])]
    detail["artifacts"] = rows(connection, "select kind, path, sha256, status, bytes, first_seen, last_seen "
                                           "from artifacts where run_id=? order by first_seen", run_id or "")
    keys = [video.get("source"), run_id, *(a["path"] for a in detail["artifacts"])]
    detail["events"] = rows(connection, "select at, kind, key, event, detail from events where key in "
                            f"({','.join('?' * len(keys))}) order by id desc limit 40", *[k for k in keys if k])
    return detail


def markdown(index):
    cell = lambda item: item["label"] + (f"（{item['versions']} 版）" if item and item.get("versions", 0) > 1 else "") if item else "—"

    def card(video):
        own = cell(video.get("datacard"))
        joined = "；".join(f"系列卡「{s['name']}」{s['label']}" for s in video.get("series", []))
        return joined if own == "—" and joined else (f"{own}／{joined}" if joined else own)
    scanned = (index["scanned_at"] or "")[:16].replace("T", " ")
    lines = ["# 工作索引", "",
             f"掃描時間：{scanned} UTC。由 `.\\run.ps1 catalog` 產生，每次透過 run.ps1 處理後自動更新，請勿手動修改。", "",
             "| 影片 | 逐字稿 | 資料卡 | 講義 |", "| --- | --- | --- | --- |"]
    for video in index["videos"]:
        name = video["name"].replace("|", "｜")
        if video.get("source_status") == "missing":
            name += "（原檔已移除）"
        if video.get("note"):
            name += f"（{video['note']}）"
        lines.append(f"| {name} | {cell(video['transcript'])} | {card(video)} | {cell(video.get('handout'))} |")
    if index["orphans"]:
        lines += ["", "## 原檔已不在原檔資料夾的任務", "", "| 影片 | 逐字稿 | 資料卡 | 講義 |", "| --- | --- | --- | --- |"]
        for video in index["orphans"]:
            lines.append(f"| {video['name']} | {cell(video['transcript'])} | {card(video)} | {cell(video.get('handout'))} |")
    if index.get("series"):
        lines += ["", "## 系列資料卡", "", "| 系列 | 收錄影片 | 狀態 | 成品 |", "| --- | --- | --- | --- |"]
        for item in index["series"]:
            members = "；".join(f"{p['part']}. {p['title'].replace('|', '｜')}" for p in item["parts"])
            output = (item["output"] or "—").replace("|", "｜")
            versions = f"（{item['versions']} 版）" if item["versions"] > 1 else ""
            lines.append(f"| {item['name'].replace('|', '｜')} | {members} | {item['label']}{versions} | {output} |")
    gone = index["removed"]
    if any(gone.values()):
        lines += ["", "## 已移除或被取代的紀錄（保留備查）", ""]
        for source in gone["sources"]:
            lines.append(f"- 原檔已移除：{source['name']}（最後出現 {source['last_seen'][:10]}）")
        for output in gone["outputs"]:
            lines.append(f"- 成品已移除：{output['path']}")
        for run in gone["runs"]:
            lines.append(f"- 任務資料夾不見了：{run['run_id']}")
        for item in gone["superseded"]:
            lines.append(f"- 有舊版本：{item['path']}（{item['versions']} 個被取代的版本）")
    todo = sum(1 for v in index["videos"] if v["transcript"]["status"] is None)
    lines += ["", f"共 {len(index['videos'])} 支原檔，未轉錄 {todo} 支。"]
    return "\n".join(lines) + "\n"


def find_run(connection, value, root=ROOT):
    """Run folder for a run ID, run path, source path or title; explains what to do otherwise."""
    root = Path(root)
    candidate = Path(value)
    for path in (candidate, root / candidate, root / "工作資料/任務/轉錄" / str(value)):
        if (path / "state.json").is_file():
            return path.resolve()
    entries = (lambda i: i["videos"] + i["orphans"])(overview(connection))
    target = None
    for entry in entries:
        if entry.get("source") and Path(entry["source"]).name == candidate.name:
            target = entry
            break
    matches = [target] if target else search(connection, str(value))["videos"]
    if not matches:
        raise ValueError(f"索引中找不到影片或轉錄任務：{value}（先執行 .\\run.ps1 catalog 查看）")
    if len(matches) > 1:
        raise ValueError("符合多支影片，請指定：" + "；".join(m["name"] for m in matches[:8]))
    video = matches[0]
    if not video.get("run_dir"):
        raise ValueError(f"「{video['name']}」尚未轉錄：先走逐字稿流程（process），完成後再做資料卡或講義")
    if video["transcript"]["status"] not in FINISHED:
        raise ValueError(f"「{video['name']}」的逐字稿尚未完成（{video['transcript']['label']}），先完成逐字稿流程")
    return (root / video["run_dir"]).resolve()
