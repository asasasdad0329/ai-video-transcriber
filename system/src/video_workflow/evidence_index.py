"""Local visual inventory and selective evidence retrieval, without model calls.

Labels and representative ranks are heuristics, never semantic review results.
All source frames remain retrievable, including states not chosen for preview.
"""
from __future__ import annotations

import hashlib
import json
import math
from difflib import SequenceMatcher
from pathlib import Path


CONTACT_SHEET_GROUPS = 12


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _features(frame, resolve):
    from PIL import Image, ImageFilter, ImageStat
    path = resolve(frame["path"])
    with Image.open(path) as opened:
        im = opened.convert("RGB")
    w, h = im.size
    rows = []
    for item in frame.get("boxes", []):
        points = item["box"]
        xs = [number(p[0], "box x") for p in points]
        ys = [number(p[1], "box y") for p in points]
        if not xs or not ys or min(xs) < 0 or min(ys) < 0 or max(xs) > w or max(ys) > h:
            raise ValueError("OCR box outside image")
        box = [min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h]
        # Candidate region only; bottom-position text may also be a chart label.
        bottom = box[1] >= .65 and box[3] - box[1] <= .15
        rows.append({"text": item["text"], "box": box, "bottom_candidate": bottom})
    gray = im.convert("L")
    sharpness = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
    body = gray.crop((0, 0, w, int(h * .65)))
    return {"id": frame["id"], "time": number(frame["time"], "frame time"),
            "path": str(Path(path).resolve()), "image_sha256": file_hash(path),
            "size": [w, h], "text": frame.get("text", ""), "rows": rows,
            "sharpness": sharpness, "body_hash": hashlib.sha256(body.tobytes()).hexdigest(),
            "full_pixel_hash": hashlib.sha256(im.tobytes()).hexdigest(),
            "body_preview": list(body.resize((32, 24)).getdata()),
            "duplicate_of": frame.get("duplicate_of"),
            "ocr_evidence_time": frame.get("ocr_evidence_time", frame["time"])}


def build(visuals, run_id, resolve):
    frames = [_features(f, resolve) for f in visuals["frames"]]
    ids = [f["id"] for f in frames]
    if not frames or len(ids) != len(set(ids)):
        raise ValueError("visual index requires unique nonempty frames")
    if any(f["time"] < 0 for f in frames) or any(b["time"] < a["time"] for a, b in zip(frames, frames[1:])):
        raise ValueError("frame times must be nonnegative and ordered")
    groups = []
    previous = None
    for pos, frame in enumerate(frames):
        neighbors = frames[max(0, pos - 2):pos + 3]
        bottom_texts = {"".join(r["text"] for r in f["rows"] if r["bottom_candidate"])
                        for f in neighbors}
        bottom_texts.discard("")
        likely_subtitles = len(bottom_texts) >= 2
        frame["subtitle_text"] = "\n".join(r["text"] for r in frame["rows"]
                                            if likely_subtitles and r["bottom_candidate"])
        frame["data_text"] = "\n".join(r["text"] for r in frame["rows"]
                                        if not (likely_subtitles and r["bottom_candidate"]))
        frame["subtitle_candidate"] = bool(frame["subtitle_text"])
        frame["label"] = "data_candidate" if frame["data_text"] else "scenario_or_unknown"
        if (frame["subtitle_candidate"] and not frame["data_text"] and len(neighbors) >= 3
                and all(n["body_hash"] == frame["body_hash"] for n in neighbors)):
            frame["label"] = "subtitle_only_candidate"
        # Do not infer a decorative scene from absent OCR or unchanged pixels.
        same = previous is not None and frame["body_hash"] == previous["body_hash"]
        if previous and not same and frame["data_text"] and previous["data_text"]:
            similarity = SequenceMatcher(None, previous["data_text"], frame["data_text"]).ratio()
            delta = sum(abs(a - b) for a, b in zip(previous["body_preview"], frame["body_preview"])) / (255 * 32 * 24)
            same = similarity >= .65 and delta <= .12
        if (not previous or not same or frame["label"] != previous["label"]
                or frame["time"] - groups[-1]["start"] > 60):
            groups.append({"id": f"g{len(groups) + 1:04d}", "start": frame["time"],
                           "end": frame["time"], "label": frame["label"], "frame_ids": []})
        groups[-1]["frame_ids"].append(frame["id"])
        groups[-1]["end"] = frame["time"]
        frame["group_id"] = groups[-1]["id"]
        previous = frame
    by_id = {f["id"]: f for f in frames}
    for group in groups:
        members = [by_id[fid] for fid in group["frame_ids"]]
        # Text amount is only one rank signal; no frame is discarded by rank.
        best = max(members, key=lambda f: (len(f["data_text"]), min(f["sharpness"], 2000), f["time"]))
        group["representative_id"] = best["id"]
        group["state_change_ids"] = [f["id"] for i, f in enumerate(members)
                                     if i == 0 or f["full_pixel_hash"] != members[i-1]["full_pixel_hash"]]
        group["requires_semantic_review"] = True
        group["rank_is_completeness_proof"] = False
    for frame in frames:
        del frame["body_preview"]
    return {"version": 1, "run_id": run_id, "frames": frames, "groups": groups,
            "source_coverage": visuals.get("coverage", {}), "semantic_reviewed": False,
            "limitations": ["Region and group labels are heuristics, not semantic categories.",
                             "Bottom text may be a data label; full images remain available.",
                             "Representative ranks do not prove completeness; inspect changed states for arrows or deletions.",
                             "No text does not imply no useful visual content; unknown scenes require an overview."]}


