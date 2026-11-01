"""Persistent CLI orchestration; expensive work and model review are separate."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import uuid
from urllib.parse import quote
import wave

from . import compact, media, review
import workspace_paths as locations

ROOT = locations.ROOT


def _now():
    return datetime.now(timezone.utc).isoformat()


def _review_task_id(explicit=None):
    """Use the host conversation as one review task; callers may supply it elsewhere."""
    host_value = None
    host_name = None
    for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = os.environ.get(name)
        if value and value.strip():
            host_value = value.strip()
            host_name = name
            break
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise ValueError("task ID must be a nonempty string")
        explicit = explicit.strip()
        if host_value and explicit != host_value:
            raise ValueError(f"--task-id cannot override the active host session from {host_name}")
        return explicit
    return host_value


def _reject_model_source_dump(operation):
    if _review_task_id():
        raise ValueError(
            f"{operation} exposes a full legacy source view and is disabled in model sessions; "
            "use focused-review, targeted evidence-query, draft-excerpt, or the single read-draft candidate")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _root_path(value):
    return locations.resolve(value, ROOT)


def _portable(path):
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def _config():
    config = _read(ROOT / "system/config/default.json")
    for key in ("ffmpeg", "ffprobe", "whisper"):
        if not isinstance(config.get("tools", {}).get(key), str):
            raise ValueError(f"config.tools.{key} is required")
    return config


def _glossary():
    path = ROOT / "system/config/glossary.txt"
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")] if path.exists() else []


@contextmanager
def _lock():
    """OS releases this lock on exit/crash; a leftover file is not a stale lock."""
    directory = ROOT / locations.RUNS
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".workflow.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("另一個工作流正在執行；請等候它釋放 GPU／寫入鎖。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_run(run_dir, *, allow_pending_commit=False):
    run = _root_path(run_dir)
    state = _read(run / "state.json")
    if not isinstance(state.get("run_id"), str):
        raise ValueError("Invalid state: missing run_id")
    if (run / "review_commit_pending.json").is_file() and not allow_pending_commit:
        raise ValueError("Final review publication is interrupted; retry finish-review for the same draft first")
    return run, state


def _save_state(run, state):
    # State is the current index; original evidence and identity remain unchanged.
    state["source"] = _portable(_root_path(state["source"]))
    for container, keys in (
        (state, ("primary_transcript",)),
        (state.get("visual_review", {}), ("output", "structured_output", "woven_output")),
        (state.get("exports", {}), ("transcripts", "evidence")),
    ):
        for key in keys:
            if container.get(key):
                container[key] = _portable(_root_path(container[key]))
    state["updated_at"] = _now()
    _write(run / "state.json", state)
    _write(run / "task.json", {
        "task_id": state["run_id"], "workflow": "video-transcribe",
        "source": _portable(_root_path(state["source"])),
        "source_sha256": state["identity"]["source_sha256"],
        "status": state["status"], "updated_at": state["updated_at"],
        "primary_transcript": state.get("primary_transcript"),
        "state": _portable(run / "state.json"),
    })


def _artifact(run, path):
    path = Path(path).resolve()
    return {"path": path.relative_to(run).as_posix(), "sha256": _hash(path)}


def _stage_valid(run, stage):
    if stage.get("status") != "done" or not stage.get("artifacts"):
        return False
    for artifact in stage["artifacts"]:
        path = (run / artifact["path"]).resolve()
        if not path.is_relative_to(run) or not path.is_file() or _hash(path) != artifact["sha256"]:
            return False
    return True


def _stage(run, state, name, action):
    previous = state["stages"].get(name, {})
    if _stage_valid(run, previous):
        return
    if previous.get("status") == "done":
        error = f"{name} 的既有證據遺失或雜湊不同；為保護原稿，拒絕覆寫。請還原證據或建立新 run。"
        state.update({"status": "failed", "error": error})
        _save_state(run, state)
        raise RuntimeError(error)
    stage = {"status": "running", "started_at": _now(), "attempt": previous.get("attempt", 0) + 1}
    state["stages"][name] = stage
    _save_state(run, state)
    try:
        artifacts = action(stage["attempt"])
        stage.update({"status": "done", "completed_at": _now(),
                      "artifacts": [_artifact(run, path) for path in artifacts]})
        state.pop("error", None)
    except Exception as exc:
        stage.update({"status": "failed", "error": str(exc), "failed_at": _now()})
        state.update({"status": "failed", "error": str(exc)})
        _save_state(run, state)
        raise
    _save_state(run, state)


def _identity(source, config):
    source_hash = _hash(source)
    locks = {}
    for relative in ("system/config/requirements.lock.txt", "system/config/bootstrap-lock.json", "system/config/glossary.txt"):
        path = _root_path(relative)
        locks[relative] = _hash(path) if path.is_file() else None
    code = {path.name: _hash(path) for path in sorted((ROOT / "system/src").rglob("*.py"))}
    tool_hashes = {name: _hash(_root_path(path)) for name, path in config["tools"].items()}
    identity = {"source_sha256": source_hash, "source_size": source.stat().st_size,
                "model_sha256": _hash(_root_path(config["model"])), "config": config,
                "dependency_locks": locks, "tool_sha256": tool_hashes, "code_sha256": code}
    digest = _json_hash(identity)
    return source_hash[:12] + "-" + digest[:8], identity


def _record(run):
    path = run / "review_state.json"
    return _read(path) if path.is_file() else {"audit": [], "decisions": {}, "applied_documents": []}


def _base(run):
    record = _record(run)
    # One atomic record is authoritative; corrected JSON is an export mirror.
    return record.get("transcript") or _read(run / "transcript.raw.json")


def _rechecks(run):
    result = []
    for path in sorted((run / "rechecks").glob("*/result.json")):
        item = _read(path)
        if item.get("run_id") == _read(run / "state.json")["run_id"]:
            result.append(item)
    return result


def _review_counts(run, state):
    from .reading import UNRESOLVED

    def open_(decision):
        # A partial fix is a correction that still carries 〔？〕 after the uncertain words.
        return decision["decision"] == "needs_review" or UNRESOLVED in decision.get("new_text", "")

    suspects = [item["segment_id"] for item in _read(run / "triage.json")["suspects"]]
    decisions = _record(run).get("decisions", {})
    suspects += [decision["segment_id"] for decision in decisions.values()
                 if open_(decision) and decision["segment_id"] not in suspects]
    unresolved = [sid for sid in suspects if str(sid) not in decisions or open_(decisions[str(sid)])]
    state["review"] = {"suspect_count": len(suspects), "resolved_count": len(suspects) - len(unresolved),
                       "unresolved_ids": unresolved,
                       "pending_count": sum(str(sid) not in decisions for sid in suspects),
                       "needs_review_count": sum(str(sid) in decisions and open_(decisions[str(sid)]) for sid in suspects)}
    return unresolved


def _segment_reading(segment):
    if segment is None:
        return "（無）"
    return f"{segment['id']} [{segment['start']:.2f}–{segment['end']:.2f}s]\n{segment['text']}"


def _write_review_packet(run, bundle, output=None):
    output = output or run / "review_packet.md"
    suspects = {item["segment_id"]: item for item in _read(run / "triage.json")["suspects"]}
    lines = ["# 局部鑑字複核閱讀包", "", f"run_id: {bundle['run_id']}",
             f"transcript_sha256: {bundle['transcript_sha256']}", "",
             "原文逐字保留；低分是解碼分數，並非正確率。畫面／詞彙只提供線索；未聽證不可宣稱已確認發音。",
             "疑點無法解決時保留原文並回覆 needs_review。精確 patch 欄位見同目錄 review_bundle.json。",
             f"[原始音訊]({_link(output.parent, run / 'audio.wav')})", ""]
    for item in bundle["items"]:
        sid = item["segment_id"]
        low = item.get("low_score_tokens", suspects.get(sid, {}).get("low_score_tokens", []))
        lines += [f"## {sid}", "", "### 原文", "", _segment_reading(item["segment"]), "",
                  "前句：", _segment_reading(item.get("previous")), "",
                  "後句：", _segment_reading(item.get("next")), ""]
        low_text = "、".join(f"{token['text']} (p={token['p']:.3f})" for token in low)
        lines += ["低分文字：" + (low_text or "（無）")]
        reasons = [reason if isinstance(reason, str) else str(reason.get("detail", reason))
                   for reason in item.get("reasons", [])]
        lines += ["原因：" + "；".join(reasons), "證據 IDs：" + ", ".join(item["evidence_ids"]), "", "候選："]
        if not item.get("candidates"):
            lines += ["（無）"]
        for candidate in item.get("candidates", []):
            if candidate.get("kind") == "same_model_recheck":
                lines += [f"- {candidate['evidence_id']}：同模型局部較寬 beam 重辨，非獨立證據。"]
                lines += [_segment_reading(segment) for segment in candidate["segments"]]
            else:
                lines += [f"- {candidate.get('original', '')} → {candidate.get('suggestion', '')}；"
                          f"相似度 {candidate.get('similarity', '未提供')}；證據 {candidate.get('evidence_id', '')}。不可自動套用。"]
        lines.append("")
    lines += ["# 共用畫面與詞彙證據", "", "檔案路徑以工作區根目錄為基準。OCR 不等同實際講述。", ""]
    seen = set()
    for evidence in bundle["evidence"]:
        eid = evidence["id"]
        if eid in seen:
            continue
        seen.add(eid)
        if evidence.get("kind") == "video_frame":
            lines += [f"## {eid} [{evidence['time']:.2f}s]", "", f"路徑：{evidence['path']}",
                      f"[查看畫面]({_link(output.parent, _root_path(evidence['path']))})", "",
                      evidence.get("text") or "（無 OCR 文字）", ""]
        elif evidence.get("kind") == "user_glossary":
            lines += [f"- {eid}：{evidence['text']}"]
        elif evidence.get("kind") == "same_model_recheck":
            lines += [f"- {eid}：[局部音訊]({_link(output.parent, _root_path(evidence['audio_path']))})；"
                      "同模型重辨，未經人工聽證。"]
    text = "\n".join(lines)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return len(text)


def _bundle(run, state):
    transcript, visuals = _base(run), _read(run / "visuals.json")
    triage = _read(run / "triage.json")
    decisions = _record(run).get("decisions", {})
    pending = {**triage, "suspects": [item for item in triage["suspects"]
        if decisions.get(str(item["segment_id"]), {}).get("decision") not in ("keep", "correct")]}
    pending["suspects"] = copy.deepcopy(pending["suspects"])
    by_id = {item["segment_id"]: item for item in pending["suspects"]}
    source_evidence_ids = {frame["id"] for frame in visuals["frames"]}
    source_evidence_ids.update(item["id"] for item in triage.get("glossary_evidence", []))
    for decision in decisions.values():
        if decision["decision"] != "needs_review":
            continue
        sid = decision["segment_id"]
        if sid not in by_id:
            by_id[sid] = {"segment_id": sid, "reasons": [], "candidates": [],
                          "evidence_ids": [eid for eid in decision["evidence_ids"] if eid in source_evidence_ids]}
            pending["suspects"].append(by_id[sid])
        by_id[sid]["reasons"].append({"kind": "manual_needs_review", "detail": decision["reason"]})
    bundle = review.make_review_bundle(transcript, visuals, pending, state["run_id"])
    bundle["paths_relative_to"] = "workspace_root"
    bundle["transcript_sha256"] = _json_hash(transcript)
    bundle["patch_schema"]["run_id"] = state["run_id"]
    rechecks = {item["segment_id"]: item for item in _rechecks(run)}
    for item in bundle["items"]:
        sid = item["segment_id"]
        evidence_id = "audio:" + str(sid)
        evidence = {"id": evidence_id, "kind": "original_audio", "path": _portable(run / "audio.wav"),
                    "start": item["segment"]["start"], "end": item["segment"]["end"],
                    "audio_verified": False}
        bundle["evidence"].append(evidence)
        bundle["evidence_ids"].append(evidence_id)
        item["evidence_ids"].append(evidence_id)
        if sid in rechecks:
            result = rechecks[sid]
            item.setdefault("candidates", []).append({"kind": "same_model_recheck", "auto_apply": False,
                                                       "evidence_id": result["id"], "segments": result["segments"]})
            item["evidence_ids"].append(result["id"])
            bundle["evidence"].append(result)
            bundle["evidence_ids"].append(result["id"])
    bundle["stats"]["review_packet_characters"] = _write_review_packet(run, bundle)
    _write(run / "review_bundle.json", bundle)
    return bundle


def _context_basis(run):
    """Bind a semantic review to both current speech and reviewed visual cards."""
    path = run / "visual_review.json"
    return {"transcript_sha256": _json_hash(_base(run)),
            "visual_review_sha256": _json_hash(_read(path)) if path.is_file() else None}


def _context_bundle(run, state):
    from . import reading
    _reject_model_source_dump("context-bundle")
    transcript = _base(run)
    path = run / "visual_review.json"
    if not path.is_file() or _hash(path) != state.get("visual_review", {}).get("record_sha256"):
        raise ValueError("Finalize reviewed visual cards before context-bundle")
    reviewed = _read(path)
    if reviewed["visuals_sha256"] != _json_hash(_read(run / "visuals.json")):
        raise ValueError("Reviewed visual cards are stale")
    decisions = _record(run).get("decisions", {})
    segments = [{"id": s["id"], "start": s["start"], "end": s["end"], "text": s["text"],
                 "decision": decisions.get(str(s["id"]), {}).get("decision")}
                for s in transcript["segments"]]
    layout_path = run / "reading_layout.json"
    terms = _read(layout_path).get("protected_terms", []) if layout_path.is_file() else []
    cards = [{"id": c["id"], "start": c["start"], "end": c["end"], "title": c["title"],
              "frame_ids": c["frame_ids"], "text": reading.visual_body(c, protected_terms=terms)}
             for c in reviewed["cards"]]
    bundle = {"run_id": state["run_id"], **_context_basis(run), "segments": segments, "cards": cards,
              "context_review_schema": {**_context_basis(run),
                  "reviewed_segment_ids": [s["id"] for s in segments],
                  "reviewed_card_ids": [c["id"] for c in cards]}}
    _write(run / "context_bundle.json", bundle)
    lines = ["# 全文上下文與畫面交叉校字包", "", f"run_id: {state['run_id']}",
             "與分段排版共用這次全文閱讀；所有段落都檢查，不限低分或未決疑點。",
             "上下文、同音術語、同段原局及前後一致性可支持最小更正，不必假裝已聽音。",
             "畫面缺字可提出候選並回查圖片；不可把推論直接冒充畫面可見文字。",
             "只回傳差異 patches；未解用 needs_review。完整閱讀後才提交 context_review 覆蓋紀錄。",
             "文字證據 ID 為 text:<segment_id>，畫面證據用 frame_id；分數不是正確保證。", "", "## 正文", ""]
    for s in segments:
        lines.append(f"{s['id']} [{s['start']:.2f}–{s['end']:.2f}] {s['text']}" +
                     (f" 〔{s['decision']}〕" if s["decision"] else ""))
    lines += ["", "## 已轉錄畫面", ""]
    for c in cards:
        lines += [f"### {c['id']} [{c['start']:.2f}–{c['end']:.2f}] {c['title']}",
                  "frame_ids: " + ", ".join(c["frame_ids"]), "", c["text"], ""]
    (run / "context_packet.md").write_text("\n".join(lines), encoding="utf-8")
    return {"run_id": state["run_id"], "context_packet": _portable(run / "context_packet.md"),
            "context_bundle": _portable(run / "context_bundle.json"), "segment_count": len(segments),
            "card_count": len(cards), "text_characters": sum(len(s["text"]) for s in segments)}


def _chapters(run, state):
    transcript, visuals = _base(run), _read(run / "visuals.json")
    transcript_hash = _json_hash(transcript)
    chunks = review.make_chapter_bundles(transcript, visuals, state["config"].get("chapter_seconds", 300))
    entries = []
    for chunk in chunks:
        chunk.update({"run_id": state["run_id"], "paths_relative_to": "workspace_root", "transcript_sha256": transcript_hash})
        path = run / "chapters" / (chunk["id"] + ".json")
        _write(path, chunk)
        reading_path = path.with_suffix(".md")
        lines = [f"# {chunk['id']} 章節閱讀包", "", f"run_id: {state['run_id']}",
                 f"transcript_sha256: {transcript_hash}", f"時間：{chunk['start']:.2f}–{chunk['end']:.2f} 秒", "",
                 "本檔完整呈現本章來源，尚未摘要。講述與畫面分開；講義每點需附來源 ID。", "",
                 "## 逐字稿", ""]
        for segment in chunk["segments"]:
            lines += [_segment_reading(segment), ""]
        lines += ["## 畫面 OCR", "", "抽樣畫面可能不完整；OCR 文字不是已確認的發言。", ""]
        for frame in chunk["frames"]:
            lines += [f"### {frame['id']} [{frame['time']:.2f}s]", "", f"路徑：{frame['path']}",
                      f"[查看畫面]({_link(reading_path.parent, _frame_path(run, frame))})", "",
                      frame.get("text") or "（無 OCR 文字）", ""]
        reading_path.write_text("\n".join(lines), encoding="utf-8")
        entries.append({"id": chunk["id"], "start": chunk["start"], "end": chunk["end"],
                        "path": path.relative_to(run).as_posix(), "reading_path": reading_path.relative_to(run).as_posix(),
                        "reading_sha256": _hash(reading_path), **chunk["coverage"]})
    manifest = {"run_id": state["run_id"], "transcript_sha256": transcript_hash,
                "chapters": entries, "status": "awaiting_agent_authoring",
                "instruction": "逐章閱讀證據並撰寫講義。完整理解仍需模型閱讀各章內容；這些檔案不是已完成摘要。"}
    _write(run / "manifest.json", manifest)
    return manifest


def _link(destination, target):
    return quote(Path(os.path.relpath(target, destination)).as_posix(), safe="/._-#")


def _frame_path(run, frame):
    return _root_path(frame["path"])


def _time_label(seconds):
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _time_range(start, end):
    first, last = _time_label(start), _time_label(end)
    return first if first == last else first + "–" + last


def _normalized_export(transcript, directory, basename, protected_terms=()):
    from . import reading
    normalized = copy.deepcopy(transcript)
    for segment in normalized["segments"]:
        segment["text"] = reading.traditional(segment["text"], protected_terms, punctuation=False)
        segment["token_alignment_matches_text"] = False
    result = review.export_transcripts(normalized, directory, basename + ".zh-TW")
    _write(directory / (basename + ".zh-TW.metadata.json"), {
        "normalization": "OpenCC s2tw + config/taiwan_terms.txt", "taiwan_terms_sha256": _hash(reading.TAIWAN_TERMS),
        "protected_terms": list(protected_terms), "source_transcript_sha256": _json_hash(transcript),
        "note": "此副本僅正規化繁體字形與台灣用語；不是鑑字更正，不作為 patch old_text 來源。",
        "files": {key: Path(value).name for key, value in result.items()}})


def _exports(run, state):
    run_id = state["run_id"]
    transcript_out = run / "exports/transcripts"
    layout_path = run / "reading_layout.json"
    terms = _read(layout_path).get("protected_terms", []) if layout_path.is_file() else []
    raw = _read(run / "transcript.raw.json")
    review.export_transcripts(raw, transcript_out, "raw")
    _normalized_export(raw, transcript_out, "raw", terms)
    record = _record(run)
    if record.get("transcript"):
        _write(run / "transcript.corrected.json", record["transcript"])
        review.export_transcripts(record["transcript"], transcript_out, "corrected")
        _normalized_export(record["transcript"], transcript_out, "corrected", terms)
    # Review evidence stays in the run; AI筆記與資料卡輸出 belongs to the downstream workflow.
    lines = ["# 畫面文字證據", "", "場景變化與定時取樣仍可能漏掉短暫畫面。以下是 OCR，未經人工確認。", ""]
    for frame in _read(run / "visuals.json")["frames"]:
        label = f"{frame['id']} · {frame['time']:.2f} 秒"
        lines += [f"## {label}", "", f"[查看畫面]({_link(run, _frame_path(run, frame))})", ""]
        if frame.get("duplicate_of"):
            lines += [f"與 {frame['duplicate_of']} 像素完全相同；沿用該畫面的 OCR。", ""]
        lines += [frame.get("text") or "（未辨識到文字）", ""]
    (run / "visuals.md").write_text("\n".join(lines), encoding="utf-8")
    return {"transcripts": _portable(transcript_out), "evidence": _portable(run)}


def _primary_accepted(run, state, woven_md, visual_review):
    """The exported primary, or a user-edited version of it with the same paragraph structure.

    Returns (accepted, structure_problem). User edits are authoritative: they are
    recorded and snapshotted, never treated as a missing visual transcription.
    """
    if not woven_md.is_file():
        state.pop("primary_user_edits", None)
        return False, None
    current = _hash(woven_md)
    if current == visual_review.get("woven_output_sha256"):
        state.pop("primary_user_edits", None)
        return True, None
    mapping_path = run / "reading_map.json"
    if not visual_review.get("woven_output_sha256") or not mapping_path.is_file():
        return False, None
    from derived_common import primary as user_primary
    problem, edits = user_primary.compare(woven_md.read_text(encoding="utf-8"), _read(mapping_path)["blocks"])
    if problem:
        state.pop("primary_user_edits", None)
        return False, problem
    user_primary.snapshot(run, woven_md, "偵測到使用者修改")
    previous = state.get("primary_user_edits") or {}
    state["primary_user_edits"] = {
        "primary_sha256": current, "paragraphs": [e["paragraph"] for e in edits if not e.get("removed")],
        "removed_paragraphs": [e["paragraph"] for e in edits if e.get("removed")],
        "detected_at": previous.get("detected_at") if previous.get("primary_sha256") == current else _now(),
        "history": user_primary.HISTORY}
    return True, None


def _status(run, state):
    if not (run / "triage.json").is_file():
        return state
    unresolved = _review_counts(run, state)
    visual_hash = _json_hash(_read(run / "visuals.json"))
    state["visuals_sha256"] = visual_hash
    visual_review = state.get("visual_review", {})
    visual_md = _root_path(visual_review.get("output", "."))
    visual_json = _root_path(visual_review.get("structured_output", "."))
    woven_md = _root_path(visual_review.get("woven_output", "."))
    woven_ok, primary_problem = _primary_accepted(run, state, woven_md, visual_review)
    valid_visuals = (visual_review.get("visuals_sha256") == visual_hash
                     and visual_md.is_file() and visual_json.is_file()
                     and _hash(visual_md) == visual_review.get("output_sha256")
                     and _hash(visual_json) == visual_review.get("structured_output_sha256")
                     and woven_ok
                     and _json_hash(_base(run)) == visual_review.get("woven_transcript_sha256"))
    context = _record(run).get("context_review", {})
    valid_context = bool(context) and all(context.get(k) == v for k, v in _context_basis(run).items())
    state["context_review"] = {"completed": valid_context, "recorded_at": context.get("recorded_at"),
                               "method": context.get("method", "agent_context_and_visual_review"), "audio_verified": False}
    state.pop("review_task", None)
    task_id = _review_task_id()
    if task_id:
        final_path = run / "final_review_tasks.json"
        focus_path = run / "focused_review_tasks.json"
        final_entry = None
        resumable = None
        focused = False
        if final_path.is_file():
            task_ledger = _read(final_path)
            final_entry = next((item for item in task_ledger.get("tasks", [])
                                if item.get("task_id") == task_id), None)
            resumable = next((item for item in reversed(task_ledger.get("tasks", []))
                              if item.get("status") in ("active", "final_candidate_ready", "full_read_open")
                              and item.get("draft_dir")
                              and (item.get("readiness_receipt") or item.get("read_receipt")
                                   or item.get("finish_attempted_at"))), None)
        if focus_path.is_file():
            focus_ledger = _read(focus_path)
            focused = task_id in focus_ledger.get("tasks", {})
        if final_entry or focused or resumable:
            state["review_task"] = {
                "task_id": task_id, "focused_review_completed": focused,
                "status": (final_entry.get("status") if final_entry else
                            ("checkpoint_available" if resumable else "evidence_review")),
                "draft": final_entry.get("draft_dir") if final_entry else None,
                "final_candidate_sealed": bool(final_entry and final_entry.get("readiness_receipt")),
                "full_candidate_shown": bool(final_entry and final_entry.get("read_receipt")),
                "review_checkpoint_available": bool(final_entry and final_entry.get("checkpoint_read_receipt")),
                "finish_attempted": bool(final_entry and final_entry.get("finish_attempted_at"))}
            if not final_entry and resumable:
                state["review_task"]["resume_checkpoint"] = {
                    "draft": resumable["draft_dir"],
                    "final_candidate_sealed": bool(resumable.get("readiness_receipt")),
                    "full_candidate_reviewed": bool(resumable.get("read_receipt")),
                    "finish_attempted": bool(resumable.get("finish_attempted_at"))}
    if state["review"]["pending_count"]:
        state["status"] = "awaiting_review"
    elif primary_problem:
        state["status"] = "primary_structure_changed"
    elif not valid_visuals:
        state["status"] = "awaiting_visuals"
    elif not valid_context:
        state["status"] = "awaiting_context_review"
    else:
        state["status"] = "completed_with_unresolved" if unresolved or visual_review.get("uncertainty_count") else "completed"
    state["message"] = {"primary_structure_changed": f"逐字稿主稿的段落結構被改動：{primary_problem}。"
                                                     "恢復段落結構，或從 run 的 primary_history 取回舊版。",
                        "awaiting_review": "本機轉錄與畫面整理完成，仍有逐字稿疑點尚未記錄複核決議。",
                        "awaiting_visuals": "逐字稿疑點決議已記錄，等待完整涵蓋抽樣畫面的視覺轉錄。",
                        "awaiting_context_review": "畫面轉錄已完成，請用 start-draft 累積前置修正、seal-draft 封存，再用 read-draft 閱讀一次並由 finish-review 批次交付。",
                        "completed_with_unresolved": "逐字稿與畫面轉錄已輸出，未解文字／畫面疑點仍明確保留。",
                        "completed": "逐字稿複核與畫面轉錄已完成。"}[state["status"]]
    state["completion_requirements"] = {"all_asr_suspects_decided": state["review"]["pending_count"] == 0,
                                        "sampled_frames_transcribed": valid_visuals,
                                        "full_context_review_recorded": valid_context,
                                        "visual_uncertainty_count": visual_review.get("uncertainty_count", 0),
                                        "notes_required": False}
    _save_state(run, state)
    return state


def _refresh(run, state):
    _bundle(run, state)
    state["exports"] = _exports(run, state)
    record = run / "visual_review.json"
    visual_review = state.get("visual_review", {})
    if (record.is_file() and _hash(record) == visual_review.get("record_sha256")
            and visual_review.get("visuals_sha256") == _json_hash(_read(run / "visuals.json"))):
        document = _read(record)
        output, structured = _visual_exports(run, document)
        state["visual_review"].update({
            "output": _portable(output), "output_sha256": _hash(output),
            "structured_output": _portable(structured), "structured_output_sha256": _hash(structured)})
        _weave_visuals(run, state, document["cards"])
    _status(run, state)


_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".ts", ".wmv"}
_RESOLUTION_SUFFIX = re.compile(r"[ _.-]*\(?\d{3,4}[pP]\)?$")
_DURATION_TOLERANCE = 1.0


def inspect_targets(sources):
    """Read metadata for explicit targets and same-name siblings; never select or skip a file."""
    targets = [_root_path(source) for source in sources]
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("請指定不重複的單檔或批次目標")
    for path in targets:
        if not path.is_file():
            raise FileNotFoundError(path)
    name_key = lambda path: _RESOLUTION_SUFFIX.sub("", path.stem).casefold()
    selected = set(targets)
    paths = set(targets)
    for source in targets:
        if source.suffix.lower() in _VIDEO_SUFFIXES:
            paths.update(path.resolve() for path in source.parent.iterdir()
                         if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES
                         and name_key(path) == name_key(source))
    ffprobe = _root_path(_config()["tools"]["ffprobe"])
    files = []
    for path in sorted(paths):
        info = media.probe(ffprobe, path)
        files.append({"path": _portable(path), "selected": path in selected,
                      "name": name_key(path), "duration": info["duration"],
                      "is_video": path.suffix.lower() in _VIDEO_SUFFIXES
                      and any(s.get("codec_type") == "video" for s in info.get("streams", []))})
    matches = []
    for index, first in enumerate(files):
        for second in files[index + 1:]:
            if not (first["selected"] or second["selected"]):
                continue
            if (first["is_video"] and second["is_video"] and first["name"] == second["name"]
                    and abs(first["duration"] - second["duration"]) <= _DURATION_TOLERANCE):
                matches.append({"paths": [first["path"], second["path"]],
                                "duration_difference": abs(first["duration"] - second["duration"])})
    return {"status": "needs_decision" if matches else "ready",
            "targets": [_portable(path) for path in targets], "files": files,
            "possible_matches": matches, "duration_tolerance_seconds": _DURATION_TOLERANCE,
            "message": "檔名與片長符合；請先詢問使用者要處理哪些檔案。這不是內容相同的判定。" if matches
            else "未發現同名同片長候選，可依指定清單處理。"}


def process(source, language=None, device=None, interval=None, *, resume_run=None):
    source = _root_path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    if resume_run is not None:
        run, previous_state = _load_run(resume_run)
        config, identity = previous_state["config"], previous_state["identity"]
        run_id = previous_state["run_id"]
        if _hash(source) != identity["source_sha256"] or source.stat().st_size != identity["source_size"]:
            raise RuntimeError("原始影片已變更，不能沿用此 run")
        if _hash(_root_path(config["model"])) != identity["model_sha256"]:
            raise RuntimeError("ASR 模型已變更，不能沿用此 run")
        for name, expected in identity["tool_sha256"].items():
            if _hash(_root_path(config["tools"][name])) != expected:
                raise RuntimeError(f"工具 {name} 已變更，不能沿用此 run")
        for relative, expected in identity["dependency_locks"].items():
            path = _root_path(relative)
            actual = _hash(path) if path.is_file() else None
            if actual != expected:
                raise RuntimeError(f"依賴／詞彙鎖 {relative} 已變更，不能沿用此 run")
    else:
        config = _config()
        for key, value in (("language", language), ("device", device), ("frame_interval", interval)):
            if value is not None:
                config[key] = value
        run_id, identity = _identity(source, config)
        run = ROOT / locations.RUNS / run_id
    with _lock():
        run.mkdir(parents=True, exist_ok=True)
        state_path = run / "state.json"
        if state_path.exists():
            _, state = _load_run(run)
            if state.get("identity") != identity:
                raise RuntimeError("Run identity mismatch")
        else:
            state = {"run_id": run_id, "created_at": _now(), "source": _portable(source),
                     "identity": identity, "config": config, "stages": {}, "status": "running"}
            _save_state(run, state)
        if resume_run is not None:
            state["last_resume"] = {"at": _now(), "original_identity_preserved": True,
                                    "current_code_sha256": {path.name: _hash(path) for path in sorted((ROOT / "system/src").rglob("*.py"))}}
        tools = {name: _root_path(path) for name, path in config["tools"].items()}
        def do_probe(attempt):
            _write(run / "probe.json", media.probe(tools["ffprobe"], source))
            return [run / "probe.json"]
        _stage(run, state, "probe", do_probe)
        def do_audio(attempt):
            temporary = run / "work" / f"audio-{attempt:03d}.wav"
            media.extract_audio(tools["ffmpeg"], source, temporary)
            os.replace(temporary, run / "audio.wav")
            return [run / "audio.wav"]
        _stage(run, state, "audio", do_audio)
        def do_asr(attempt):
            out = run / "work" / f"asr-{attempt:03d}"
            transcript = media.transcribe(tools["whisper"], _root_path(config["model"]), run / "audio.wav", out,
                                          config.get("language", "zh"), config.get("device", "auto"), config.get("threads", 4))
            transcript.update({"run_id": run_id, "source_sha256": identity["source_sha256"]})
            # Native JSON/log locations stay portable within the run.
            for key in ("raw_json", "stdout_log", "stderr_log"):
                if key in transcript:
                    transcript[key] = _portable(transcript[key])
            transcript["paths_relative_to"] = "workspace_root"
            _write(run / "transcript.raw.json", transcript)
            return [run / "transcript.raw.json"] + list(out.glob("*.json")) + list(out.glob("*.log"))
        _stage(run, state, "asr", do_asr)
        def do_visuals(attempt):
            out = run / "work" / f"visuals-{attempt:03d}"
            visuals = media.extract_visuals(tools["ffmpeg"], source, out, config.get("frame_interval", 10),
                                            config.get("dedup_threshold", 5))
            for frame in visuals["frames"]:
                frame["path"] = _portable(frame["path"])
            for key, path in visuals.get("model_paths", {}).items():
                visuals["model_paths"][key] = _portable(path)
            visuals.update({"run_id": run_id, "paths_relative_to": "workspace_root"})
            _write(run / "visuals.json", visuals)
            return [run / "visuals.json"] + list(out.rglob("*.png"))
        _stage(run, state, "visuals", do_visuals)
        def do_triage(attempt):
            result = review.triage(_read(run / "transcript.raw.json"), _read(run / "visuals.json"), config, _glossary())
            _write(run / "triage.json", result)
            return [run / "triage.json"]
        _stage(run, state, "triage", do_triage)
        _refresh(run, state)
        return {"run_dir": str(run), "run_id": run_id, "status": state["status"],
                "message": state["message"], "review": state["review"], "exports": state["exports"],
                "primary_transcript": state.get("primary_transcript")}


def resume(run_dir):
    run, state = _load_run(run_dir)
    return process(_root_path(state["source"]), resume_run=run)


def apply(run_dir, patch_path):
    run, state = _load_run(run_dir)
    document = _read(_root_path(patch_path))
    if not isinstance(document, dict) or document.get("run_id") != state["run_id"]:
        raise ValueError("patch_document.run_id 必須與此 run 完全相同")
    digest = _json_hash(document)
    with _lock():
        state = _read(run / "state.json")
        record = _record(run)
        if digest in record["applied_documents"]:
            return {"run_id": state["run_id"], "status": "already_applied", "patch_sha256": digest}
        base = _base(run)
        if "transcript_sha256" in document and document["transcript_sha256"] != _json_hash(base):
            raise ValueError("patch transcript_sha256 is stale; rebuild decisions from current text")
        bundle = _bundle(run, state)
        context = document.get("context_review")
        if context is not None:
            basis = _context_basis(run)
            if not isinstance(context, dict) or not basis["visual_review_sha256"] or any(context.get(k) != v for k, v in basis.items()):
                raise ValueError("context_review basis is stale or missing")
            if _hash(run / "visual_review.json") != state.get("visual_review", {}).get("record_sha256"):
                raise ValueError("context_review visual record was modified outside finalize-visuals")
            if context.get("reviewed_segment_ids") != [s["id"] for s in base["segments"]]:
                raise ValueError("context_review must cover every segment once in order")
            if context.get("reviewed_card_ids") != [c["id"] for c in _read(run / "visual_review.json")["cards"]]:
                raise ValueError("context_review must cover every visual card once in order")
        allowed = set(bundle["evidence_ids"])
        # Previously resolved segments may receive a later evidence-backed correction.
        allowed.update(frame["id"] for frame in _read(run / "visuals.json")["frames"])
        allowed.update(item["id"] for item in _read(run / "triage.json").get("glossary_evidence", []))
        allowed.update(item["id"] for item in _rechecks(run))
        allowed.update(item["id"] for item in _indexed_evidence(run))
        allowed.update("audio:" + str(segment["id"]) for segment in base["segments"])
        allowed.update("text:" + str(segment["id"]) for segment in base["segments"])
        corrected, changes = review.apply_patches(base, document, allowed)
        for change in changes:
            change.update({"patch_sha256": digest, "recorded_at": _now()})
            record["decisions"][str(change["segment_id"])] = change
        record["audit"].extend(changes)
        record["applied_documents"].append(digest)
        record.update({"run_id": state["run_id"], "transcript": corrected})
        if context is not None:
            record["context_review"] = {**context, "input_transcript_sha256": context["transcript_sha256"],
                "transcript_sha256": _json_hash(corrected), "recorded_at": _now(), "patch_sha256": digest,
                "method": "agent_context_and_visual_review", "audio_verified": False}
        _write(run / "review_state.json", record)
        _write(run / "patches" / (digest + ".json"), document)
        _write(run / "transcript.corrected.json", corrected)
        _write(run / "correction_audit.json", record["audit"])
        if changes:
            state.setdefault("notes", {})["stale"] = True
        _refresh(run, state)
        return {"run_id": state["run_id"], "status": state["status"], "applied": sum(c["applied"] for c in changes),
                "decisions_recorded": len(changes), "review": state["review"], "notes_stale": bool(changes),
                "primary_transcript": state.get("primary_transcript")}


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _refs(value, allowed, name, required=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
        raise ValueError(f"{name}: unknown or malformed evidence ID")
    if len(value) != len(set(value)) or (required and not value):
        raise ValueError(f"{name}: empty or duplicate references")
    return value


def finalize(run_dir, notes_path):
    """Legacy lecture-note export, excluded from workflow completion."""
    run, state = _load_run(run_dir)
    notes = _read(_root_path(notes_path))
    with _lock():
        state = _read(run / "state.json")
        manifest = _chapters(run, state)
        if not isinstance(notes, dict) or notes.get("run_id") != state["run_id"]:
            raise ValueError("notes.run_id 必須與此 run 完全相同")
        if notes.get("transcript_sha256") != manifest["transcript_sha256"]:
            raise ValueError("notes.transcript_sha256 與目前逐字稿不同；請依最新章節包重新核對講義")
        chapters = notes.get("chapters")
        expected = {entry["id"]: entry for entry in manifest["chapters"]}
        if not isinstance(chapters, list) or any(not isinstance(c, dict) for c in chapters):
            raise ValueError("notes.chapters must be a list of objects")
        ids = [chapter.get("id") for chapter in chapters]
        if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) != set(expected):
            raise ValueError("講義章節 ID 必須完整對應 manifest，不能漏章或新增未知章節")
        if not chapters:
            raise ValueError("沒有可撰寫的章節來源")
        unresolved = set(_review_counts(run, state))
        author_unresolved = set()
        for chapter in chapters:
            entry = expected[chapter["id"]]
            sids, fids = set(entry["segment_ids"]), set(entry["frame_ids"])
            _required_text(chapter.get("title"), "title")
            _required_text(chapter.get("summary"), "summary")
            if not isinstance(chapter.get("bullets"), list) or not isinstance(chapter.get("visual_notes"), list):
                raise ValueError("bullets and visual_notes must be lists")
            if (sids or fids) and not chapter["bullets"] and not chapter["visual_notes"]:
                raise ValueError("含來源的章節至少需要一則附引用的重點或畫面補充")
            for bullet in chapter["bullets"]:
                _required_text(bullet.get("text"), "bullet.text")
                sr = _refs(bullet.get("segment_ids", []), sids, "bullet.segment_ids")
                fr = _refs(bullet.get("frame_ids", []), fids, "bullet.frame_ids")
                if not sr and not fr:
                    raise ValueError("每則重點必須至少附一個章內來源引用")
            for visual in chapter["visual_notes"]:
                _required_text(visual.get("text"), "visual_note.text")
                _refs(visual.get("frame_ids"), fids, "visual_note.frame_ids", required=True)
            author_unresolved.update(_refs(chapter.get("needs_review", []), sids, "needs_review"))
        # Only after validating every chapter do we write the deliverable.
        unresolved |= author_unresolved
        destination = run / "exports/legacy-notes"
        destination.mkdir(parents=True, exist_ok=True)
        transcript_path = run / "exports/transcripts" / ("corrected.md" if _record(run).get("transcript") else "raw.md")
        frames = {f["id"]: f for f in _read(run / "visuals.json")["frames"]}
        segments = {s["id"]: s for s in _base(run)["segments"]}
        def citations(sids, fids):
            result = [f"[{sid} · {segments[sid]['start']:.2f}秒]({_link(destination, transcript_path)})" for sid in sids]
            result += [f"[{fid} · {frames[fid]['time']:.2f}秒]({_link(destination, _frame_path(run, frames[fid]))})" for fid in fids]
            return "、".join(result)
        lines = ["# 影片講義", "", f"Run：{state['run_id']}", "", "本講義依逐字稿與抽樣畫面整理；畫面補充另列，原始逐字稿保留。", ""]
        if unresolved:
            lines += ["未解疑點：" + "、".join(sorted(unresolved)) + "。相關內容仍需核對。", ""]
        by_id = {c["id"]: c for c in chapters}
        for chapter_id, entry in expected.items():
            chapter = by_id[chapter_id]
            chapter_link = _link(destination, run / entry.get("reading_path", entry["path"]))
            lines += [f"## {chapter['title']}", "", f"{entry['start']:.2f}–{entry['end']:.2f} 秒 · {chapter_id}", "",
                      f"本章摘要來源：[完整章節證據]({chapter_link})（含本章全部逐字稿及畫面 ID）。", "",
                      chapter["summary"], "", "### 講述重點", ""]
            for bullet in chapter["bullets"]:
                lines += [f"- {bullet['text']}（{citations(bullet.get('segment_ids', []), bullet.get('frame_ids', []))}）"]
            lines += ["", "### 畫面補充", ""]
            if not chapter["visual_notes"]:
                lines += ["（未另加畫面解讀）"]
            for visual in chapter["visual_notes"]:
                lines += [f"- {visual['text']}（{citations([], visual['frame_ids'])}）"]
            pending = set(entry["segment_ids"]) & unresolved
            if pending:
                lines += ["", "本章待核對：" + "、".join(sorted(pending))]
            lines.append("")
        output = destination / "講義.md"
        output.write_text("\n".join(lines), encoding="utf-8")
        _write(run / "notes.json", notes)
        state["notes"] = {"stale": False, "finalized_at": _now(), "transcript_sha256": manifest["transcript_sha256"],
                          "notes_sha256": _json_hash(notes), "output": output.relative_to(ROOT).as_posix(), "output_sha256": _hash(output),
                          "unresolved_ids": sorted(unresolved), "validation": "IDs, coverage and citations checked; semantic accuracy requires author review"}
        _status(run, state)
        return {"run_id": state["run_id"], "status": state["status"], "output": str(output),
                "legacy_notes_export": True, "unresolved_ids": sorted(unresolved)}


def finalize_visuals(run_dir, document_path):
    run, state = _load_run(run_dir)
    document = _read(_root_path(document_path))
    with _lock():
        state = _read(run / "state.json")
        visuals = _read(run / "visuals.json")
        visual_hash = _json_hash(visuals)
        if not isinstance(document, dict) or document.get("run_id") != state["run_id"]:
            raise ValueError("visuals reviewed document.run_id 必須與此 run 完全相同")
        if document.get("visuals_sha256") != visual_hash:
            raise ValueError("visuals_sha256 與目前畫面來源不同，請依最新畫面重新核對")
        cards = document.get("cards")
        if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
            raise ValueError("cards must be a list of objects")
        frames = {frame["id"]: frame for frame in visuals["frames"]}
        required_frames = set(frames)
        supplemental = {frame["id"]: frame for frame in _indexed_evidence(run)}
        frames.update(supplemental)
        known = set(frames)
        card_ids, covered, normalized = set(), [], []
        allowed_kinds = {"text", "paragraph", "slide", "table", "chart", "diagram", "scene", "caption", "other"}
        for card in cards:
            cid = _required_text(card.get("id"), "card.id")
            if cid in card_ids:
                raise ValueError("Duplicate card.id")
            card_ids.add(cid)
            if not isinstance(card.get("kind"), str) or card["kind"] not in allowed_kinds:
                raise ValueError("Unknown visual card.kind")
            _required_text(card.get("title"), "card.title")
            if card.get("reviewed") is not True:
                raise ValueError("每張卡片必須 reviewed=true，表示作者已實際查看所涵蓋畫面")
            fids = _refs(card.get("frame_ids"), known, "card.frame_ids", required=True)
            # Either field may be left out; the card still needs one of them with content.
            card = {"verbatim_text": "", "structure": "", **card}
            for key in ("verbatim_text", "structure"):
                if not isinstance(card.get(key), str):
                    raise ValueError(f"card.{key} must be text")
            if not card["verbatim_text"].strip() and not card["structure"].strip():
                raise ValueError("每張卡片至少需要可見原文或畫面結構轉錄")
            uncertainty = card.get("uncertainties")
            if not isinstance(uncertainty, list) or any(not isinstance(item, str) or not item.strip() for item in uncertainty):
                raise ValueError("card.uncertainties must be a list of nonempty text")
            covered.extend(fids)
            ordered = sorted((frames[fid] for fid in fids), key=lambda frame: (frame["time"], frame["id"]))
            normalized.append({**copy.deepcopy(card),
                "frame_ids": [frame["id"] for frame in ordered],
                "frame_times": [{"frame_id": frame["id"], "time": frame["time"], "path": frame["path"]} for frame in ordered],
                "start": ordered[0]["time"], "end": ordered[-1]["time"]})
        if len(covered) != len(set(covered)):
            raise ValueError("每個 frame 必須恰屬一卡，不能重複歸入不同卡片")
        omissions = copy.deepcopy(document.get("omissions", []))
        if not isinstance(omissions, list):
            raise ValueError("omissions must be a list")
        omission_groups = document.get("omission_groups", [])
        if not isinstance(omission_groups, list):
            raise ValueError("omission_groups must be a list")
        for item in omission_groups:
            if not isinstance(item, dict):
                raise ValueError("omission group must be an object")
            _, _, index = _index_package(item.get("index_directory", ""), run, state)
            group = next((g for g in index["groups"] if g["id"] == item.get("group_id")), None)
            if group is None:
                raise ValueError("unknown omission group")
            ids = [fid for fid in group["frame_ids"] if fid not in covered]
            if ids:
                omissions.append({"frame_ids": ids, **{k: item.get(k) for k in
                                  ("decision", "reason", "basis_frame_ids", "reviewed")}})
        omitted = []
        for omission in omissions:
            if not isinstance(omission, dict) or omission.get("decision") not in ("redundant", "decorative", "subtitle_indexed"):
                raise ValueError("unknown visual omission decision")
            _required_text(omission.get("reason"), "omission.reason")
            if omission.get("reviewed") is not True:
                raise ValueError("omissions require explicit model review; local labels are insufficient")
            fids = _refs(omission.get("frame_ids"), known, "omission.frame_ids", required=True)
            _refs(omission.get("basis_frame_ids"), set(covered), "omission.basis_frame_ids", required=True)
            omitted.extend(fids)
        if len(omitted) != len(set(omitted)) or set(omitted) & set(covered):
            raise ValueError("frames cannot be omitted twice or both omitted and transcribed")
        if required_frames - set(covered) - set(omitted):
            missing = sorted(required_frames - set(covered) - set(omitted))
            raise ValueError("畫面轉錄缺少 frame IDs：" + ", ".join(missing))
        normalized.sort(key=lambda card: (card["start"], card["id"]))
        uncertainty_count = sum(len(card["uncertainties"]) for card in normalized)
        result = {"run_id": state["run_id"], "visuals_sha256": visual_hash,
                  "source_document_sha256": _json_hash(document), "reviewed_at": _now(),
                  "paths_relative_to": "workspace_root", "cards": normalized, "omissions": omissions,
                  "coverage": {"sampled_frame_count": len(required_frames), "covered_frame_count": len(covered),
                               "omitted_frame_count": len(omitted),
                               "supplemental_frames_used": len((set(covered) | set(omitted)) & set(supplemental)),
                               "each_frame_exactly_once": True, "all_sampled_frames_reviewed": not bool(omitted),
                               "source_sampling": visuals.get("coverage", {}), "exhaustive_video_coverage": False},
                  "ocr_source": _portable(run / "visuals.json"),
                  "uncertainty_count": uncertainty_count,
                  "reviewed_semantics": "作者聲明已查看畫面；程式驗證來源版本、卡片欄位與取樣覆蓋，無法自動證實轉錄語意。"}
        # Validate the full document before creating either deliverable.
        output, structured = _visual_exports(run, result)
        _write(run / "visual_review.json", result)
        state["visual_review"] = {"visuals_sha256": visual_hash, "finalized_at": _now(),
            "output": _portable(output), "output_sha256": _hash(output),
            "structured_output": _portable(structured), "structured_output_sha256": _hash(structured),
            "card_count": len(normalized), "frame_count": len(frames), "uncertainty_count": uncertainty_count,
            "record_sha256": _hash(run / "visual_review.json"),
            "uncertain_card_ids": [card["id"] for card in normalized if card["uncertainties"]]}
        woven = _weave_visuals(run, state, normalized)
        _status(run, state)
        return {"run_id": state["run_id"], "status": state["status"], "output": str(output),
                "structured_output": str(structured), "woven_output": str(woven),
                "card_count": len(normalized), "frame_count": len(frames),
                "uncertainty_count": uncertainty_count, "review": state.get("review", {})}


def _visual_exports(run, document):
    """Regenerate relocatable display files without altering the reviewed evidence."""
    normalized = document["cards"]
    frame_count = document["coverage"]["sampled_frame_count"]
    uncertainty_count = document["uncertainty_count"]
    destination = run / "exports/visuals"
    destination.mkdir(parents=True, exist_ok=True)
    output, structured = destination / "畫面轉錄.md", destination / "畫面轉錄.json"
    lines = ["# 畫面轉錄", "",
             f"涵蓋 {frame_count} 個抽樣畫面，整理為 {len(normalized)} 張卡片；保留 {uncertainty_count} 項畫面疑點。",
             "每個抽樣畫面恰屬一卡。時間為實際取樣點，並不表示兩點之間的所有畫面皆已捕捉。",
             "同一畫面文字狀態可合併，仍保留各取樣時間的來源連結。字幕同樣列入可見原文。",
             f"[未修訂的原始 OCR]({_link(destination, run / 'visuals.json')})", ""]
    for card in normalized:
        lines += [f"## 畫面｜{_time_range(card['start'], card['end'])}", "", card["title"], ""]
        first = card["frame_times"][0]
        lines += [f"![{_time_label(first['time'])} 畫面]({_link(destination, _root_path(first['path']))})", "", "畫面來源：", ""]
        for frame in card["frame_times"]:
            lines += [f"- [查看 {_time_label(frame['time'])} 畫面]({_link(destination, _root_path(frame['path']))})"]
        lines += ["", "### 可見原文", "", card["verbatim_text"] or "（無可辨識文字）", "",
                  "### 結構轉錄", "", card["structure"] or "（無額外結構）", ""]
        if card["uncertainties"]:
            lines += ["### 尚待確認", ""] + ["- " + item for item in card["uncertainties"]] + [""]
    output.write_text("\n".join(lines), encoding="utf-8")
    _write(structured, document)
    return output, structured


def _reading_output_path(state):
    """Match the source filename; disambiguate other runs or existing user files."""
    directory = ROOT / locations.OUTPUTS / "逐字稿"
    stem = _root_path(state["source"]).stem
    # Leave room for the collision suffix and extension on Windows.
    if len(stem.encode("utf-16-le")) // 2 > 200:
        stem = stem.encode("utf-16-le")[:400].decode("utf-16-le", errors="ignore").rstrip(" .")
    candidates = [directory / (stem + ".md"), directory / (stem + "__" + state["run_id"] + ".md")]
    previous = state.get("primary_transcript") or state.get("visual_review", {}).get("woven_output")
    previous = _root_path(previous) if previous else None
    # Keep an assigned name stable on resume, including a collision suffix.
    if previous in candidates:
        return previous
    for candidate in candidates:
        if not candidate.exists():
            return candidate
    raise FileExistsError("Transcript filenames are already occupied; existing files were preserved")


def _video_title(state):
    """Reading-edition H1: the video's name without hashtags or a _720p-style suffix."""
    from catalog.scan import display_name
    return display_name(Path(state["source"]).name)


