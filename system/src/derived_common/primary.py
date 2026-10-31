"""The primary transcript is the user's file; their edits to it are authoritative.

Any change that keeps the paragraph structure recorded in reading_map.json
(count, order, speech/visual kind) is accepted as it is, because every
paragraph still lines up with its recorded timing. Paragraphs cut from the end
are accepted too: what remains still lines up, and the cut ones are recorded as
removed. Other structural changes are refused. Before a version can be
replaced, a copy is kept in the run's primary_history folder, which is
append-only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

HISTORY = "primary_history"
LEDGER = "index.jsonl"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compare(text, mapping):
    """Return (problem, edits): problem is None while the structure still matches."""
    from derived_common.source import parse_reading_edition
    try:
        _, sections = parse_reading_edition(text)
    except ValueError as exc:
        return str(exc), []
    chunks = [chunk for section in sections for chunk in section["chunks"]]
    for number, (chunk, record) in enumerate(zip(chunks, mapping), 1):
        if chunk["kind"] != record["kind"]:
            kinds = {"speech": "口述", "visual": "畫面"}
            return (f"第 {number} 段原本是{kinds[record['kind']]}，現在是{kinds[chunk['kind']]}；"
                    "段落被增刪、合併或改了類型，時間無法對應"), []
    if len(chunks) > len(mapping) or not chunks:
        return (f"主稿有 {len(chunks)} 段，時間對應紀錄有 {len(mapping)} 段；"
                "段落被增刪或合併，時間無法對應"), []
    edits = []
    for number, (chunk, record) in enumerate(zip(chunks, mapping), 1):
        if chunk["text"] != record["text"]:
            similarity = SequenceMatcher(None, record["text"], chunk["text"], autojunk=False).ratio()
            edits.append({"paragraph": number, "kind": chunk["kind"],
                          "mapped_text_sha256": _sha(record["text"]),
                          "edited_text_sha256": _sha(chunk["text"]),
                          "similarity": round(similarity, 3)})
    for number, record in enumerate(mapping[len(chunks):], len(chunks) + 1):
        edits.append({"paragraph": number, "kind": record["kind"], "removed": True,
                      "mapped_text_sha256": _sha(record["text"])})
    return None, edits


def snapshot(run, path, reason):
    """Keep the current file before it can be replaced; one copy per distinct content."""
    path = Path(path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(run) / HISTORY
    if folder.is_dir():
        existing = next(folder.glob(f"*_{digest[:16]}.md"), None)
        if existing:
            return existing
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    target = folder / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{digest[:16]}.md"
    target.write_bytes(data)
    entry = {"file": target.name, "sha256": digest, "reason": reason, "saved_at": now.isoformat(),
             "primary": path.name}
    with (folder / LEDGER).open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return target
