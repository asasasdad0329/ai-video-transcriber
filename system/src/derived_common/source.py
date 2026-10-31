"""Read a finished transcription run as ordered, addressable blocks.

The reading edition (primary transcript) is the content both derived workflows
must stay faithful to; reading_map.json supplies the IDs behind each paragraph.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from derived_common import primary as user_primary
from workspace_paths import ROOT, resolve

FINISHED = {"completed", "completed_with_unresolved"}
_VISUAL_HEAD = re.compile(r"^> \*\*畫面補充：(.*)\*\*$")


def clock(seconds):
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def rel(path):
    path = Path(path)
    return path.relative_to(ROOT).as_posix() if path.is_absolute() and path.is_relative_to(ROOT) else path.as_posix()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def find_run(value):
    """Accept a run ID, run folder, source video path or title; untranscribed videos are refused."""
    from catalog import find_run as lookup
    return lookup(value)  # the index refuses videos that have no finished transcript


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_reading_edition(text):
    """Split the primary Markdown into title, sections and speech/visual chunks."""
    title, sections = None, []
    chunk = []

    def flush():
        if not chunk:
            return
        if not sections:
            chunk.clear()  # the fixed note under the title belongs to no section
            return
        head = _VISUAL_HEAD.match(chunk[0])
        if head:
            body = [line[2:] if line.startswith("> ") else "" for line in chunk[1:]]
            while body and not body[0]:
                body.pop(0)
            sections[-1]["chunks"].append({"kind": "visual", "title": head.group(1), "text": "\n".join(body)})
        else:
            sections[-1]["chunks"].append({"kind": "speech", "text": "\n".join(chunk)})
        chunk.clear()

    for line in text.splitlines():
        if title is None and line.startswith("# "):
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            sections.append({"title": line[3:].strip(), "chunks": []})
        elif not line.strip():
            flush()
        else:
            chunk.append(line)
    flush()
    if not title or not sections:
        raise ValueError("主稿缺少標題或小節")
    return title, sections


def _video_title(title, primary, run_id):
    """Older reading editions carry a generic H1; their file is named after the video."""
    from video_workflow.reading import DEFAULT_TITLE, traditional
    if title != DEFAULT_TITLE:
        return title  # the video's name, or a title the user wrote into the primary
    from catalog.scan import display_name
    stem = primary.stem.removesuffix("__" + run_id)  # a second run of the same video
    return traditional(display_name(stem + primary.suffix))


def load_run(value, *, allow_edited_primary=True):
    """User edits to the primary are always accepted while its paragraph structure holds.

    allow_edited_primary is kept for older callers and task files; it no longer gates anything.
    """
    run = find_run(value)
    state = _json(run / "state.json")
    if state.get("status") not in FINISHED:
        raise ValueError(f"轉錄尚未完成（status={state.get('status')}），先完成轉錄流程")
    primary = resolve(state["primary_transcript"])
    if not primary.is_file():
        raise ValueError(f"找不到主稿：{state['primary_transcript']}")
    text = primary.read_text(encoding="utf-8")
    title, parsed = parse_reading_edition(text)
    mapping = _json(run / "reading_map.json")["blocks"]
    problem, primary_edits = user_primary.compare(text, mapping)
    if problem:
        raise ValueError(f"主稿段落結構被改動：{problem}。恢復段落結構，或從 {user_primary.HISTORY} 取回舊版")
    if primary_edits:
        user_primary.snapshot(run, primary, "讀取時偵測到使用者修改")
    while parsed and not parsed[-1]["chunks"]:
        parsed.pop()  # a heading left behind after cutting the ending holds nothing
    segments = {s["id"]: s for s in _json(run / "transcript.corrected.json")["segments"]}
    cards = {c["id"]: c for c in _json(run / "visual_review.json")["cards"]}

    blocks, order, sections = {}, [], []
    cursor, speech_no = 0, 0
    for index, section in enumerate(parsed, 1):
        sid = f"S{index:02d}"
        ids = []
        for chunk in section["chunks"]:
            record = mapping[cursor]
            cursor += 1
            if chunk["kind"] == "speech":
                speech_no += 1
                bid = f"b{speech_no:03d}"
                times = [segments[s] for s in record["segment_ids"]]
                block = {"kind": "speech", "text": chunk["text"], "segment_ids": record["segment_ids"],
                         "start": min(t["start"] for t in times), "end": max(t["end"] for t in times)}
            else:
                bid = record["card_id"]
                card = cards[bid]
                frames = [{"frame_id": f.get("frame_id"), "time": f.get("time"), "path": rel(resolve(f["path"]))}
                          for f in card.get("frame_times", []) if f.get("path")]
                block = {"kind": "visual", "text": chunk["text"], "title": chunk["title"], "card_kind": card.get("kind"),
                         "start": card.get("start", 0.0), "end": card.get("end", card.get("start", 0.0)), "frames": frames}
            block.update(id=bid, section=sid)
            blocks[bid] = block
            ids.append(bid)
            order.append(bid)
        speech = [blocks[b] for b in ids if blocks[b]["kind"] == "speech"] or [blocks[b] for b in ids]
        start = min(b["start"] for b in speech) if speech else 0.0
        end = max(b["end"] for b in speech) if speech else start
        sections.append({"id": sid, "title": section["title"], "start": start, "end": end, "blocks": ids})

    probe = _json(run / "probe.json") if (run / "probe.json").is_file() else {}
    duration = float(probe.get("duration") or probe.get("format", {}).get("duration") or 0)
    return {
        "run_id": state["run_id"],
        "run_dir": rel(run),
        "status": state["status"],
        "source_video": Path(state["source"]).name,
        "duration": duration,
        "title": _video_title(title, primary, state["run_id"]),
        "primary": rel(primary),
        "stem": primary.stem,
        "primary_sha256": sha256_file(primary),
        "allow_edited_primary": allow_edited_primary,
        "primary_edits": primary_edits,
        "reading_map_sha256": sha256_file(run / "reading_map.json"),
        "sections": sections,
        "blocks": blocks,
        "order": order,
    }


def section_text(doc, section, kinds=("speech", "visual")):
    return "\n".join(doc["blocks"][b]["text"] for b in section["blocks"] if doc["blocks"][b]["kind"] in kinds)


def document_text(doc, kinds=("speech", "visual")):
    return "\n".join(doc["blocks"][b]["text"] for b in doc["order"] if doc["blocks"][b]["kind"] in kinds)


def packet(doc, purpose, extra_lines=()):
    """Compact one-pass reading packet; IDs let the agent point at text instead of copying it."""
    label = {"datacard": "資料卡", "handout": "講義"}[purpose]
    lines = [f"# {label}輸入包｜{doc['title']}", "",
             f"run：{doc['run_id']}｜影片長度 {clock(doc['duration'])}｜主稿 sha256：{doc['primary_sha256']}", ""]
    if doc.get("parts"):
        lines += [f"共 {len(doc['parts'])} 部，依下列順序合為一份；時間碼各部自行起算。", ""]
        for part in doc["parts"]:
            lines.append(f"- 第 {part['part']} 部｜{part['title']}｜{clock(part['duration'])}｜"
                         f"{part['sections'][0]}–{part['sections'][-1]}｜run {part['run_id']}")
        lines.append("")
    lines += list(extra_lines)
    shown = None
    for section in doc["sections"]:
        if section.get("part") and section["part"] != shown:
            shown = section["part"]
            part = doc["parts"][shown - 1]
            lines += [f"# 第 {shown} 部｜{part['title']}", ""]
        lines += [f"## {section['id']}｜{section['title']}｜{clock(section['start'])}–{clock(section['end'])}", ""]
        for bid in section["blocks"]:
            block = doc["blocks"][bid]
            if block["kind"] == "speech":
                lines += [f"[{bid} {clock(block['start'])}] {block['text']}", ""]
            else:
                head = f"[{bid}｜畫面｜{block['card_kind']}｜{clock(block['start'])}｜{block['title']}"
                if purpose == "handout" and block["frames"]:
                    head += "｜幀 " + ",".join(f["frame_id"] for f in block["frames"] if f.get("frame_id"))
                lines += [head + "]", block["text"], f"[/{bid}]", ""]
    return "\n".join(lines).rstrip() + "\n"


_TRIM = "（(【[｜|：:、，,。．・-–—~〜 　"
_UNSAFE = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def series_name(titles):
    """Series title from what the part titles share, e.g. ...（一）｜調候用神 -> ...。"""
    shared = os.path.commonprefix([t.strip() for t in titles]).strip()
    return shared.rstrip(_TRIM).strip()


def series_id(run_ids):
    """Stable folder name for the same set of parts in the same order."""
    digest = hashlib.sha256("|".join(run_ids).encode("utf-8")).hexdigest()
    return "series-" + digest[:12]


def safe_stem(name):
    stem = _UNSAFE.sub("", name).strip().rstrip(".")
    if not stem:
        raise ValueError("系列名稱無法當作檔名，請用 --name 指定")
    return stem


def load_series(values, name=None, *, allow_edited_primary=False):
    """Load several finished runs as one document: parts keep their own order and clocks."""
    values = list(values)
    if len(values) < 2:
        raise ValueError("系列資料卡至少需要兩支影片")
    docs = [load_run(value, allow_edited_primary=True) if allow_edited_primary else load_run(value)
            for value in values]
    run_ids = [d["run_id"] for d in docs]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("同一支影片重複指定：" + "、".join(sorted({r for r in run_ids if run_ids.count(r) > 1})))

    blocks, order, sections, parts = {}, [], [], []
    number = 0
    for index, doc in enumerate(docs, 1):
        tag = f"p{index}-"
        part_sections = []
        for section in doc["sections"]:
            number += 1
            sid = f"S{number:02d}"
            ids = []
            for bid in section["blocks"]:
                block = dict(doc["blocks"][bid], id=tag + bid, section=sid, part=index)
                blocks[block["id"]] = block
                ids.append(block["id"])
                order.append(block["id"])
            sections.append({"id": sid, "title": section["title"], "start": section["start"],
                             "end": section["end"], "blocks": ids, "part": index})
            part_sections.append(sid)
        parts.append({"part": index, "run_id": doc["run_id"], "run_dir": doc["run_dir"], "title": doc["title"],
                      "source_video": doc["source_video"], "duration": doc["duration"], "primary": doc["primary"],
                      "primary_sha256": doc["primary_sha256"], "sections": part_sections,
                      "primary_edits": doc.get("primary_edits", []),
                      "reading_map_sha256": doc.get("reading_map_sha256")})

    title = (name or "").strip() or series_name([d["title"] for d in docs])
    if not title:
        raise ValueError("各部標題沒有共同開頭，請用 --name 指定系列名稱")
    return {
        "run_id": series_id(run_ids),
        "run_dir": [d["run_dir"] for d in docs],
        "status": "completed_with_unresolved" if any(d["status"] != "completed" for d in docs) else "completed",
        "source_video": [d["source_video"] for d in docs],
        "duration": sum(d["duration"] for d in docs),
        "title": title,
        "primary": [d["primary"] for d in docs],
        "stem": safe_stem(title),
        "primary_sha256": hashlib.sha256("|".join(d["primary_sha256"] for d in docs).encode("utf-8")).hexdigest(),
        "sections": sections,
        "blocks": blocks,
        "order": order,
        "parts": parts,
        "allow_edited_primary": allow_edited_primary,
    }