def _weave_visuals(run, state, cards):
    """Export a standalone reading edition; audit locations stay in the run."""
    from . import reading
    transcript = _base(run)
    output = _reading_output_path(state)
    destination = output.parent
    destination.mkdir(parents=True, exist_ok=True)
    layout_path = run / "reading_layout.json"
    layout = (_read(layout_path) if layout_path.is_file()
              else reading.default_layout(transcript["segments"], cards, _video_title(state)))
    content, records = reading.render(transcript, cards, _record(run).get("decisions", {}), layout)
    if output.is_file() and output.read_text(encoding="utf-8") != content:
        from derived_common import primary as user_primary
        user_edited = _hash(output) != state.get("visual_review", {}).get("woven_output_sha256")
        saved = user_primary.snapshot(run, output, "系統重新匯出前" + ("（含使用者修改）" if user_edited else ""))
        if user_edited:
            state["primary_user_edits_replaced"] = {"snapshot": f"{user_primary.HISTORY}/{saved.name}", "at": _now()}
    output.write_text(content, encoding="utf-8")
    _write(run / "reading_map.json", {"run_id": state["run_id"],
        "transcript_sha256": _json_hash(transcript), "blocks": records,
        "format": "standalone_markdown", "normalization": "OpenCC s2tw, config/taiwan_terms.txt and punctuation/paragraphs",
        "taiwan_terms_sha256": _hash(reading.TAIWAN_TERMS),
        "layout_sha256": _hash(layout_path) if layout_path.is_file() else None})
    state["visual_review"].update({"woven_output": _portable(output), "woven_output_sha256": _hash(output),
        "woven_transcript_sha256": _json_hash(transcript), "reading_format": "standalone_markdown"})
    state["primary_transcript"] = _portable(output)
    return output


