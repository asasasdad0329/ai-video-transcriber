"""Read-only assembly and diagnostics for evidence-backed review documents.

No run files are opened or changed here. A successful result validates structure
and existing review contracts, not the factual correctness of a decision.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import review


def _error(errors, path, message):
    item = {"path": path, "message": message}
    if item not in errors:
        errors.append(item)


def _identifier(value):
    return not isinstance(value, bool) and isinstance(value, (str, int)) and value != ""


def _fields(value, allowed, required, path, errors):
    if not isinstance(value, dict):
        _error(errors, path, "must be an object")
        return False
    for key in value.keys() - allowed:
        _error(errors, f"{path}.{key}", "unknown field")
    for key in required - value.keys():
        _error(errors, f"{path}.{key}", "required field missing")
    return True


def _bundle(bundle, errors):
    if not isinstance(bundle, dict):
        _error(errors, "bundle", "must be an object")
        return {}, set()
    for key in ("run_id", "transcript_sha256"):
        if not isinstance(bundle.get(key), str) or not bundle[key].strip():
            _error(errors, f"bundle.{key}", "must be nonempty text")
    segments = bundle.get("segments")
    index = {}
    if not isinstance(segments, list):
        _error(errors, "bundle.segments", "must be a list")
    else:
        for number, segment in enumerate(segments):
            path = f"bundle.segments[{number}]"
            if not isinstance(segment, dict) or not _identifier(segment.get("id")):
                _error(errors, path, "segment must have a string or integer id")
                continue
            sid = segment["id"]
            if sid in index:
                _error(errors, path, f"duplicate segment id: {sid}")
            if not isinstance(segment.get("text"), str):
                _error(errors, path + ".text", "must be a string")
            else:
                index[sid] = segment
    evidence = bundle.get("evidence_ids")
    if evidence is None:
        evidence = bundle.get("evidence", [])
        if isinstance(evidence, list):
            evidence = [item.get("id") if isinstance(item, dict) else item for item in evidence]
    if not isinstance(evidence, (list, set)) or any(not isinstance(ref, str) or not ref for ref in evidence):
        _error(errors, "bundle.evidence_ids", "must contain nonempty evidence IDs")
        return index, set()
    return index, set(evidence)


def _binding(document, bundle, errors, path, require_hash=True):
    for key in ("run_id", "transcript_sha256"):
        if key == "transcript_sha256" and not require_hash and key not in document:
            continue
        if not isinstance(document.get(key), str) or not document[key].strip():
            _error(errors, f"{path}.{key}", "must be nonempty text")
        elif document[key] != bundle.get(key):
            _error(errors, f"{path}.{key}", "does not match current bundle (stale or different run)")


def _patch_errors(patch, index, evidence, path, errors, seen):
    allowed = {"segment_id", "old_text", "new_text", "decision", "evidence_ids", "reason"}
    if not _fields(patch, allowed, allowed, path, errors):
        return
    sid = patch.get("segment_id")
    known = _identifier(sid) and sid in index
    if not known:
        _error(errors, path + ".segment_id", "unknown or malformed segment id")
    elif sid in seen:
        _error(errors, path + ".segment_id", f"duplicate patch for segment: {sid}")
    if _identifier(sid):
        seen.add(sid)
    old = patch.get("old_text")
    new = patch.get("new_text")
    if not isinstance(old, str):
        _error(errors, path + ".old_text", "must be a string")
    elif known and old != index[sid]["text"]:
        _error(errors, path + ".old_text", "does not match exactly")
    if not isinstance(new, str):
        _error(errors, path + ".new_text", "must be a string")
    decision = patch.get("decision")
    if not isinstance(decision, str) or decision not in {"correct", "keep", "needs_review"}:
        _error(errors, path + ".decision", "unknown decision")
    elif decision == "correct":
        if isinstance(new, str) and (new == old or not new.strip()):
            _error(errors, path + ".new_text", "correction must contain a nonempty change")
        if not patch.get("evidence_ids"):
            _error(errors, path + ".evidence_ids", "correction requires evidence")
    elif new != old:
        _error(errors, path + ".new_text", "keep/needs_review must preserve the original text")
    if not isinstance(patch.get("reason"), str) or not patch["reason"].strip():
        _error(errors, path + ".reason", "nonempty reason required")
    refs = patch.get("evidence_ids")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
        _error(errors, path + ".evidence_ids", "unknown or malformed evidence ids")
    elif len(refs) != len(set(refs)):
        _error(errors, path + ".evidence_ids", "duplicate evidence ids")


def assemble(bundle, decisions):
    """Expand minimal decisions; return no patch document if any input is invalid.

    The caller supplies the current bundle. Its hash binds the full transcript,
    even when the bundle contains only a selected subset of segments.
    """
    errors = []
    index, evidence = _bundle(bundle, errors)
    fields = {"run_id", "transcript_sha256", "decisions"}
    if not _fields(decisions, fields, fields, "decisions", errors):
        return {"ok": False, "errors": errors, "patch_document": None}
    _binding(decisions, bundle if isinstance(bundle, dict) else {}, errors, "decisions")
    entries = decisions.get("decisions")
    patches, seen = [], set()
    if not isinstance(entries, list):
        _error(errors, "decisions.decisions", "must be a list")
        entries = []
    allowed = {"segment_id", "new_text", "decision", "evidence_ids", "reason"}
    for number, entry in enumerate(entries):
        path = f"decisions.decisions[{number}]"
        if not _fields(entry, allowed, allowed - {"new_text"}, path, errors):
            continue
        sid = entry.get("segment_id")
        segment = index.get(sid) if _identifier(sid) else None
        old = segment["text"] if segment else None
        patch = {key: copy.deepcopy(entry[key]) for key in allowed if key in entry}
        patch["old_text"] = old
        if "new_text" not in patch and entry.get("decision") in ("keep", "needs_review"):
            patch["new_text"] = old
        _patch_errors(patch, index, evidence, path, errors, seen)
        patches.append(patch)
    document = {"run_id": decisions.get("run_id"),
                "transcript_sha256": decisions.get("transcript_sha256"), "patches": patches}
    return {"ok": not errors, "errors": errors, "patch_document": document if not errors else None}


def _layout_errors(transcript, cards, layout, errors):
    from . import reading

    if not isinstance(layout, dict):
        _error(errors, "layout", "must be an object")
        return
    if not isinstance(cards, list) or any(not isinstance(card, dict) or not _identifier(card.get("id")) for card in cards):
        _error(errors, "cards", "must be a list of cards with IDs")
        return
    segments = transcript.get("segments", [])
    if not isinstance(segments, list) or any(not isinstance(s, dict) or not _identifier(s.get("id")) for s in segments):
        return  # The transcript validator reports malformed source segments.
    expected = [s["id"] for s in segments]
    card_ids = [card["id"] for card in cards]
    spoken, visual = [], []
    sections = layout.get("sections")
    if not isinstance(sections, list):
        _error(errors, "layout.sections", "must be a list")
        return
    for number, section in enumerate(sections):
        section_path = f"layout.sections[{number}]"
        if not isinstance(section, dict) or not isinstance(section.get("blocks"), list):
            _error(errors, section_path, "must contain a blocks list")
            continue
        for block_number, block in enumerate(section["blocks"]):
            path = f"{section_path}.blocks[{block_number}]"
            if not isinstance(block, dict):
                _error(errors, path, "must be an object")
                continue
            if "card_id" in block:
                cid = block["card_id"]
                if not _identifier(cid) or cid not in card_ids:
                    _error(errors, path + ".card_id", "unknown card ID")
                else:
                    visual.append(cid)
                continue
            ids = block.get("segment_ids")
            if not isinstance(ids, list) or not ids or any(not _identifier(sid) or sid not in expected for sid in ids):
                _error(errors, path + ".segment_ids", "unknown or empty speech range")
                ids = []
            spoken.extend(ids)
            punctuation = block.get("punctuation", {})
            if not isinstance(punctuation, dict):
                _error(errors, path + ".punctuation", "must be an object")
                continue
            for sid, ending in punctuation.items():
                if sid not in ids:
                    _error(errors, path + f".punctuation.{sid}", "punctuation outside paragraph")
                if not isinstance(ending, str) or not ending or not set(ending) <= reading._PUNCTUATION:
                    _error(errors, path + f".punctuation.{sid}", "only punctuation may be added")
    if spoken != expected:
        _error(errors, "layout.sections", "must cover all speech exactly once in original order")
    if len(visual) != len(set(visual)) or set(visual) != set(card_ids):
        _error(errors, "layout.sections", "must cover every visual card exactly once")
    # Keep rendering as the authority, including conversion/protected-term checks.
    try:
        reading.render(copy.deepcopy(transcript), copy.deepcopy(cards), {}, copy.deepcopy(layout))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        _error(errors, "layout", str(exc))


def preflight(transcript, bundle, patch_document, layout=None, cards=None):
    """Accumulate diagnostics without applying changes to caller-owned objects."""
    errors = []
    index, evidence = _bundle(bundle, errors)
    if not isinstance(transcript, dict):
        _error(errors, "transcript", "must be an object")
        return {"ok": False, "errors": errors}
    bundle = bundle if isinstance(bundle, dict) else {}
    digest = hashlib.sha256(json.dumps(transcript, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    if bundle.get("transcript_sha256") != digest:
        _error(errors, "bundle.transcript_sha256", "does not match current transcript (stale)")
    if transcript.get("run_id") != bundle.get("run_id"):
        _error(errors, "bundle.run_id", "does not match transcript run_id")
    try:
        segments = review._validate_segments(transcript)
        index = {segment["id"]: segment for segment in segments}
        for segment in bundle.get("segments", []) if isinstance(bundle.get("segments"), list) else []:
            if isinstance(segment, dict) and _identifier(segment.get("id")):
                if segment["id"] not in index or segment.get("text") != index[segment["id"]]["text"]:
                    _error(errors, "bundle.segments", f"stale or unknown segment: {segment['id']}")
    except (ValueError, TypeError, KeyError) as exc:
        _error(errors, "transcript", str(exc))
    fields = {"run_id", "transcript_sha256", "patches", "context_review"}
    if not _fields(patch_document, fields, {"run_id", "patches"}, "patch_document", errors):
        return {"ok": False, "errors": errors}
    _binding(patch_document, bundle, errors, "patch_document", require_hash=False)
    patches = patch_document.get("patches")
    if not isinstance(patches, list):
        _error(errors, "patch_document.patches", "must be a list")
        patches = []
    seen = set()
    for number, patch in enumerate(patches):
        _patch_errors(patch, index, evidence, f"patch_document.patches[{number}]", errors, seen)
    context = patch_document.get("context_review")
    if context is not None:
        if not isinstance(context, dict):
            _error(errors, "patch_document.context_review", "must be an object")
        else:
            for key in ("transcript_sha256", "visual_review_sha256"):
                if not bundle.get(key) or context.get(key) != bundle[key]:
                    _error(errors, f"patch_document.context_review.{key}", "basis is stale or missing")
            if context.get("reviewed_segment_ids") != list(index):
                _error(errors, "patch_document.context_review.reviewed_segment_ids", "must cover every segment once in order")
            source_cards = cards if cards is not None else bundle.get("cards", [])
            if not isinstance(source_cards, list) or any(not isinstance(c, dict) or "id" not in c for c in source_cards):
                _error(errors, "cards", "must contain card IDs")
            elif context.get("reviewed_card_ids") != [c["id"] for c in source_cards]:
                _error(errors, "patch_document.context_review.reviewed_card_ids", "must cover every visual card once in order")
    corrected = transcript
    try:
        corrected, _ = review.apply_patches(copy.deepcopy(transcript), copy.deepcopy(patch_document), evidence)
    except (ValueError, TypeError, KeyError) as exc:
        _error(errors, "patch_document", str(exc))
    if layout is not None:
        _layout_errors(corrected, [] if cards is None else cards, layout, errors)
    return {"ok": not errors, "errors": errors}
