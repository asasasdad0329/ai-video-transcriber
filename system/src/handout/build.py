"""Prepare the handout packet, check the agent's source, render DOCX."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

import domains
from workspace_paths import ROOT
from derived_common import source as src
from derived_common import tasks
from derived_common.text import UNRESOLVED, meta_hits, protected_tokens, squash
from handout import parse as hp
from handout.render_docx import load_theme, render

MIN_RATIO, MAX_RATIO = 0.55, 1.15
CARD_USED = 0.6


def _speech_order(doc):
    return [b for b in doc["order"] if doc["blocks"][b]["kind"] == "speech"]


def _span(ids):
    return f"{ids[0]}-{ids[-1]}" if len(ids) > 1 else ids[0]


def template(doc):
    lines = ["---", f"title: {doc['title'].split('｜')[0]}"]
    if "｜" in doc["title"]:
        lines.append(f"subtitle: {doc['title'].split('｜', 1)[1]}")
    lines += ["---", ""]
    for section in doc["sections"]:
        speech = [b for b in section["blocks"] if doc["blocks"][b]["kind"] == "speech"]
        if speech:
            lines += [f"## {section['title']} {{src={_span(speech)}}}", "", ""]
    return "\n".join(lines)


def prepare(run):
    doc = src.load_run(run)
    folder = tasks.task_dir("handout", doc["run_id"])
    (folder / "packet.md").write_text(src.packet(doc, "handout"), encoding="utf-8")
    (folder / "handout.template.md").write_text(template(doc), encoding="utf-8")
    tasks.write_task("handout", folder, doc, status="awaiting_source",
                     message="讀 packet.md 後依 SKILL 寫 handout.md，再執行 handout build")
    return {"task": src.rel(folder), "packet": src.rel(folder / "packet.md"),
            "template": src.rel(folder / "handout.template.md"), "source": src.rel(folder / "handout.md")}


def check(doc, meta, chapters, domain):
    errors, warnings = [], []
    whole = src.document_text(doc)
    flat = [b for c in chapters for b in c["src"]]
    if flat != _speech_order(doc):
        errors.append("章節 src 必須依原順序恰好涵蓋全部口述段落（可合併相鄰段落，不可遺漏、重複或重排）")
        return errors, warnings, {}
    for field in ("title", "subtitle"):
        if meta.get(field) and meta_hits(str(meta[field]), whole):
            errors.append(f"{field} 含整理說明用語")

    all_output = "\n".join(c["title"] + "\n" + "\n".join(hp.element_text(e) for e in c["elements"]) for c in chapters)
    total_src = total_out = 0
    for number, chapter in enumerate(chapters, 1):
        label = f"第 {number} 章「{chapter['title']}」"
        speech = "\n".join(doc["blocks"][b]["text"] for b in chapter["src"])
        output = chapter["title"] + "\n" + "\n".join(hp.element_text(e) for e in chapter["elements"])
        prose = hp.prose_text(chapter["elements"])
        missing = sorted(t for t in protected_tokens(speech, domain.protected_tokens(speech)) if t not in output)
        if missing:
            errors.append(f"{label} 少了原文的數字、專名或干支：{'、'.join(missing)}")
        if speech.count(UNRESOLVED) > output.count(UNRESOLVED):
            errors.append(f"{label} 的〔？〕少於原文（{speech.count(UNRESOLVED)} 處），疑字需保留標記")
        hits = meta_hits(output, whole)
        if hits:
            errors.append(f"{label} 含整理說明用語：{'、'.join(hits)}")
        size_src, size_out = len(squash(speech)), len(squash(prose))
        total_src, total_out = total_src + size_src, total_out + size_out
        ratio = size_out / size_src if size_src else 1
        if ratio < MIN_RATIO:
            warnings.append(f"{label} 段落文字只剩原文的 {ratio:.0%}（不含表格與圖），確認沒有漏講內容；已整理進表格可忽略")
        elif ratio > MAX_RATIO:
            warnings.append(f"{label} 內文是原文的 {ratio:.0%}，確認沒有加入講者沒說的內容")
        if not chapter["elements"]:
            errors.append(f"{label} 沒有內容")

    referenced = {e["card"] for c in chapters for e in c["elements"] if e["type"] == "image"}
    for card in referenced:
        if card not in doc["blocks"] or doc["blocks"][card]["kind"] != "visual":
            errors.append(f"圖片引用不存在的畫面卡：{card}")
    present = set(squash(all_output))
    for bid in doc["order"]:
        block = doc["blocks"][bid]
        if block["kind"] != "visual" or bid in referenced:
            continue
        # Rewritten tables and lists keep the characters, not the original lines.
        chars = {c for c in squash(block["text"]) if c.isalnum()}
        if chars and len(chars & present) / len(chars) < CARD_USED:
            warnings.append(f"畫面卡 {bid}「{block['title']}」沒有放進講義（封面、片尾等可忽略）")
    return errors, warnings, {"source_characters": total_src, "prose_characters": total_out,
                              "ratio": round(total_out / total_src, 3) if total_src else None}


def _frame_asset(doc, element, folder):
    block = doc["blocks"].get(element["card"])
    frames = block.get("frames", []) if block else []
    if not frames:
        raise ValueError(f"畫面卡 {element['card']} 沒有可用的截圖")
    frame = next((f for f in frames if f.get("frame_id") == element["frame"]), None) if element["frame"] else frames[-1]
    if frame is None:
        raise ValueError(f"畫面卡 {element['card']} 沒有幀 {element['frame']}")
    source = ROOT / frame["path"]
    target = folder / "assets" / f"{element['card']}-{frame.get('frame_id') or 'frame'}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        if image.width > 1280:
            image = image.resize((1280, round(image.height * 1280 / image.width)))
        image.save(target)
    return {"path": target, "width_px": 1280}


def build(run, source_path=None, preview=False):
    doc = src.load_run(run)
    folder = tasks.task_dir("handout", doc["run_id"])
    source_file = Path(source_path) if source_path else folder / "handout.md"
    if not source_file.is_file():
        raise ValueError(f"找不到講義原稿：{src.rel(source_file)}，先 prepare 並依 SKILL 撰寫")
    raw = source_file.read_bytes()
    report = {"checked_at": tasks.now(), "primary_sha256": doc["primary_sha256"],
              "source_sha256": hashlib.sha256(raw).hexdigest(), "errors": [], "warnings": []}
    output = tasks.output_path("handout", doc)
    try:
        meta, chapters = hp.parse(raw.decode("utf-8"), _speech_order(doc))
        domain = domains.load(meta.get("domain"))
        theme = load_theme(meta.get("theme"))
    except ValueError as exc:
        report["errors"].append(str(exc))
        return _finish(doc, folder, report, output, None)
    errors, warnings, stats = check(doc, meta, chapters, domain)
    report["errors"] += errors
    report["warnings"] += warnings
    report["stats"] = stats
    assets = {}
    if not errors:
        diagrams = [e for c in chapters for e in c["elements"] if e["type"] == "mermaid"]
        if diagrams:
            from derived_common.mermaid import Mermaid
            limit = theme["mermaid"].get("max_width_px", 720)
            with Mermaid(theme["mermaid"]) as mermaid:
                for number, element in enumerate(diagrams, 1):
                    png = folder / "assets" / f"diagram-{number:02d}.png"
                    rendered = mermaid.render(element["code"], png)
                    if not rendered.get("ok"):
                        report["errors"].append(f"第 {number} 張 Mermaid 圖無法渲染：{rendered.get('error', '').splitlines()[0]}")
                        continue
                    if rendered["width"] > limit:
                        report["warnings"].append(f"第 {number} 張圖寬 {rendered['width']:.0f}px，手機上會太小；改成由上往下或拆圖")
                    assets[id(element)] = {"path": png, "width_px": rendered["width"]}
        for chapter in chapters:
            for element in chapter["elements"]:
                if element["type"] == "image":
                    try:
                        assets[id(element)] = _frame_asset(doc, element, folder)
                    except (ValueError, OSError) as exc:
                        report["errors"].append(str(exc))
    if report["errors"]:
        return _finish(doc, folder, report, output, None)
    render(meta, chapters, assets, theme, output)
    report.update(output=src.rel(output), output_sha256=src.sha256_file(output), theme=theme["name"],
                  chapters=len(chapters),
                  diagrams=sum(e["type"] == "mermaid" for c in chapters for e in c["elements"]),
                  images=sum(e["type"] == "image" for c in chapters for e in c["elements"]))
    if preview:
        try:
            from handout.preview import pages
            report["preview"] = [src.rel(p) for p in pages(output, folder / "preview")]
        except (RuntimeError, OSError) as exc:
            report["warnings"].append(f"無法產生預覽：{exc}")
    return _finish(doc, folder, report, output, output)


def _finish(doc, folder, report, output, written):
    tasks.write_json(folder / "check.json", report)
    status = "completed" if written else "needs_fix"
    tasks.write_task("handout", folder, doc, status=status, output=src.rel(output) if written else None,
                     message="已輸出講義" if written else "依 check.json 修正 handout.md 對應處後重跑 build")
    return {"status": status, "task": src.rel(folder), **report}