def recheck(run_dir, segment_id):
    run, state = _load_run(run_dir)
    raw = _read(run / "transcript.raw.json")
    segment = next((s for s in raw["segments"] if str(s["id"]) == segment_id), None)
    if segment is None:
        raise ValueError("Unknown segment_id")
    # IDs become paths only after exact lookup and explicit path-safety validation.
    if not segment_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in segment_id):
        raise ValueError("Unsafe segment_id")
    with _lock():
        state = _read(run / "state.json")
        config = state["config"]
        with wave.open(str(run / "audio.wav"), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
        start, end = max(0, segment["start"] - 2), min(duration, segment["end"] + 2)
        if end <= start:
            raise ValueError("Segment has no audio in source")
        prompt = ""
        parameters = {"segment": segment, "start": start, "end": end, "prompt": prompt, "beam_size": 8,
                      "model_sha256": state["identity"]["model_sha256"], "config": config}
        digest = _json_hash(parameters)
        out = run / "rechecks" / segment_id
        result_path = out / "result.json"
        if result_path.is_file() and _read(result_path).get("parameters_sha256") == digest:
            return _read(result_path)
        attempt = out / ("attempt-" + uuid.uuid4().hex[:8])
        attempt.mkdir(parents=True, exist_ok=True)
        clip = attempt / "audio.wav"
        ffmpeg = _root_path(config["tools"]["ffmpeg"])
        operation = {"status": "running", "started_at": _now(), "parameters_sha256": digest,
                     "attempt": _portable(attempt)}
        state.setdefault("rechecks", {})[segment_id] = operation
        _save_state(run, state)
        try:
            media._run([str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-ss", str(start), "-i", str(run / "audio.wav"),
                        "-t", str(end - start), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(clip)],
                       attempt, 120, attempt / "crop")
            result = media.transcribe(_root_path(config["tools"]["whisper"]), _root_path(config["model"]), clip,
                                      attempt / "asr", config.get("language", "zh"), config.get("device", "auto"),
                                      config.get("threads", 4), prompt=prompt, beam_size=8)
        except Exception as exc:
            operation.update({"status": "failed", "error": str(exc), "failed_at": _now()})
            state.update({"status": "failed", "error": "recheck: " + str(exc)})
            _write(attempt / "failure.json", operation)
            _save_state(run, state)
            raise
        for item in result["segments"]:
            item["start"] += start
            item["end"] += start
            for token in item.get("tokens", []):
                if token["start"] is not None:
                    token["start"] += start
                    token["end"] += start
        for key in ("raw_json", "stdout_log", "stderr_log"):
            if key in result:
                result[key] = _portable(result[key])
        result.update({"id": "recheck:" + segment_id, "segment_id": segment_id, "run_id": state["run_id"],
                       "kind": "same_model_recheck", "independent_evidence": False, "audio_verified": False,
                       "parameters_sha256": digest, "clip_start": start, "clip_end": end, "prompt": prompt,
                       "audio_path": _portable(clip), "paths_relative_to": "workspace_root", "beam_size": 8,
                       "review_glossary": _glossary(),
                       "note": "同一 ASR 模型以較寬 beam 局部重辨；未加入中文 hotwords，詞彙只供後續複核。候選不是獨立模型或人工聽證。"})
        _write(result_path, result)
        operation.update({"status": "done", "completed_at": _now(), "result": _portable(result_path),
                          "result_sha256": _hash(result_path)})
        state.pop("error", None)
        _bundle(run, state)
        _exports(run, state)
        _status(run, state)
        return result


def doctor():
    config = _config()
    checks = {"python": sys.version.split()[0], "executable": sys.executable,
              "project_venv": Path(sys.prefix).resolve() == (ROOT / locations.ENV).resolve(), "tools": {}}
    for key, value in config["tools"].items():
        path = _root_path(value)
        checks["tools"][key] = {"path": str(path), "exists": path.is_file()}
    model = _root_path(config["model"])
    checks["model"] = {"path": str(model), "exists": model.is_file(), "bytes": model.stat().st_size if model.is_file() else None}
    checks["packages"] = {}
    for name in ("rapidocr-onnxruntime", "onnxruntime", "Pillow", "pypinyin", "rapidfuzz"):
        try:
            checks["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            checks["packages"][name] = None
    whisper_dir = _root_path(config["tools"]["whisper"]).parent
    checks["cublas"] = {"exists": all((whisper_dir / name).is_file() for name in ("cublas64_11.dll", "cublasLt64_11.dll"))}
    # System-level components are reported, never installed; see AGENTS.md for the approval route.
    from . import system_components
    checks["system"] = system_components.check()
    checks["ready"] = (checks["project_venv"] and model.is_file() and all(t["exists"] for t in checks["tools"].values())
                       and all(checks["packages"].values()) and checks["cublas"]["exists"]
                       and checks["system"]["vc_runtime"]["ok"])
    checks["gpu_verified"] = False
    checks["gpu_note"] = "doctor 僅檢查環境；CUDA 是否實際使用由每次 ASR 載入與配置記錄驗證。"
    return checks


def _decision_basis(run, state):
    """Read the same evidence universe apply accepts, without refreshing a run."""
    transcript = _base(run)
    allowed = [frame["id"] for frame in _read(run / "visuals.json")["frames"]]
    allowed += [item["id"] for item in _read(run / "triage.json").get("glossary_evidence", [])]
    allowed += [item["id"] for item in _rechecks(run)]
    allowed += [item["id"] for item in _indexed_evidence(run)]
    allowed += [prefix + str(s["id"]) for s in transcript["segments"] for prefix in ("audio:", "text:")]
    return transcript, {"run_id": state["run_id"], "transcript_sha256": _json_hash(transcript),
                        "segments": [compact.segment_view(s) for s in transcript["segments"]],
                        "evidence_ids": list(dict.fromkeys(allowed))}


def decision_bundle(run_dir, segment_ids=None):
    if _review_task_id():
        raise ValueError("decision-bundle is disabled in model sessions; use the one focused-review and task-capped draft-excerpt")
    run, state = _load_run(run_dir)
    _, bundle = _decision_basis(run, state)
    if segment_ids:
        chosen = set(segment_ids)
        if len(chosen) != len(segment_ids) or chosen - {s["id"] for s in bundle["segments"]}:
            raise ValueError("unknown or duplicate segment ID")
        bundle["segments"] = [s for s in bundle["segments"] if s["id"] in chosen]
    bundle["scope"] = "Decision assembly snapshot; not a claim of full-context review or listened audio."
    return bundle


def assemble_patch(run_dir, decisions_path, output):
    from . import preflight
    run, state = _load_run(run_dir)
    _, bundle = _decision_basis(run, state)
    result = preflight.assemble(bundle, _read(_root_path(decisions_path)))
    if not result["ok"]:
        return result
    target = _root_path(output)
    # Never overwrite an existing source/evidence/decision file, even on a typo.
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result["patch_document"], stream, ensure_ascii=False, indent=2)
    return {"ok": True, "errors": [], "patch_document": _portable(target),
            "patch_count": len(result["patch_document"]["patches"]), "applied": False}


def preflight_patch(run_dir, patch_path, layout_path=None):
    from . import preflight
    run, state = _load_run(run_dir)
    transcript, bundle = _decision_basis(run, state)
    cards_path = run / "visual_review.json"
    cards = _read(cards_path)["cards"] if cards_path.is_file() else []
    if cards_path.is_file():
        bundle.update(_context_basis(run))
        bundle["cards"] = cards
    document = _read(_root_path(patch_path))
    result = preflight.preflight(transcript, bundle, document,
                                 _read(_root_path(layout_path)) if layout_path else None, cards)
    if cards_path.is_file() and (layout_path or (isinstance(document, dict) and document.get("context_review") is not None)):
        if _hash(cards_path) != state.get("visual_review", {}).get("record_sha256"):
            result["errors"].append({"path": "visual_review", "message": "visual record was modified outside finalize-visuals"})
        if _read(cards_path).get("visuals_sha256") != _json_hash(_read(run / "visuals.json")):
            result["errors"].append({"path": "visual_review.visuals_sha256", "message": "reviewed visual cards are stale"})
        result["ok"] = not result["errors"]
    return result


def saved_recheck_view(run_dir, segment_id, full=False):
    run, state = _load_run(run_dir)
    if segment_id not in {str(s["id"]) for s in _read(run / "transcript.raw.json")["segments"]}:
        raise ValueError("Unknown segment_id")
    if not segment_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in segment_id):
        raise ValueError("Unsafe segment_id")
    path = run / "rechecks" / segment_id / "result.json"
    result = _read(path)
    if result.get("run_id") != state["run_id"] or result.get("segment_id") != segment_id:
        raise ValueError("recheck evidence belongs to a different run or segment")
    return result if full else compact.recheck_view(result, path, _portable(path))


