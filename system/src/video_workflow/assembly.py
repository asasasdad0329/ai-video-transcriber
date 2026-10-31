"""Compile a range-based review plan without reading or modifying run files.

The caller supplies current source snapshots and previously validated cards.
Compilation validates mechanics only; it never attests to reading or listening.
"""
from __future__ import annotations

import copy

from . import preflight, reading, review


def _failure(errors):
    return {"ok": False, "errors": errors, "patch_document": None,
            "layout": None, "preview": None, "reading_map": None}


def _expand_layout(source, segments, errors):
    fields = {"title", "protected_terms", "sections"}
    if not preflight._fields(source, fields, {"title", "sections"}, "layout", errors):
        return None
    if not isinstance(source.get("title"), str):
        preflight._error(errors, "layout.title", "must be a string")
    terms = source.get("protected_terms", [])
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t for t in terms):
        preflight._error(errors, "layout.protected_terms", "must contain nonempty strings")
    sections = source.get("sections")
    if not isinstance(sections, list):
        preflight._error(errors, "layout.sections", "must be a list")
        return None
    ids = [s["id"] for s in segments]
    positions = {sid: number for number, sid in enumerate(ids)}
    layout = {"title": source.get("title"), "protected_terms": copy.deepcopy(terms), "sections": []}
    for number, section in enumerate(sections):
        path = f"layout.sections[{number}]"
        if not preflight._fields(section, {"title", "blocks"}, {"title", "blocks"}, path, errors):
            continue
        if not isinstance(section.get("title"), str):
            preflight._error(errors, path + ".title", "must be a string")
        if not isinstance(section.get("blocks"), list):
            preflight._error(errors, path + ".blocks", "must be a list")
            continue
        expanded = {"title": section.get("title"), "blocks": []}
        layout["sections"].append(expanded)
        for block_number, block in enumerate(section["blocks"]):
            block_path = f"{path}.blocks[{block_number}]"
            if not isinstance(block, dict):
                preflight._error(errors, block_path, "must be an object")
                continue
            if "card_id" in block:
                preflight._fields(block, {"card_id", "include_structure"}, {"card_id"}, block_path, errors)
                if "include_structure" in block and not isinstance(block["include_structure"], bool):
                    preflight._error(errors, block_path + ".include_structure", "must be a boolean")
                expanded["blocks"].append(copy.deepcopy(block))
                continue
            fields = {"start_segment_id", "end_segment_id", "punctuation"}
            preflight._fields(block, fields, fields - {"punctuation"}, block_path, errors)
            endpoints = [block.get("start_segment_id"), block.get("end_segment_id")]
            valid = True
            for key, sid in zip(("start_segment_id", "end_segment_id"), endpoints):
                if not preflight._identifier(sid) or sid not in positions:
                    preflight._error(errors, block_path + "." + key, "unknown or malformed segment ID")
                    valid = False
            selected = []
            if valid:
                start, end = (positions[sid] for sid in endpoints)
                if start > end:
                    preflight._error(errors, block_path, "speech range ends before it starts")
                else:
                    selected = ids[start:end + 1]
            punctuation = block.get("punctuation", {})
            if not isinstance(punctuation, dict):
                preflight._error(errors, block_path + ".punctuation", "must be an object")
            else:
                for sid, ending in punctuation.items():
                    if sid not in selected:
                        preflight._error(errors, block_path + f".punctuation.{sid}", "punctuation outside paragraph")
                    if not isinstance(ending, str) or not ending or not set(ending) <= reading._PUNCTUATION:
                        preflight._error(errors, block_path + f".punctuation.{sid}", "only punctuation may be added")
            expanded["blocks"].append({"segment_ids": selected, "punctuation": copy.deepcopy(punctuation)})
    return layout


def compile_review(transcript, bundle, plan, cards=None, prior_decisions=None):
    """Return patch, expanded layout, preview and map, or no artifacts on error.

    ``prior_decisions`` is the existing segment-ID-to-audit-entry mapping. New
    decisions override only their corresponding prior entries, preserving other
    unresolved markers. ``cards`` defaults to the bundle's verified cards;
    the plan can reference cards but cannot supply or overwrite their content.
    """
    errors = []
    fields = {"run_id", "transcript_sha256", "visual_review_sha256", "decisions", "layout"}
    if not preflight._fields(plan, fields, fields, "plan", errors):
        return _failure(errors)
    if not isinstance(bundle, dict):
        preflight._error(errors, "bundle", "must be an object")
        return _failure(errors)
    for document, path in ((plan, "plan"), (bundle, "bundle")):
        value = document.get("visual_review_sha256")
        if "visual_review_sha256" not in document:
            preflight._error(errors, path + ".visual_review_sha256", "required field missing")
        elif value is not None and (not isinstance(value, str) or not value.strip()):
            preflight._error(errors, path + ".visual_review_sha256", "must be nonempty text or null")
    if plan.get("visual_review_sha256") != bundle.get("visual_review_sha256"):
        preflight._error(errors, "plan.visual_review_sha256", "does not match current bundle (stale)")
    minimal = {key: copy.deepcopy(plan.get(key)) for key in ("run_id", "transcript_sha256", "decisions")}
    assembled = preflight.assemble(bundle, minimal)
    errors.extend(assembled["errors"])
    try:
        if not isinstance(transcript, dict):
            raise ValueError("must be an object")
        segments = review._validate_segments(transcript)
    except (ValueError, TypeError, KeyError) as exc:
        preflight._error(errors, "transcript", str(exc))
        return _failure(errors)
    layout = _expand_layout(plan.get("layout"), segments, errors)
    source_cards = copy.deepcopy(bundle.get("cards", []) if cards is None else cards)
    if not isinstance(source_cards, list):
        preflight._error(errors, "cards", "must be a list")
    else:
        seen = set()
        for number, card in enumerate(source_cards):
            if not isinstance(card, dict) or not preflight._identifier(card.get("id")):
                preflight._error(errors, f"cards[{number}]", "must be a card with an ID")
            elif card["id"] in seen:
                preflight._error(errors, f"cards[{number}].id", "duplicate card ID")
            else:
                seen.add(card["id"])
    decisions = {} if prior_decisions is None else copy.deepcopy(prior_decisions)
    if not isinstance(decisions, dict):
        preflight._error(errors, "prior_decisions", "must be a segment-ID-to-decision mapping")
    else:
        known = {s["id"] for s in segments}
        for sid, entry in decisions.items():
            if not preflight._identifier(sid) or sid not in known:
                preflight._error(errors, f"prior_decisions.{sid}", "unknown segment ID")
            if not isinstance(entry, dict) or not isinstance(entry.get("decision"), str) or entry["decision"] not in {"correct", "keep", "needs_review"}:
                preflight._error(errors, f"prior_decisions.{sid}", "must contain a valid decision")
    if errors:
        return _failure(errors)
    document = assembled["patch_document"]
    checked = preflight.preflight(transcript, bundle, document, layout=layout, cards=source_cards)
    if not checked["ok"]:
        return _failure(checked["errors"])
    try:
        _, evidence = preflight._bundle(bundle, errors)
        corrected, audit = review.apply_patches(copy.deepcopy(transcript), copy.deepcopy(document), evidence)
        decisions.update({entry["segment_id"]: entry for entry in audit})
        preview, records = reading.render(corrected, source_cards, decisions, copy.deepcopy(layout))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        preflight._error(errors, "preview", str(exc))
        return _failure(errors)
    return {"ok": True, "errors": [], "patch_document": document,
            "layout": layout, "preview": preview, "reading_map": records}
