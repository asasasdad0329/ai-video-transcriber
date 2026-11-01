"""Validate an agent's data-card plan and assemble the Markdown card.

The plan only points at blocks and quotes source sentences; every transcript
sentence in the card is copied by this program from the reading edition.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

import domains
from derived_common import source as src
from derived_common import tasks
from derived_common.mermaid import labels
from derived_common.text import contains, meta_hits, squash, unresolved_clauses

SCHEMA = "datacard-plan/1"
MAX_KEYWORDS = 15
MAX_SUMMARY = 2
MAX_MADE_BY = 60


def draft(doc):
    sections = []
    for section in doc["sections"]:
        entry = {"title": section["title"], "blocks": list(section["blocks"]), "summary": [], "terms": [],
                 "cases": [], "diagrams": []}
        if section.get("part"):
            entry = {"part": section["part"], **entry}
        sections.append(entry)
    return {"schema": SCHEMA, "primary_sha256": doc["primary_sha256"], "domain": None, "keywords": [],
            "sections": sections, "differences": []}


def target(runs, name=None, *, allow_edited_primary=None):
    """One run, several runs (a series), or the ID of a series task already prepared."""
    runs = [r for r in ([runs] if isinstance(runs, str) else list(runs)) if r]
    if not runs:
        raise ValueError("請指定影片或系列任務 ID")
    if len(runs) == 1:
        task = tasks.read_task(tasks.task_dir("datacard", runs[0], create=False))
        if task.get("parts"):
            allowed = task.get("allow_edited_primary", False) if allow_edited_primary is None else allow_edited_primary
            return src.load_series([p["run_id"] for p in task["parts"]], name or task.get("title"),
                                   **({"allow_edited_primary": True} if allowed else {}))
        run = src.find_run(runs[0])
        task = tasks.read_task(tasks.task_dir("datacard", run.name, create=False))
        allowed = task.get("allow_edited_primary", False) if allow_edited_primary is None else allow_edited_primary
        return src.load_run(run, **({"allow_edited_primary": True} if allowed else {}))
    return src.load_series(runs, name, **({"allow_edited_primary": True} if allow_edited_primary else {}))


def prepare(runs, name=None, *, allow_edited_primary=False):
    doc = target(runs, name, allow_edited_primary=allow_edited_primary)
    folder = tasks.task_dir("datacard", doc["run_id"])
    (folder / "packet.md").write_text(src.packet(doc, "datacard"), encoding="utf-8")
    tasks.write_json(folder / "plan.draft.json", draft(doc))
    tasks.write_task("datacard", folder, doc, status="awaiting_plan",
                     message="讀 packet.md 後依 SKILL 寫 plan.json，再執行 datacard build")
    result = {"task": src.rel(folder), "packet": src.rel(folder / "packet.md"),
              "draft": src.rel(folder / "plan.draft.json"), "plan": src.rel(folder / "plan.json")}
    if doc.get("parts"):
        result.update(series=doc["run_id"], title=doc["title"], output=src.rel(tasks.output_path("datacard", doc)),
                      parts=[{"part": p["part"], "run_id": p["run_id"], "title": p["title"]} for p in doc["parts"]])
    return result


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def validate(doc, plan, parse_mermaid=None):
    """Return (errors, warnings, sections) where sections carry resolved block data."""
    errors, warnings = [], []
    whole = src.document_text(doc)
    if plan.get("schema") != SCHEMA:
        errors.append(f"schema 必須是 {SCHEMA}")
    if plan.get("primary_sha256") != doc["primary_sha256"]:
        errors.append("plan 對應的主稿版本已變更，重新 prepare 後再寫 plan")
    try:
        domain = domains.load(plan.get("domain"))
    except ValueError as exc:
        errors.append(str(exc))
        domain = domains.Plain()
    made_by = plan.get("made_by")
    if made_by is not None and (not isinstance(made_by, str) or not made_by.strip() or len(made_by) > MAX_MADE_BY):
        errors.append(f"made_by 需為 1–{MAX_MADE_BY} 字的製作模型名稱")

    keywords = _as_list(plan.get("keywords"))
    if len(keywords) > MAX_KEYWORDS:
        errors.append(f"keywords 最多 {MAX_KEYWORDS} 個")
    for word in keywords:
        if not isinstance(word, str) or not contains(whole, word):
            errors.append(f"keywords 不在原文中：{word}")

    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        return errors + ["sections 必須是非空清單"], warnings, []
    flat = [b for s in sections for b in _as_list(s.get("blocks"))]
    if flat != doc["order"]:
        errors.append("sections.blocks 必須依原順序恰好涵蓋全部段落與畫面（可合併相鄰小節，不可拆散或重排）")
        return errors, warnings, []

    resolved = []
    for number, section in enumerate(sections, 1):
        sid = f"S{number:02d}"
        ids = section["blocks"]
        in_parts = {doc["blocks"][b].get("part") for b in ids}
        if len(in_parts) > 1:
            errors.append(f"{sid} 不可跨部合併小節（第 {'、'.join(str(p) for p in sorted(in_parts))} 部）")
        speech = "\n".join(doc["blocks"][b]["text"] for b in ids if doc["blocks"][b]["kind"] == "speech")
        visual = "\n".join(doc["blocks"][b]["text"] for b in ids if doc["blocks"][b]["kind"] == "visual")
        both = speech + "\n" + visual
        title = section.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 30:
            errors.append(f"{sid} title 需為 1–30 字")
        elif meta_hits(title, whole):
            errors.append(f"{sid} title 含整理說明用語：{'、'.join(meta_hits(title, whole))}")

        summary = _as_list(section.get("summary"))
        if len(summary) > MAX_SUMMARY:
            errors.append(f"{sid} summary 最多 {MAX_SUMMARY} 句")
        for sentence in summary:
            if not isinstance(sentence, str) or not contains(speech, sentence):
                errors.append(f"{sid} summary 必須是本節口述原句：{sentence}")
            elif len(squash(sentence)) > 120:
                warnings.append(f"{sid} summary 偏長（{len(squash(sentence))} 字）")
        if speech.strip() and not summary:
            warnings.append(f"{sid} 沒有 summary")

        for term in _as_list(section.get("terms")):
            if not isinstance(term, dict) or not contains(both, term.get("term", "")) or not contains(both, term.get("text", "")):
                errors.append(f"{sid} terms 的詞與原文都必須出自本節：{term}")

        for case in _as_list(section.get("cases")):
            if not isinstance(case, dict) or not isinstance(case.get("name"), str) or not case["name"].strip():
                errors.append(f"{sid} cases 需要 name")
                continue
            if meta_hits(case["name"], whole):
                errors.append(f"{sid} case name 含整理說明用語：{case['name']}")
            for key, value in (case.get("fields") or {}).items():
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{sid} {case['name']}.{key} 必須是非空字串")
                    continue
                problem = domain.validate_case_field(key, value, both)
                if problem:
                    errors.append(f"{sid} {case['name']}：{problem}")
                elif not domain.handles(key) and not contains(both, value):
                    errors.append(f"{sid} {case['name']}.{key} 不在本節原文或畫面中：{value}")
            for quote in _as_list(case.get("quotes")):
                if not isinstance(quote, str) or not contains(both, quote):
                    errors.append(f"{sid} {case['name']} quotes 必須是本節原句：{quote}")

        cards_here = {b for b in ids if doc["blocks"][b]["kind"] == "visual"}
        diagrams = []
        for diagram in _as_list(section.get("diagrams")):
            card = diagram.get("card") if isinstance(diagram, dict) else None
            code = diagram.get("mermaid") if isinstance(diagram, dict) else None
            if card not in cards_here or not isinstance(code, str) or not code.strip():
                errors.append(f"{sid} diagrams 必須指向本節畫面卡並提供 mermaid")
                continue
            if parse_mermaid:
                problem = parse_mermaid(code)
                if problem:
                    errors.append(f"{sid} {card} Mermaid 語法錯誤：{problem.splitlines()[0]}")
            card_text = doc["blocks"][card]["text"]
            stray = [label for label in labels(code) if not contains(card_text, label)]
            if stray:
                errors.append(f"{sid} {card} 圖中文字不在該畫面轉錄中：{'、'.join(stray)}")
            diagrams.append({"card": card, "mermaid": code.strip()})
        resolved.append({"id": sid, "title": title, "blocks": ids, "part": doc["blocks"][ids[0]].get("part"),
                         "summary": summary,
                         "terms": _as_list(section.get("terms")), "cases": _as_list(section.get("cases")),
                         "diagrams": diagrams})

    speech_all = src.document_text(doc, ("speech",))
    visual_all = src.document_text(doc, ("visual",))
    for item in _as_list(plan.get("differences")):
        if not isinstance(item, dict) or not contains(speech_all, item.get("speech", "")) \
                or not contains(visual_all, item.get("screen", "")):
            errors.append(f"differences 需是口述原句與畫面原文的配對：{item}")
    return errors, warnings, resolved


def assemble(doc, plan, resolved):
    blocks = doc["blocks"]
    unresolved = []
    for section in resolved:
        for bid in section["blocks"]:
            for clause in unresolved_clauses(blocks[bid]["text"]):
                unresolved.append({"節": section["id"], "原文": clause})
            # What the screen showed but could not be read stays data, not a note in the text.
            for note in blocks[bid].get("notes", []):
                unresolved.append({"節": section["id"], "畫面": blocks[bid]["title"], "疑點": note})
    differences = []
    for item in _as_list(plan.get("differences")):
        where = next((s["id"] for s in resolved for b in s["blocks"]
                      if blocks[b]["kind"] == "speech" and contains(blocks[b]["text"], item["speech"])), None)
        differences.append({"節": where, "口述": item["speech"], "畫面": item["screen"]})

    front = {"doc_id": f"datacard-{doc['run_id']}", "title": doc["title"], "doc_type": "video_datacard",
             "language": "zh-Hant"}
    if plan.get("made_by"):
        front["made_by"] = plan["made_by"].strip()
    if doc.get("parts"):
        front["doc_type"] = "video_datacard_series"
        front["parts"] = [{"part": p["part"], "title": p["title"], "video": p["source_video"],
                           "duration": src.clock(p["duration"]), "transcript_run": p["run_id"],
                           "sections": f"{p['sections'][0]}–{p['sections'][-1]}"} for p in doc["parts"]]
    else:
        front["source"] = {"video": doc["source_video"], "duration": src.clock(doc["duration"]),
                           "transcript_run": doc["run_id"]}
    if plan.get("domain"):
        front["domain"] = plan["domain"]
    if plan.get("keywords"):
        front["keywords"] = list(plan["keywords"])
    front["sections"] = len(resolved)
    if unresolved:
        front["unresolved"] = unresolved
    if differences:
        front["differences"] = differences
    dump = lambda data: yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=4096).rstrip()

    series = bool(doc.get("parts"))
    lines = ["---", dump(front), "---", "", f"# {doc['title']}", "", "## 目錄", ""]
    lines += (["| 節 | 部 | 時間 | 標題 | 摘要 |", "| --- | --- | --- | --- | --- |"] if series
              else ["| 節 | 時間 | 標題 | 摘要 |", "| --- | --- | --- | --- |"])
    for section in resolved:
        span = f"{src.clock(_start(doc, section))}–{src.clock(_end(doc, section))}"
        summary = " ".join(section["summary"]).replace("|", "｜")
        part = f" {section['part']} |" if series else ""
        lines.append(f"| {section['id']} |{part} {span} | {section['title']} | {summary} |")
    lines.append("")
    shown = None
    for section in resolved:
        span = f"{src.clock(_start(doc, section))}–{src.clock(_end(doc, section))}"
        if series and section["part"] != shown:
            shown = section["part"]
            part = doc["parts"][shown - 1]
            lines += [f"## 第 {shown} 部｜{part['title']}", ""]
        # In a series the parts take the h2 level, so their sections sit one level down.
        lines += [f"{'###' if series else '##'} {section['id']}｜{section['title']}｜{span}", ""]
        structure = {}
        if section["terms"]:
            structure["術語"] = [{"詞": t["term"], "原文": t["text"]} for t in section["terms"]]
        if section["cases"]:
            structure["案例"] = [{"名稱": c["name"], **(c.get("fields") or {}),
                                  **({"原文": _as_list(c.get("quotes"))} if c.get("quotes") else {})}
                                 for c in section["cases"]]
        if structure:
            lines += ["```yaml", dump(structure), "```", ""]
        diagrams = {d["card"]: d["mermaid"] for d in section["diagrams"]}
        for bid in section["blocks"]:
            block = blocks[bid]
            if block["kind"] == "speech":
                lines += [block["text"], ""]
                continue
            lines.append(f"> 畫面：{block['title']}")
            lines.append(">")
            lines += ["> " + line if line else ">" for line in block["text"].splitlines()]
            lines.append("")
            if bid in diagrams:
                lines += ["```mermaid", diagrams[bid], "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def _start(doc, section):
    speech = [doc["blocks"][b] for b in section["blocks"] if doc["blocks"][b]["kind"] == "speech"]
    return min(b["start"] for b in (speech or [doc["blocks"][b] for b in section["blocks"]]))


def _end(doc, section):
    speech = [doc["blocks"][b] for b in section["blocks"] if doc["blocks"][b]["kind"] == "speech"]
    return max(b["end"] for b in (speech or [doc["blocks"][b] for b in section["blocks"]]))


def build(runs, plan_path=None, name=None):
    doc = target(runs, name)
    folder = tasks.task_dir("datacard", doc["run_id"])
    plan_file = Path(plan_path) if plan_path else folder / "plan.json"
    if not plan_file.is_file():
        raise ValueError(f"找不到 plan：{src.rel(plan_file)}，先 prepare 並依 SKILL 撰寫")
    raw = plan_file.read_bytes()
    plan = json.loads(raw.decode("utf-8"))
    needs_browser = any(s.get("diagrams") for s in plan.get("sections", []) if isinstance(s, dict))
    if needs_browser:
        from derived_common.mermaid import Mermaid
        with Mermaid() as mermaid:
            errors, warnings, resolved = validate(doc, plan, mermaid.parse)
    else:
        errors, warnings, resolved = validate(doc, plan)
    report = {"checked_at": tasks.now(), "primary_sha256": doc["primary_sha256"],
              "plan_sha256": hashlib.sha256(raw).hexdigest(), "errors": errors, "warnings": warnings}
    output = tasks.output_path("datacard", doc)
    if not errors:
        text = assemble(doc, plan, resolved)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        report.update(output=src.rel(output), output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      characters=len(text))
    tasks.write_json(folder / "check.json", report)
    status = "completed" if not errors else "needs_fix"
    tasks.write_task("datacard", folder, doc, status=status, output=src.rel(output) if not errors else None,
                     message="已輸出資料卡" if not errors else "依 check.json 修正 plan 對應處後重跑 build")
    return {"status": status, "task": src.rel(folder), **report}