def _review_plan_basis(run, state):
    transcript, bundle = _decision_basis(run, state)
    path = run / "visual_review.json"
    cards = []
    bundle["visual_review_sha256"] = None
    if path.is_file():
        document = _read(path)
        if document.get("run_id") != state["run_id"]:
            raise ValueError("visual record belongs to a different run")
        if _hash(path) != state.get("visual_review", {}).get("record_sha256"):
            raise ValueError("visual record was modified outside finalize-visuals")
        if document.get("visuals_sha256") != _json_hash(_read(run / "visuals.json")):
            raise ValueError("reviewed visual cards are stale")
        cards = document["cards"]
        bundle["visual_review_sha256"] = _json_hash(document)
    return transcript, bundle, cards


def review_plan_bundle(run_dir):
    from . import reading
    _reject_model_source_dump("review-plan-bundle")
    run, state = _load_run(run_dir)
    _, bundle, cards = _review_plan_basis(run, state)
    layout_path = run / "reading_layout.json"
    terms = _read(layout_path).get("protected_terms", []) if layout_path.is_file() else []
    bundle["protected_terms"] = terms
    bundle["cards"] = [{"id": c["id"], "title": c["title"], "start": c["start"], "end": c["end"],
                        "text": reading.visual_body(c, protected_terms=terms), "frame_ids": c["frame_ids"]} for c in cards]
    bundle["prior_decisions"] = {sid: {"decision": d["decision"], "reason": d["reason"]}
                                 for sid, d in _record(run).get("decisions", {}).items()
                                 if d["decision"] == "needs_review"}
    bundle["scope"] = "Assembly snapshot only; semantic punctuation and paragraph boundaries remain reviewer decisions."
    return bundle