def menu(index):
    frames = {f["id"]: f for f in index["frames"]}
    return {"run_id": index["run_id"], "semantic_reviewed": False,
            "groups": [{"id": g["id"], "start": g["start"], "end": g["end"],
                        "label": g["label"], "representative_id": g["representative_id"],
                        "frame_count": len(g["frame_ids"]), "changed_states": len(g["state_change_ids"]),
                        "data_preview": frames[g["representative_id"]]["data_text"][:160]}
                       for g in index["groups"]], "limitations": index["limitations"]}


def contact_sheets(index, directory):
    from PIL import Image, ImageDraw
    frames = {f["id"]: f for f in index["frames"]}
    chosen = [frames[g["representative_id"]] for g in index["groups"]]
    outputs = []
    for offset in range(0, len(chosen), CONTACT_SHEET_GROUPS):
        batch = chosen[offset:offset + CONTACT_SHEET_GROUPS]
        canvas = Image.new("RGB", (960, math.ceil(len(batch) / 3) * 210), "white")
        draw = ImageDraw.Draw(canvas)
        for n, frame in enumerate(batch):
            with Image.open(frame["path"]) as source:
                thumb = source.convert("RGB")
                thumb.thumbnail((320, 180))
            x, y = (n % 3) * 320, (n // 3) * 210
            canvas.paste(thumb, (x, y))
            draw.text((x + 2, y + 182), f'{frame["group_id"]} {frame["time"]:.2f}s', fill="black")
        path = Path(directory) / f"overview-{offset // CONTACT_SHEET_GROUPS + 1:03d}.jpg"
        canvas.save(path, quality=85)
        outputs.append(str(path.resolve()))
    return outputs


def review_batches(index, directory, sheets_per_batch=3, batch_id=None):
    """Partition existing contact sheets without sending image or full OCR data to the planner."""
    if isinstance(sheets_per_batch, bool) or not isinstance(sheets_per_batch, int) or not 1 <= sheets_per_batch <= 4:
        raise ValueError("sheets_per_batch must be an integer from 1 to 4")
    groups = index["groups"]
    if not groups:
        raise ValueError("visual review requires at least one group")
    directory = Path(directory).resolve()
    sheet_count = math.ceil(len(groups) / CONTACT_SHEET_GROUPS)
    sheets = [directory / f"overview-{n:03d}.jpg" for n in range(1, sheet_count + 1)]
    if any(not sheet.is_file() for sheet in sheets):
        raise ValueError("contact sheet missing from evidence index")
    batches = []
    for sheet_offset in range(0, sheet_count, sheets_per_batch):
        selected_sheets = sheets[sheet_offset:sheet_offset + sheets_per_batch]
        selected_groups = groups[sheet_offset * CONTACT_SHEET_GROUPS:
                                 (sheet_offset + len(selected_sheets)) * CONTACT_SHEET_GROUPS]
        batches.append({"batch_id": f"vb{len(batches) + 1:03d}",
                        "start": selected_groups[0]["start"], "end": selected_groups[-1]["end"],
                        "first_group_id": selected_groups[0]["id"],
                        "last_group_id": selected_groups[-1]["id"],
                        "group_count": len(selected_groups), "sheet_count": len(selected_sheets),
                        "_groups": selected_groups, "_sheets": selected_sheets})
    if batch_id is not None:
        selected = next((item for item in batches if item["batch_id"] == batch_id), None)
        if selected is None:
            raise ValueError("unknown visual batch ID")
        return {"run_id": index["run_id"], "index_directory": str(directory),
                "batch_id": batch_id, "contact_sheets": [str(path) for path in selected["_sheets"]],
                "groups": [{"id": group["id"], "start": group["start"], "end": group["end"],
                            "label": group["label"], "representative_id": group["representative_id"],
                            "frame_count": len(group["frame_ids"]),
                            "changed_states": len(group["state_change_ids"])}
                           for group in selected["_groups"]],
                "semantic_reviewed": False}
    return {"run_id": index["run_id"], "index_directory": str(directory),
            "sheets_per_batch": sheets_per_batch, "batch_count": len(batches),
            "batches": [{key: value for key, value in item.items() if not key.startswith("_")}
                        for item in batches], "semantic_reviewed": False}


def select(index, *, start=None, end=None, group_id=None, frame_ids=None, full=False,
           offset=0, limit=8):
    if group_id:
        groups = [g for g in index["groups"] if g["id"] == group_id]
        if not groups:
            raise ValueError("unknown group ID")
        ids = groups[0]["frame_ids"] if full else list(dict.fromkeys(
            [groups[0]["representative_id"], *groups[0]["state_change_ids"]]))
    elif frame_ids:
        ids = frame_ids
    elif start is not None and end is not None:
        start, end = number(start, "start"), number(end, "end")
        if start < 0 or end < start:
            raise ValueError("invalid time range")
        ids = [f["id"] for f in index["frames"] if start <= f["time"] <= end]
    else:
        raise ValueError("select a group, frames, or start/end range")
    known = {f["id"] for f in index["frames"]}
    if len(ids) != len(set(ids)) or set(ids) - known:
        raise ValueError("unknown or duplicate frame ID")
    if not isinstance(offset, int) or offset < 0 or (limit is not None and (not isinstance(limit, int) or limit < 1)):
        raise ValueError("invalid page")
    ids = ids[offset:offset+limit if limit is not None else None]
    frames = {f["id"]: f for f in index["frames"]}
    return [{k: f[k] for k in ("id", "time", "path", "text", "subtitle_text", "data_text",
                               "image_sha256", "ocr_evidence_time")}
            for f in (frames[fid] for fid in ids)]


def focused_bundle(transcript, triage, index, segment_ids=None, rechecks=None, vocabulary=None):
    segments = transcript["segments"]
    positions = {s["id"]: i for i, s in enumerate(segments)}
    suspects = {s["segment_id"]: s for s in triage["suspects"]}
    ids = segment_ids if segment_ids is not None else list(suspects)
    if len(ids) != len(set(ids)) or set(ids) - set(positions):
        raise ValueError("unknown or duplicate segment ID")
    context, evidence, items, visual_context = {}, {}, [], {}
    groups = {g["id"]: g for g in index["groups"]}
    indexed_frames = {f["id"]: f for f in index["frames"]}
    rechecks = rechecks or {}
    for sid in ids:
        s = segments[positions[sid]]
        for neighbor in segments[max(0, positions[sid]-1):positions[sid]+2]:
            context[neighbor["id"]] = {k: neighbor[k] for k in ("id", "start", "end", "text")}
        nearby = [f for f in index["frames"] if s["start"] - 20 <= f["time"] <= s["end"] + 20]
        suspect = suspects.get(sid, {})
        candidate_ids = {c.get("evidence_id") for c in suspect.get("candidates", [])}
        current = suspect.get("current_decision")
        if current:
            candidate_ids.update(current.get("evidence_ids", []))
        # Always retain cited candidates even when they are outside the initial window.
        cited = [f for f in index["frames"] if f["id"] in candidate_ids]
        near = sorted(nearby, key=lambda f: (0 if s["start"] <= f["time"] <= s["end"] else 1,
                                             abs(f["time"] - (s["start"] + s["end"])/2)))
        selected = {f["id"]: f for f in [*near[:2], *cited]}
        refs = []
        for f in selected.values():
            # Cited candidates keep full OCR. Otherwise send changing subtitles and
            # share stable data-region text once per group; all full frames are queryable.
            payload = {k: f[k] for k in ("id", "time", "ocr_evidence_time")}
            payload["text"] = f["text"] if f["id"] in candidate_ids or not f["subtitle_text"] else f["subtitle_text"]
            payload["full_frame_available"] = True
            if f["id"] in evidence and len(evidence[f["id"]]["text"]) > len(payload["text"]):
                payload = evidence[f["id"]]
            evidence[f["id"]] = payload
            group = groups[f["group_id"]]
            representative = indexed_frames[group["representative_id"]]
            visual_context[group["id"]] = {"group_id": group["id"], "label": group["label"],
                "representative_id": representative["id"], "path": representative["path"],
                "data_text": representative["data_text"], "changed_states": len(group["state_change_ids"]),
                "meaning_unverified": True}
            refs.append(f["id"])
        row = {"segment_id": sid, "reasons": suspect.get("reasons", ["manual_review"]),
               "candidates": suspect.get("candidates", []), "evidence_ids": refs,
               "available_nearby_frames": len(nearby), "needs_more_evidence": not bool(selected),
               "expand_range": [max(0, s["start"]-20), s["end"]+20]}
        if current:
            row["current_decision"] = current
        if sid in rechecks:
            row["recheck"] = rechecks[sid]
        if vocabulary:
            row["term_hints"] = vocabulary(s["text"])
        items.append(row)
    return {"run_id": index["run_id"], "transcript_sha256": digest(transcript),
            "segments": sorted(context.values(), key=lambda s: positions[s["id"]]),
            "items": items, "evidence": list(evidence.values()),
            "visual_context": list(visual_context.values()),
            "selection_is_exhaustive": False, "audio_verified": False,
            "expand_instruction": "If evidence is insufficient or conflicting, query the listed time range; do not guess.",
            "omitted_details": "Decoder scores, OCR boxes and unselected frames remain in source files."}