def assemble_review(run_dir, plan_path, output_dir):
    """Build and validate a candidate directory. Never apply to the source run."""
    from . import assembly
    run, state = _load_run(run_dir)
    transcript, bundle, cards = _review_plan_basis(run, state)
    result = assembly.compile_review(transcript, bundle, _read(_root_path(plan_path)), cards,
                                     _record(run).get("decisions", {}))
    if not result["ok"]:
        return {"ok": False, "errors": result["errors"], "applied": False}
    destination = _root_path(output_dir)
    # Source/evidence trees and existing candidates must remain intact.
    if destination.is_relative_to(run) or destination.is_relative_to((ROOT / locations.RUNS).resolve()):
        raise ValueError("candidate output directory must be outside transcription runs")
    destination.mkdir(parents=True, exist_ok=False)
    documents = {"patch.json": result["patch_document"], "reading_layout.json": result["layout"],
                 "reading_map.json": result["reading_map"], "check.json": {"ok": True, "errors": [],
                  "semantic_review_verified": False, "audio_verified": False}}
    for name, document in documents.items():
        _write(destination / name, document)
    (destination / "preview.md").write_text(result["preview"], encoding="utf-8")
    # A complete manifest is written last; no partial package is marked complete.
    _write(destination / "manifest.json", {"complete": True, "applied": False,
        "run_id": state["run_id"], "source_run": _portable(run),
        "transcript_sha256": bundle["transcript_sha256"],
        "visual_review_sha256": bundle["visual_review_sha256"],
        "plan_sha256": _hash(_root_path(plan_path)),
        "files": {name: _hash(destination / name) for name in [*documents, "preview.md"]}})
    return {"ok": True, "errors": [], "applied": False, "candidate_directory": _portable(destination),
            "preview": _portable(destination / "preview.md"),
            "patch_count": len(result["patch_document"]["patches"]),
            "speech_segments": len(transcript["segments"]), "visual_cards": len(cards)}


def list_runs():
    """Small current-state index without loading ASR tokens or OCR boxes."""
    result = []
    for path in sorted((ROOT / locations.RUNS).glob("*/state.json")):
        state = _read(path)
        output = state.get("primary_transcript")
        result.append({"run_id": state["run_id"], "workflow": "video-transcribe",
                       "run_dir": _portable(path.parent), "status": state["status"],
                       "source": _portable(_root_path(state["source"])),
                       "primary_transcript": _portable(_root_path(output)) if output else None})
    return {"runs": result}


def _index_package(path, run=None, state=None):
    """Validate immutable local package and its run binding before evidence use."""
    from . import evidence_index as ei
    directory = _root_path(path)
    manifest = _read(directory / "manifest.json")
    if manifest.get("complete") is not True:
        raise ValueError("incomplete evidence index")
    if _hash(directory / "index.json") != manifest["index_sha256"]:
        raise ValueError("evidence index changed after creation")
    index = _read(directory / "index.json")
    if run is not None:
        if (manifest["run_id"] != state["run_id"] or index["run_id"] != state["run_id"]
                or manifest["raw_sha256"] != _hash(run / "transcript.raw.json")
                or manifest["visuals_sha256"] != _hash(run / "visuals.json")):
            raise ValueError("evidence index belongs to another run or stale source")
        if manifest.get("supplemental") and manifest.get("source_sha256") != state.get("identity", {}).get("source_sha256"):
            raise ValueError("supplemental index source identity mismatch")
    for frame in index["frames"]:
        if ei.file_hash(_root_path(frame["path"])) != frame["image_sha256"]:
            raise ValueError("indexed frame changed or missing: " + frame["id"])
    return directory, manifest, index


def index_evidence(run_dir, output_dir, interval=None, start=0.0, end=None):
    from . import evidence_index as ei
    run, state = _load_run(run_dir)
    destination = _root_path(output_dir)
    if destination.is_relative_to((ROOT / locations.RUNS).resolve()) or destination.is_relative_to(run):
        raise ValueError("index output must be outside transcription runs")
    if interval is None and (start != 0 or end is not None):
        raise ValueError("start/end require --interval for supplemental sampling")
    if interval is not None and (ei.number(interval, "interval") <= 0 or ei.number(start, "start") < 0
                                 or (end is not None and ei.number(end, "end") <= start)):
        raise ValueError("invalid supplemental sampling range")
    source_hash = None
    if interval is not None:
        source = _root_path(state["source"])
        identity = state.get("identity", {})
        source_hash = _hash(source)
        if source_hash != identity.get("source_sha256") or source.stat().st_size != identity.get("source_size"):
            raise ValueError("source video changed; cannot add evidence to this run")
    destination.mkdir(parents=True, exist_ok=False)
    original = _read(run / "visuals.json")
    combined = copy.deepcopy(original)
    supplemental = False
    if interval is not None:
        config = _config()
        extra = media.extract_visuals(_root_path(config["tools"]["ffmpeg"]), _root_path(state["source"]),
                                      destination / "sampling", interval, start=start, end=end)
        if _hash(_root_path(state["source"])) != source_hash:
            raise ValueError("source video changed during sampling")
        prefix = "extra:" + _json_hash(extra)[:16] + ":"
        for frame in extra["frames"]:
            for key in ("id", "duplicate_of", "perceptually_similar_to", "text_source_frame"):
                if frame.get(key):
                    frame[key] = prefix + frame[key]
        combined["frames"].extend(extra["frames"])
        combined["frames"].sort(key=lambda f: (f["time"], f["id"]))
        combined["coverage"] = {"original": original.get("coverage", {}),
                                "supplemental": extra["coverage"], "exhaustive": False}
        supplemental = True
    index = ei.build(combined, state["run_id"], _root_path)
    _write(destination / "index.json", index)
    overview = ei.menu(index)
    overview["contact_sheets"] = ei.contact_sheets(index, destination)
    overview["index_directory"] = _portable(destination)
    _write(destination / "menu.json", overview)
    manifest = {"complete": True, "run_id": state["run_id"], "source_run": _portable(run),
                "raw_sha256": _hash(run / "transcript.raw.json"),
                "visuals_sha256": _hash(run / "visuals.json"),
                "index_sha256": _hash(destination / "index.json"), "supplemental": supplemental,
                "source_sha256": source_hash,
                "original_frame_ids": [f["id"] for f in original["frames"]],
                "semantic_reviewed": False, "applied": False}
    _write(destination / "manifest.json", manifest)
    return {"ok": True, "index_directory": _portable(destination), "menu": _portable(destination / "menu.json"),
            "frame_count": len(index["frames"]), "group_count": len(index["groups"]),
            "contact_sheets": overview["contact_sheets"], "applied": False}


def register_evidence(run_dir, index_dir):
    run, state = _load_run(run_dir)
    directory, manifest, _ = _index_package(index_dir, run, state)
    record = {"directory": _portable(directory), "manifest_sha256": _hash(directory / "manifest.json")}
    target = run / "evidence_indexes" / (manifest["index_sha256"] + ".json")
    if target.exists():
        if _read(target) != record:
            raise ValueError("evidence registration changed")
        return {"ok": True, "status": "already_registered"}
    _write(target, record)
    return {"ok": True, "status": "registered", "semantic_reviewed": False, "applied": False}


def _indexed_evidence(run):
    """Only registered, hash-verified supplemental frames become patch evidence."""
    state = _read(run / "state.json")
    result = {}
    for record_path in sorted((run / "evidence_indexes").glob("*.json")):
        record = _read(record_path)
        directory = _root_path(record["directory"])
        if _hash(directory / "manifest.json") != record["manifest_sha256"]:
            raise ValueError("registered evidence manifest changed")
        _, manifest, index = _index_package(directory, run, state)
        original = set(manifest["original_frame_ids"])
        for frame in index["frames"]:
            if frame["id"] not in original:
                item = {k: frame[k] for k in ("id", "time", "path", "text", "image_sha256", "ocr_evidence_time")}
                item.update(kind="supplemental_video_frame", audio_verified=False)
                if item["id"] in result and result[item["id"]] != item:
                    raise ValueError("conflicting supplemental frame ID")
                result[item["id"]] = item
    return list(result.values())


def evidence_query(index_dir, start=None, end=None, group_id=None, frame_ids=None, full=False, offset=0, limit=8):
    from . import evidence_index as ei
    _, _, index = _index_package(index_dir)
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("offset must be nonnegative and limit between 1 and 100")
    selected = ei.select(index, start=start, end=end, group_id=group_id, frame_ids=frame_ids, full=full, limit=None)
    page = selected[offset:offset+limit]
    return {"run_id": index["run_id"], "frames": page, "semantic_reviewed": False,
            "total_available": len(selected), "next_offset": offset+len(page) if offset+len(page) < len(selected) else None,
            "truncated": offset+len(page) < len(selected),
            "note": "Only this page is returned. Query next_offset or a specific frame; not all states were inspected."}


def visual_batches(index_dir, sheets_per_batch=3, batch_id=None):
    from . import evidence_index as ei
    directory, _, index = _index_package(index_dir)
    return ei.review_batches(index, directory, sheets_per_batch, batch_id)


def _vocabulary_entries():
    path = ROOT / "工作資料/索引/校字詞表.json"
    entries = _read(path)["entries"] if path.is_file() else []
    valid = []
    for item in entries:
        source = ROOT / locations.RUNS / item["run_id"]
        record = _record(source)
        current = record.get("decisions", {}).get(item["segment_id"], {})
        if _json_hash(current) == item["decision_sha256"]:
            valid.append(item)
    return valid


def remember_term(run_dir, segment_id, term, variant, scope):
    from . import vocabulary
    run, state = _load_run(run_dir)
    decision = _record(run).get("decisions", {}).get(segment_id, {})
    item = vocabulary.entry(scope, term, variant, state["run_id"], segment_id, decision, _json_hash(_base(run)))
    target = ROOT / "工作資料/索引/校字詞表.json"
    with _lock():
        data = _read(target) if target.is_file() else {"version": 1, "entries": []}
        if item["id"] not in {e["id"] for e in data["entries"]}:
            data["entries"].append(item)
            _write(target, data)
    return {"ok": True, "term": item, "auto_apply": False}


def focused_review(run_dir, index_dir, segment_ids=None, scope=None, task_id=None, domain=None):
    from . import evidence_index as ei, vocabulary
    if domain:
        from domains import load
        load(domain)  # Validate once; seal-draft will use the same named module.
    task_id = _review_task_id(task_id)
    if not task_id:
        raise ValueError("a stable review task ID is required; use --task-id when the host has no session ID")
    with _lock():
        run, state = _load_run(run_dir)
        receipt_path = run / "focused_review_tasks.json"
        receipts = (_read(receipt_path) if receipt_path.is_file()
                    else {"version": 1, "run_id": state["run_id"], "tasks": {}})
        if (receipts.get("version") != 1 or receipts.get("run_id") != state["run_id"]
                or not isinstance(receipts.get("tasks"), dict)):
            raise ValueError("invalid focused-review task ledger")
        if task_id in receipts["tasks"]:
            raise ValueError("focused-review already ran in this session; continue with the saved decisions and use only targeted evidence-query or draft-excerpt when needed")
        directory, _, index = _index_package(index_dir, run, state)
        transcript = _base(run)
        triage = copy.deepcopy(_read(run / "triage.json"))
        decisions = _record(run).get("decisions", {})
        entries = _vocabulary_entries() if scope else []
        flagged = {s["segment_id"] for s in triage["suspects"]}
        for segment in transcript["segments"]:
            if (segment["id"] not in flagged and any(e["scope"] == scope and e["variant"] in segment["text"] for e in entries)):
                triage["suspects"].append({"segment_id": segment["id"], "reasons": ["known_term_variant"]})
        # Explicit IDs permit audits of resolved or initially unflagged segments.
        if segment_ids is None:
            triage["suspects"] = [s for s in triage["suspects"]
                                  if decisions.get(s["segment_id"], {}).get("decision") not in ("keep", "correct")]
        present = {s["segment_id"] for s in triage["suspects"]}
        for sid, decision in decisions.items():
            if (decision["decision"] == "needs_review" and sid not in present
                    and (segment_ids is None or sid in segment_ids)):
                triage["suspects"].append({"segment_id": sid, "reasons": ["manual_needs_review"]})
        for suspect in triage["suspects"]:
            decision = decisions.get(suspect["segment_id"])
            if decision and decision["decision"] == "needs_review":
                suspect["current_decision"] = {k: decision[k] for k in ("decision", "reason", "evidence_ids")}
        rechecks = {r["segment_id"]: compact.recheck_view(r, run / "rechecks" / r["segment_id"] / "result.json")
                    for r in _rechecks(run)}
        result = ei.focused_bundle(transcript, triage, index, segment_ids, rechecks,
                                   lambda text: vocabulary.hints(entries, scope, text))
        allowed = set(_decision_basis(run, state)[1]["evidence_ids"])
        result["unregistered_evidence_ids"] = [f["id"] for f in result["evidence"] if f["id"] not in allowed]
        result["index_directory"] = _portable(directory)
        result["registration_instruction"] = "Register this index before citing supplemental IDs in patches."
        receipts["tasks"][task_id] = {
            "recorded_at": _now(), "index_directory": _portable(directory),
            "segment_ids": list(segment_ids) if segment_ids else None,
            "scope": scope, "domain": domain,
            "transcript_sha256": _json_hash(transcript),
            "triage_sha256": _hash(run / "triage.json"),
            "visuals_sha256": _json_hash(_read(run / "visuals.json")),
            "reviewed_segment_ids": [item["segment_id"] for item in result["items"]],
            "result_sha256": _json_hash(result)}
        _write(receipt_path, receipts)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="本機影片逐字稿、畫面證據與局部複核工作流")
    commands = parser.add_subparsers(dest="command", required=True)
    draft_start = commands.add_parser("start-draft", help="累積全文閱讀前的局部決議；不套用、不讀原始全文")
    draft_start.add_argument("run_dir")
    draft_start.add_argument("--output-dir", required=True)
    draft_start.add_argument("--changes")
    draft_start.add_argument("--layout")
    draft_start.add_argument("--accept-visual-uncertainties", action="store_true")
    draft_start.add_argument("--task-id")
    draft_edit = commands.add_parser("edit-draft", help="固定局部修正入口；不重新規劃或建立正式patch")
    draft_edit.add_argument("draft_dir")
    draft_edit.add_argument("--changes")
    draft_edit.add_argument("--find")
    draft_edit.add_argument("--replace", dest="replacement")
    draft_edit.add_argument("--segment-id")
    draft_edit.add_argument("--after")
    draft_edit.add_argument("--ending")
    draft_edit.add_argument("--task-id")
    draft_seal = commands.add_parser("seal-draft", help="完成所有前置檢查並封存唯一的最終候選稿")
    draft_seal.add_argument("draft_dir")
    draft_seal.add_argument("--accept-visual-uncertainties", action="store_true")
    draft_seal.add_argument("--task-id")
    draft_read = commands.add_parser("read-draft", help="只回傳一次已封存候選全文，供標記全部剩餘問題")
    draft_read.add_argument("draft_dir")
    draft_read.add_argument("--task-id")
    draft_excerpt = commands.add_parser("draft-excerpt", help="只取指定局部，不輸出ASR全文")
    draft_excerpt.add_argument("draft_dir")
    draft_excerpt.add_argument("--segment-id", action="append", required=True)
    draft_excerpt.add_argument("--task-id")
    draft_finish = commands.add_parser("finish-review", help="一次套用全文問題批次、預檢及輸出；不需resume")
    draft_finish.add_argument("draft_dir")
    draft_finish.add_argument("--reviewed", action="store_true")
    draft_finish.add_argument("--revision", type=int)
    draft_finish.add_argument("--changes", help="唯一一批全文閱讀問題與修正；須含 review_issues")
    draft_finish.add_argument("--task-id")
    commands.add_parser("doctor")
    commands.add_parser("list-runs", help="列出現有轉錄任務與主交付位置")
    idx = commands.add_parser("index-evidence", help="本機畫面分組與代表幀索引；不修改run")
    idx.add_argument("run_dir")
    idx.add_argument("--output-dir", required=True)
    idx.add_argument("--interval", type=float)
    idx.add_argument("--start", type=float, default=0.0)
    idx.add_argument("--end", type=float)
    register = commands.add_parser("register-evidence", help="登記已驗證補充畫面，供patch與畫面卡引用")
    register.add_argument("run_dir")
    register.add_argument("index_dir")
    query = commands.add_parser("evidence-query", help="按時間／群組／frame ID取本機證據")
    query.add_argument("index_dir")
    query.add_argument("--start", type=float)
    query.add_argument("--end", type=float)
    query.add_argument("--group-id")
    query.add_argument("--frame-id", action="append")
    query.add_argument("--full", action="store_true")
    query.add_argument("--offset", type=int, default=0)
    query.add_argument("--limit", type=int, default=8)
    visual_plan = commands.add_parser("visual-batches", help="唯讀規劃隔離讀圖批次；不回傳圖片或完整OCR")
    visual_plan.add_argument("index_dir")
    visual_plan.add_argument("--sheets-per-batch", type=int, default=3)
    visual_plan.add_argument("--batch-id")
    focus = commands.add_parser("focused-review", help="一次取得局部文字、畫面文字、既有重辨與詞表提示")
    focus.add_argument("run_dir")
    focus.add_argument("index_dir")
    focus.add_argument("--segment-id", action="append")
    focus.add_argument("--scope")
    focus.add_argument("--domain", choices=("bazi",))
    focus.add_argument("--task-id")
    term = commands.add_parser("remember-term", help="從已記錄更正建立限定領域、有來源的詞條")
    term.add_argument("run_dir")
    term.add_argument("--segment-id", required=True)
    term.add_argument("--term", required=True)
    term.add_argument("--variant", required=True)
    term.add_argument("--scope", required=True)
    commands.add_parser("resolve-path", help="解析歷史證據中的舊路徑").add_argument("path")
    targets = commands.add_parser("inspect-targets", help="唯讀檢查單檔或批次清單的檔名與片長，交由 Agent 確認目標")
    targets.add_argument("sources", nargs="+")
    proc = commands.add_parser("process")
    proc.add_argument("source")
    proc.add_argument("--language")
    proc.add_argument("--device", choices=("auto", "cuda", "cpu"))
    proc.add_argument("--interval", type=float)
    for name in ("status", "review-bundle", "context-bundle", "chapters", "resume", "snapshot-primary"):
        command = commands.add_parser(name, **({"help": "改主稿前先存一份目前版本到 run 的 primary_history"}
                                              if name == "snapshot-primary" else {}))
        command.add_argument("run_dir")
        if name == "status":
            command.add_argument("--full", action="store_true", help="回傳完整狀態；預設精簡摘要")
    decisions = commands.add_parser("decision-bundle", help="唯讀目前正文版本與合法證據ID，供最小決議組裝")
    decisions.add_argument("run_dir")
    decisions.add_argument("--segment-id", action="append")
    plan_bundle = commands.add_parser("review-plan-bundle", help="唯讀正文／圖卡版本，供修正及範圍排版")
    plan_bundle.add_argument("run_dir")
    build_review = commands.add_parser("assemble-review", help="組装修正、排版與驗收預覽到新候選目錄；不改原run")
    build_review.add_argument("run_dir")
    build_review.add_argument("plan_document")
    build_review.add_argument("--output-dir", required=True)
    assembler = commands.add_parser("assemble-patch", help="由最小決議組裝更正檔；不套用，不覆寫檔案")
    assembler.add_argument("run_dir")
    assembler.add_argument("decisions_document")
    assembler.add_argument("--output", required=True)
    check = commands.add_parser("preflight", help="唯讀累積檢查更正與排版；不代表內容已核定")
    check.add_argument("run_dir")
    check.add_argument("patch_document")
    check.add_argument("--layout")
    ap = commands.add_parser("apply")
    ap.add_argument("run_dir")
    ap.add_argument("patch_document")
    fin = commands.add_parser("finalize")
    fin.add_argument("run_dir")
    fin.add_argument("notes_document")
    vis = commands.add_parser("finalize-visuals", help="驗證畫面完整覆蓋並輸出画面轉錄")
    vis.add_argument("run_dir")
    vis.add_argument("visuals_document")
    again = commands.add_parser("recheck")
    again.add_argument("run_dir")
    again.add_argument("--segment-id", required=True)
    again.add_argument("--full", action="store_true", help="回傳完整重辨證據；預設精簡候選")
    saved = commands.add_parser("recheck-view", help="唯讀既有重辨結果，不重跑ASR")
    saved.add_argument("run_dir")
    saved.add_argument("--segment-id", required=True)
    saved.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("start-draft", "edit-draft", "seal-draft", "read-draft", "draft-excerpt", "finish-review"):
            from . import final_review
            if args.command == "start-draft":
                result = final_review.start(args.run_dir, args.output_dir, args.changes, args.layout,
                    accept_visual_uncertainties=args.accept_visual_uncertainties, task_id=args.task_id)
            elif args.command == "edit-draft":
                selectors = sum((args.changes is not None, args.find is not None or args.replacement is not None,
                                 args.after is not None or args.ending is not None))
                if selectors != 1 or (args.segment_id is not None and args.find is None):
                    raise ValueError("choose changes file, find/replace, or after/ending")
                result = final_review.edit(args.draft_dir, args.changes, find=args.find,
                    replacement=args.replacement, segment_id=args.segment_id, after=args.after,
                    ending=args.ending, task_id=args.task_id)
            elif args.command == "seal-draft":
                result = final_review.seal(args.draft_dir, args.task_id,
                                           args.accept_visual_uncertainties)
            elif args.command == "read-draft":
                result = final_review.read_once(args.draft_dir, args.task_id)
            elif args.command == "draft-excerpt":
                result = final_review.excerpt(args.draft_dir, args.segment_id, args.task_id)
            else:
                result = final_review.finish(args.draft_dir, args.reviewed, args.revision,
                                             args.task_id, args.changes)
        elif args.command == "doctor":
            result = doctor()
        elif args.command == "list-runs":
            result = list_runs()
        elif args.command == "index-evidence":
            result = index_evidence(args.run_dir, args.output_dir, args.interval, args.start, args.end)
        elif args.command == "register-evidence":
            result = register_evidence(args.run_dir, args.index_dir)
        elif args.command == "evidence-query":
            if sum((args.group_id is not None, bool(args.frame_id), args.start is not None or args.end is not None)) != 1:
                raise ValueError("choose exactly one query selector")
            result = evidence_query(args.index_dir, args.start, args.end, args.group_id, args.frame_id, args.full, args.offset, args.limit)
        elif args.command == "visual-batches":
            result = visual_batches(args.index_dir, args.sheets_per_batch, args.batch_id)
        elif args.command == "focused-review":
            result = focused_review(args.run_dir, args.index_dir, args.segment_id,
                                    args.scope, args.task_id, args.domain)
        elif args.command == "remember-term":
            result = remember_term(args.run_dir, args.segment_id, args.term, args.variant, args.scope)
        elif args.command == "resolve-path":
            path = _root_path(args.path)
            result = {"path": str(path), "exists": path.exists()}
        elif args.command == "inspect-targets":
            result = inspect_targets(args.sources)
        elif args.command == "decision-bundle":
            result = decision_bundle(args.run_dir, args.segment_id)
        elif args.command == "review-plan-bundle":
            result = review_plan_bundle(args.run_dir)
        elif args.command == "assemble-review":
            result = assemble_review(args.run_dir, args.plan_document, args.output_dir)
        elif args.command == "assemble-patch":
            result = assemble_patch(args.run_dir, args.decisions_document, args.output)
        elif args.command == "preflight":
            result = preflight_patch(args.run_dir, args.patch_document, args.layout)
        elif args.command == "recheck-view":
            if args.full:
                _reject_model_source_dump("recheck-view --full")
            result = saved_recheck_view(args.run_dir, args.segment_id, args.full)
        elif args.command == "process":
            result = process(args.source, args.language, args.device, args.interval)
        elif args.command == "resume":
            result = resume(args.run_dir)
        elif args.command == "apply":
            result = apply(args.run_dir, args.patch_document)
        elif args.command == "finalize":
            result = finalize(args.run_dir, args.notes_document)
        elif args.command == "finalize-visuals":
            result = finalize_visuals(args.run_dir, args.visuals_document)
        elif args.command == "recheck":
            if args.full:
                _reject_model_source_dump("recheck --full")
            result = recheck(args.run_dir, args.segment_id)
            if not args.full:
                result = saved_recheck_view(args.run_dir, args.segment_id)
        else:
            run, state = _load_run(args.run_dir)
            if args.command == "status":
                with _lock():
                    state = _read(run / "state.json")
                    result = _status(run, state) if state.get("status") != "failed" else state
                    if not args.full:
                        result = compact.status_view(result)
            elif args.command == "snapshot-primary":
                from derived_common import primary as user_primary
                output = state.get("primary_transcript")
                if not output or not _root_path(output).is_file():
                    raise ValueError("找不到主稿，無法存快照")
                saved = user_primary.snapshot(run, _root_path(output), "修改主稿前手動存檔")
                result = {"run_id": state["run_id"], "primary_transcript": output, "snapshot": _portable(saved)}
            else:
                with _lock():
                    if args.command == "review-bundle":
                        _reject_model_source_dump("review-bundle")
                        bundle = _bundle(run, state)
                        result = {"run_id": state["run_id"], "review_packet": _portable(run / "review_packet.md"),
                                  "review_bundle": _portable(run / "review_bundle.json"), "stats": bundle["stats"]}
                    elif args.command == "context-bundle":
                        result = _context_bundle(run, state)
                    else:
                        result = _chapters(run, state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("ok") is False or (args.command == "doctor" and not result["ready"]) else 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
