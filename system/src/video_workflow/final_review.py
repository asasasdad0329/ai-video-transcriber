"""Stage all upstream work, seal one candidate, read it once, publish one batch.

Original ASR and all segment bookkeeping stay local. The model receives only
local edit results and a single readable candidate, never a source-JSON dump.
"""
from __future__ import annotations

import copy
import hashlib
import uuid

from . import preflight, reading, review


_EDIT_FIELDS = {"edits", "punctuation", "split_before", "join_before",
                "section_titles", "protected_terms", "card_before", "card_replacements"}
_PHASES = {"staging", "final_candidate_ready", "full_read_open", "review_complete"}


def _api():
    from . import cli
    return cli


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _layout_hash(run):
    api = _api()
    path = run / "reading_layout.json"
    return api._hash(path) if path.is_file() else None


_TASKS_FILE = "final_review_tasks.json"


def _task_id(api, explicit=None, draft=None):
    value = api._review_task_id(explicit)
    if not value and draft:
        value = draft.get("task_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a stable review task ID is required; use --task-id when the host has no session ID")
    return value.strip()


def _load_tasks(run, state):
    api = _api()
    path = run / _TASKS_FILE
    if not path.is_file():
        return {"version": 1, "run_id": state["run_id"], "tasks": []}
    ledger = api._read(path)
    if (ledger.get("version") != 1 or ledger.get("run_id") != state["run_id"]
            or not isinstance(ledger.get("tasks"), list)):
        raise ValueError("invalid final-review task ledger")
    return ledger


def _save_tasks(run, ledger):
    _api()._write(run / _TASKS_FILE, ledger)


def _find_task(ledger, task_id):
    matches = [item for item in ledger["tasks"] if item.get("task_id") == task_id]
    if len(matches) > 1:
        raise ValueError("duplicate final-review task records")
    return matches[0] if matches else None


def _draft_task_receipt(draft, task_id):
    receipts = draft.get("read_receipts", {})
    if not isinstance(receipts, dict):
        raise ValueError("invalid task reading receipts")
    receipt = receipts.get(task_id)
    if receipt is None and draft.get("task_id") == task_id:
        receipt = draft.get("read_receipt")
    return receipt


def _phase(draft):
    """Recover old drafts while keeping every new transition explicit."""
    phase = draft.get("phase")
    if phase is None:
        if draft.get("final_review_batch"):
            phase = "review_complete"
        elif draft.get("read_receipt") or draft.get("read_receipts"):
            phase = "full_read_open"
        else:
            phase = "staging"
    if phase not in _PHASES:
        raise ValueError("invalid final-review draft phase")
    return phase


def _focused_readiness(run, draft, task_id):
    """Require the broad local evidence pass unless the run already decided it."""
    api = _api()
    path = run / "focused_review_tasks.json"
    ledger = api._read(path) if path.is_file() else {"version": 1, "run_id": draft["run_id"], "tasks": {}}
    if (ledger.get("version") != 1 or ledger.get("run_id") != draft["run_id"]
            or not isinstance(ledger.get("tasks"), dict)):
        raise ValueError("invalid focused-review task ledger")
    if any(not isinstance(receipt, dict) for receipt in ledger["tasks"].values()):
        raise ValueError("invalid focused-review task receipt")
    expected = draft["basis"]["transcript_sha256"]
    triage_sha256 = api._hash(run / "triage.json")
    visuals_sha256 = api._json_hash(api._read(run / "visuals.json"))
    known_segments = {segment["id"] for segment in draft["base"]["segments"]}

    def reviewed_ids(receipt):
        ids = receipt.get("reviewed_segment_ids", draft["suspect_ids"])
        if (not isinstance(ids, list) or any(not isinstance(sid, str) for sid in ids)
                or len(ids) != len(set(ids)) or set(ids) - known_segments):
            raise ValueError("focused-review receipt has invalid reviewed segment IDs")
        return ids

    def valid(receipt):
        return (receipt.get("segment_ids") is None
                and receipt.get("transcript_sha256") == expected
                and receipt.get("triage_sha256") == triage_sha256
                and receipt.get("visuals_sha256") == visuals_sha256)

    receipt = ledger["tasks"].get(task_id)
    if receipt:
        if receipt.get("segment_ids") is not None:
            raise ValueError("the final candidate requires one whole-batch focused-review; a targeted receipt is not sufficient")
        if receipt.get("transcript_sha256") != expected:
            raise ValueError("focused-review receipt is stale for the candidate transcript")
        if receipt.get("triage_sha256") != triage_sha256:
            raise ValueError("focused-review receipt is stale for the current local checks")
        if receipt.get("visuals_sha256") != visuals_sha256:
            raise ValueError("focused-review receipt is stale for the current visual evidence")
        return {"mode": "current_task_batch", "recorded_at": receipt.get("recorded_at"),
                "scope": receipt.get("scope"), "domain": receipt.get("domain"),
                "reviewed_segment_ids": reviewed_ids(receipt),
                "result_sha256": receipt.get("result_sha256")}
    checkpoint = next((candidate for candidate in reversed(list(ledger["tasks"].values()))
                       if valid(candidate)), None)
    if checkpoint:
        return {"mode": "existing_focused_review_checkpoint",
                "recorded_at": checkpoint.get("recorded_at"),
                "scope": checkpoint.get("scope"), "domain": checkpoint.get("domain"),
                "reviewed_segment_ids": reviewed_ids(checkpoint),
                "result_sha256": checkpoint.get("result_sha256")}
    if all(sid in draft["prior_decisions"] for sid in draft["suspect_ids"]):
        return {"mode": "existing_run_decisions", "scope": None, "domain": None,
                "reviewed_segment_ids": list(draft["suspect_ids"]),
                "decided_count": len(draft["suspect_ids"])}
    if not draft["suspect_ids"]:
        return {"mode": "no_local_suspects", "scope": None, "domain": None,
                "reviewed_segment_ids": [], "decided_count": 0}
    raise ValueError("run the task's one whole-batch focused-review before sealing the final candidate")


def _domain_terms(draft, requested):
    try:
        from domains import KNOWN, load
    except (ImportError, ValueError):
        return None, []
    transcript, _ = _working(draft)
    source = "\n".join(s.get("text", "") for s in transcript["segments"])
    for card in draft["cards"]:
        source += "\n" + "\n".join(str(card.get(key, "")) for key in ("title", "verbatim_text", "structure"))
    if requested:
        if requested not in KNOWN:
            return None, []
        domain = load(requested)
    else:
        domain = next((candidate for candidate in (load(name) for name in KNOWN)
                       if candidate.detects(source)), None)
    return ((domain.name, sorted(domain.protected_tokens(source))) if domain else (None, []))


def _layout_boundary_errors(blocks):
    """A visual card between unfinished speech blocks is a late layout defect."""
    errors = []
    closing = "\"'」』）】》”’"
    for index, block in enumerate(blocks):
        if block.get("kind") != "visual":
            continue
        previous = next((item for item in reversed(blocks[:index]) if item.get("kind") == "speech"), None)
        following = next((item for item in blocks[index + 1:] if item.get("kind") == "speech"), None)
        if not previous or not following:
            continue
        ending = previous.get("text", "").rstrip().removesuffix("〔？〕").rstrip(closing)
        if not ending.endswith(("。", "？", "！", ".", "?", "!")):
            errors.append({"card_id": block.get("card_id"),
                           "previous_segment_id": previous["segment_ids"][-1],
                           "next_segment_id": following["segment_ids"][0]})
    return errors


def _normalization_receipt(draft):
    transcript, _ = _working(draft)
    terms = draft["layout"].get("protected_terms", [])
    source = [s["text"] for s in transcript["segments"]]
    source.extend(str(card.get(key, "")) for card in draft["cards"]
                  for key in ("title", "verbatim_text", "structure"))
    normalized = [reading.traditional(text, terms) for text in source]
    return {"config_sha256": draft["basis"]["normalization_sha256"],
            "protected_terms": list(terms),
            "source_item_count": len(source),
            "changed_item_count": sum(before != after for before, after in zip(source, normalized)),
            "normalized_sha256": _hash_text("\u0000".join(normalized))}


def _review_issues(data, draft):
    if not isinstance(data, dict) or "review_issues" not in data:
        raise ValueError("the final changes file must contain review_issues, which may be an empty list")
    issues = data["review_issues"]
    if not isinstance(issues, list):
        raise ValueError("review_issues must be a list")
    segment_ids = {s["id"] for s in draft["base"]["segments"]}
    card_ids = {c["id"] for c in draft["cards"]}
    allowed = {"id", "kind", "summary", "resolution", "segment_ids", "card_ids"}
    normalized = []
    seen = set()
    for number, issue in enumerate(issues, 1):
        if not isinstance(issue, dict) or set(issue) - allowed:
            raise ValueError(f"review_issues[{number}] has invalid fields")
        issue_id = issue.get("id", f"issue-{number:03d}")
        normalized_id = issue_id.strip() if isinstance(issue_id, str) else None
        if not normalized_id or normalized_id in seen:
            raise ValueError("review issue IDs must be unique nonempty strings")
        seen.add(normalized_id)
        kind = issue.get("kind")
        if kind not in {"text", "punctuation", "paragraph", "layout", "normalization", "visual"}:
            raise ValueError(f"review_issues[{number}].kind is invalid")
        summary, resolution = issue.get("summary"), issue.get("resolution")
        if not isinstance(summary, str) or not summary.strip() or not isinstance(resolution, str) or not resolution.strip():
            raise ValueError("each review issue needs a nonempty summary and resolution")
        segments = issue.get("segment_ids", [])
        cards = issue.get("card_ids", [])
        if (not isinstance(segments, list) or any(not isinstance(value, str) for value in segments)
                or len(segments) != len(set(segments)) or set(segments) - segment_ids
                or not isinstance(cards, list) or any(not isinstance(value, str) for value in cards)
                or len(cards) != len(set(cards)) or set(cards) - card_ids):
            raise ValueError("review issue references unknown segment or card IDs")
        normalized.append({"id": normalized_id, "kind": kind, "summary": summary.strip(),
                           "resolution": resolution.strip(), "segment_ids": segments, "card_ids": cards})
    return normalized, {key: copy.deepcopy(value) for key, value in data.items()
                        if key != "review_issues"}


def _new_task(api, task_id, directory, draft, *, imported=False,
              accepted_visual_uncertainties=0):
    item = {"task_id": task_id, "status": "active", "created_at": api._now(),
            "draft_id": draft["id"], "draft_dir": api._portable(directory),
            "basis": draft["basis"], "read_receipt": _draft_task_receipt(draft, task_id),
            "accepted_visual_uncertainties": accepted_visual_uncertainties,
            "replaced_unread_drafts": []}
    if imported:
        item["imported_existing_draft"] = True
        checkpoint = draft.get("read_receipt")
        if checkpoint and not item["read_receipt"]:
            item["checkpoint_read_receipt"] = checkpoint
            item["checkpoint_task_id"] = checkpoint.get("task_id") or draft.get("task_id")
    return item


def _task_matches(api, item, directory, draft):
    return (item.get("draft_id") == draft["id"]
            and api._root_path(item.get("draft_dir")) == directory
            and item.get("basis") == draft["basis"])


def _bind_task(run, state, directory, draft, task_id):
    """Bind this session to one draft without affecting other sessions."""
    api = _api()
    ledger = _load_tasks(run, state)
    item = _find_task(ledger, task_id)
    if item is None:
        item = _new_task(api, task_id, directory, draft, imported=True)
        ledger["tasks"].append(item)
        _save_tasks(run, ledger)
    if not _task_matches(api, item, directory, draft):
        raise ValueError("this session is already bound to another final-review draft for the run")
    return ledger, item


def _mark_task_published(run, state, directory, draft, task_id, result):
    api = _api()
    ledger, item = _bind_task(run, state, directory, draft, task_id)
    item.update(status="published", published_at=api._now(),
                review_commit={"primary_transcript": result.get("primary_transcript"),
                               "status": result.get("status")})
    _save_tasks(run, ledger)


def _basis(run, state):
    api = _api()
    transcript, bundle, cards = api._review_plan_basis(run, state)
    return transcript, bundle, cards, {
        "transcript_sha256": api._json_hash(transcript),
        "record_sha256": api._json_hash(api._record(run)),
        "visual_review_sha256": bundle["visual_review_sha256"],
        "layout_sha256": _layout_hash(run),
        "normalization_sha256": api._hash(reading.TAIWAN_TERMS)}


def _load(directory):
    api = _api()
    directory = api._root_path(directory)
    draft = api._read(directory / "draft.json")
    if draft.get("version") != 1:
        raise ValueError("unknown final-review draft format")
    phase = _phase(draft)
    draft["phase"] = phase
    content, _ = _render(draft)
    if _hash_text(content) != draft.get("candidate_sha256"):
        raise ValueError("draft content changed outside edit-draft; reading receipt cannot be reused")
    readable = directory / "candidate.md"
    if readable.is_file():
        readable.unlink()
    map_path = directory / "candidate-map.json"
    if map_path.is_file():
        mapped = api._read(map_path)
        if any(isinstance(block, dict) and "text" in block for block in mapped.get("blocks", [])):
            api._write(map_path, {"blocks": [{key: copy.deepcopy(value) for key, value in block.items()
                                               if key != "text"}
                                              for block in mapped.get("blocks", [])]})
    receipts = list(draft.get("read_receipts", {}).values())
    if draft.get("read_receipt") and draft["read_receipt"] not in receipts:
        receipts.append(draft["read_receipt"])
    for receipt in receipts:
        if (not 0 <= receipt["revision"] <= draft["revision"]
                or (receipt["revision"] == draft["revision"]
                    and receipt["candidate_sha256"] != draft["candidate_sha256"])):
            raise ValueError("reading receipt does not match draft revision")
    seal = draft.get("seal_receipt")
    if seal:
        if (not isinstance(seal, dict) or not isinstance(seal.get("revision"), int)
                or not 0 <= seal["revision"] <= draft["revision"]):
            raise ValueError("invalid final-candidate seal")
        if (seal["revision"] == draft["revision"]
                and seal.get("candidate_sha256") != draft["candidate_sha256"]):
            raise ValueError("final-candidate seal does not match the draft")
    elif phase == "final_candidate_ready":
        raise ValueError("final candidate is missing its readiness seal")
    return directory, draft


def _live(draft):
    api = _api()
    run, state = api._load_run(draft["run_dir"])
    _, bundle, _, basis = _basis(run, state)
    if state["run_id"] != draft["run_id"] or basis != draft["basis"]:
        raise ValueError("draft source changed; do not apply this candidate over newer work")
    return run, state, bundle


def _working(draft):
    transcript = copy.deepcopy(draft["base"])
    decisions = copy.deepcopy(draft["prior_decisions"])
    for segment in transcript["segments"]:
        item = draft["decisions"].get(segment["id"])
        if item:
            segment["text"] = item["new_text"]
            if segment["text"] != next(s["text"] for s in draft["base"]["segments"] if s["id"] == segment["id"]):
                segment["token_alignment_matches_text"] = False
            decisions[segment["id"]] = item
    return transcript, decisions


def _render(draft):
    transcript, decisions = _working(draft)
    content, blocks = reading.render(transcript, draft["cards"], decisions, draft["layout"])
    return content, blocks


def _save(directory, draft):
    api = _api()
    content, blocks = _render(draft)
    draft["candidate_sha256"] = _hash_text(content)
    # Do not persist a readable candidate that could bypass read-draft's one-time
    # receipt. draft.json stays local-only authority; the map exposes IDs only.
    readable = directory / "candidate.md"
    if readable.is_file():
        readable.unlink()
    mapped = [{key: copy.deepcopy(value) for key, value in block.items() if key != "text"}
              for block in blocks]
    api._write(directory / "candidate-map.json", {"blocks": mapped})
    api._write(directory / "draft.json", draft)


def start(run_dir, output_dir, changes_path=None, layout_path=None, *,
          accept_visual_uncertainties=False, task_id=None):
    api = _api()
    task_id = _task_id(api, task_id)
    with api._lock():
        run, state = api._load_run(run_dir)
        base, bundle, cards, basis = _basis(run, state)
        if not basis["visual_review_sha256"]:
            raise ValueError("finish visual cards before building the final reading candidate")
        directory = api._root_path(output_dir)
        if directory.is_relative_to(run) or directory.is_relative_to((api.ROOT / api.locations.RUNS).resolve()):
            raise ValueError("draft directory must be outside transcription runs")
        ledger = _load_tasks(run, state)
        item = _find_task(ledger, task_id)
        replace_unread = False
        if item:
            if item.get("status") == "published":
                raise ValueError("this session already completed final review for the run; a new session may rerun downstream work")
            existing = api._root_path(item["draft_dir"])
            if item.get("basis") == basis and (existing / "draft.json").is_file():
                _, active = _load(existing)
                if not _task_matches(api, item, existing, active):
                    raise ValueError("final-review task record does not match its draft")
                if changes_path or layout_path:
                    if _phase(active) == "staging":
                        raise ValueError(f"this session already has a staging draft at {api._portable(existing)}; continue it with edit-draft")
                    raise ValueError("the final candidate is sealed; changes or a new layout cannot rebuild it or unlock another full read")
                return {"ok": True, "reused": True, "task_id": task_id,
                        "draft": api._portable(existing), "revision": active["revision"],
                        "staged_decisions": len(active["decisions"]),
                        "phase": _phase(active), "applied": False,
                        "next": ("Continue staging, then seal-draft." if _phase(active) == "staging"
                                 else "Continue the recorded phase; do not create or fully read another candidate.")}
            if item.get("read_receipt") or item.get("finish_attempted_at"):
                raise ValueError("this session already read or submitted its final candidate; rerun downstream work in a new session")
            if item.get("readiness_receipt"):
                raise ValueError("this session already sealed its final candidate; source changes require a separately authorized new task")
            if existing.is_dir():
                directory = existing
                replace_unread = True
        uncertainty_count = state.get("visual_review", {}).get("uncertainty_count", 0) or 0
        if uncertainty_count and not accept_visual_uncertainties:
            raise ValueError(f"visual review still has {uncertainty_count} uncertainty items; resolve readable tables/charts first, or accept them explicitly when the source is genuinely unreadable")
        if item is None and directory.exists():
            suffix = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
            directory = directory.with_name(directory.name + "__" + suffix)
        layout_source = api._root_path(layout_path) if layout_path else run / "reading_layout.json"
        layout = (api._read(layout_source) if layout_source.is_file()
                  else reading.default_layout(base["segments"], cards, api._video_title(state)))
        draft = {"version": 1, "id": uuid.uuid4().hex, "task_id": task_id,
                  "run_id": state["run_id"], "run_dir": str(run), "basis": basis,
                  "base": base, "cards": cards,
                  "prior_decisions": api._record(run).get("decisions", {}), "decisions": {},
                  "layout": layout, "revision": 0, "read_receipt": None,
                  "read_receipts": {}, "history": [], "phase": "staging",
                  "seal_receipt": None, "final_review_batch": None,
                  "suspect_ids": [s["segment_id"] for s in api._read(run / "triage.json")["suspects"]]}
        if changes_path:
            _edit_data(draft, api._read(api._root_path(changes_path)), bundle["evidence_ids"])
        _render(draft)
        if not replace_unread:
            directory.mkdir(parents=True, exist_ok=False)
        _save(directory, draft)
        if item:
            item.setdefault("replaced_unread_drafts", []).append({
                "draft_id": item["draft_id"], "draft_dir": item["draft_dir"],
                "basis": item["basis"], "replaced_at": api._now()})
            item.update(draft_id=draft["id"], draft_dir=api._portable(directory),
                        basis=basis, status="active", accepted_visual_uncertainties=(
                            uncertainty_count if accept_visual_uncertainties else 0))
            item.pop("readiness_receipt", None)
            item.pop("checkpoint_read_receipt", None)
            item.pop("checkpoint_task_id", None)
        else:
            item = _new_task(api, task_id, directory, draft,
                             accepted_visual_uncertainties=(
                                 uncertainty_count if accept_visual_uncertainties else 0))
            ledger["tasks"].append(item)
        _save_tasks(run, ledger)
        return {"ok": True, "task_id": task_id, "draft": api._portable(directory),
                "revision": draft["revision"], "staged_decisions": len(draft["decisions"]),
                "phase": draft["phase"], "applied": False,
                "next": "Finish all local, visual, layout, and normalization work, then seal-draft."}


def _edit_data(draft, data, allowed):
    if not isinstance(data, dict) or set(data) - _EDIT_FIELDS:
        raise ValueError("changes supports edits, punctuation, split_before, join_before, section_titles, protected_terms, card_before, card_replacements")
    before, _ = _working(draft)
    current = {s["id"]: s["text"] for s in before["segments"]}
    original = {s["id"]: s["text"] for s in draft["base"]["segments"]}
    entries = data.get("edits", [])
    if not isinstance(entries, list):
        raise ValueError("edits must be a list")
    seen, changed = set(), []
    for item in entries:
        if not isinstance(item, dict) or set(item) - {"segment_id", "old_text", "new_text", "decision", "reason", "evidence_ids"}:
            raise ValueError("invalid local edit fields")
        sid = item.get("segment_id")
        if not isinstance(sid, str) or sid not in current or sid in seen:
            raise ValueError("unknown or duplicate segment ID")
        seen.add(sid)
        if "old_text" in item and item["old_text"] != current[sid]:
            raise ValueError("local edit no longer matches: " + sid)
        decision = item.get("decision", "correct")
        if decision not in ("correct", "keep", "needs_review"):
            raise ValueError("unknown decision")
        new = item.get("new_text", current[sid])
        if not isinstance(new, str) or not new.strip():
            raise ValueError("replacement must be nonempty; resolve cross-segment repeats explicitly")
        if decision != "correct" and new != current[sid]:
            raise ValueError("keep/needs_review cannot change text")
        if decision == "needs_review" and new != original[sid]:
            raise ValueError("uncertain text cannot be staged as a confirmed correction")
        reason = item.get("reason", "依候選稿局部上下文校正；未聲稱聽音。")
        refs = item.get("evidence_ids", ["text:" + sid])
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be nonempty")
        if not isinstance(refs, list) or any(not isinstance(r, str) or r not in allowed for r in refs):
            raise ValueError("unknown evidence ID")
        # Repeated edits collapse against the original snapshot, not intermediate versions.
        final_decision = "correct" if new != original[sid] else ("needs_review" if decision == "needs_review" else "keep")
        if final_decision == "correct" and not refs:
            raise ValueError("correction needs evidence")
        draft["decisions"][sid] = {"segment_id": sid, "new_text": new, "decision": final_decision,
                                    "reason": reason, "evidence_ids": list(dict.fromkeys(refs))}
        if new != current[sid] or decision == "needs_review":
            changed.append({"segment_id": sid, "before": current[sid], "after": new})
    punctuation = data.get("punctuation", {})
    if not isinstance(punctuation, dict):
        raise ValueError("punctuation must map segment ID to ending; empty string removes it")
    for sid, ending in punctuation.items():
        if sid not in current or not isinstance(ending, str) or not set(ending) <= reading._PUNCTUATION:
            raise ValueError("invalid punctuation update")
        for section in draft["layout"]["sections"]:
            for block in section["blocks"]:
                if sid in block.get("segment_ids", []):
                    old = block.setdefault("punctuation", {}).get(sid, "")
                    if ending:
                        block["punctuation"][sid] = ending
                    else:
                        block["punctuation"].pop(sid, None)
                    if old != ending:
                        changed.append({"segment_id": sid, "ending_before": old, "ending_after": ending})
    for operation in ("split_before", "join_before"):
        ids = data.get(operation, [])
        if not isinstance(ids, list) or any(s not in current for s in ids):
            raise ValueError("invalid paragraph boundary IDs")
        for sid in ids:
            found = False
            for section in draft["layout"]["sections"]:
                blocks = section["blocks"]
                for i, block in enumerate(blocks):
                    if sid not in block.get("segment_ids", []):
                        continue
                    n = block["segment_ids"].index(sid)
                    if operation == "split_before" and n:
                        left, right = block["segment_ids"][:n], block["segment_ids"][n:]
                        blocks[i:i+1] = [{"segment_ids": part, "punctuation": {k: v for k, v in block.get("punctuation", {}).items() if k in part}} for part in (left, right)]
                    elif operation == "join_before":
                        if n or i == 0 or "segment_ids" not in blocks[i-1]:
                            raise ValueError("join must target a paragraph start after speech in the same section")
                        blocks[i-1]["segment_ids"].extend(block["segment_ids"])
                        blocks[i-1].setdefault("punctuation", {}).update(block.get("punctuation", {}))
                        del blocks[i]
                    found = True
                    break
                if found:
                    break
            changed.append({operation: sid})
    replacements = data.get("card_replacements", {})
    known_cards = {card["id"] for card in draft["cards"]}
    if not isinstance(replacements, dict) or set(replacements) - known_cards:
        raise ValueError("card_replacements must map known card IDs to literal replacements")
    card_rules = []
    for cid, rules in replacements.items():
        if (not isinstance(rules, list) or not rules
                or any(not isinstance(rule, dict) or set(rule) != {"find", "replace"}
                       or any(not isinstance(rule[key], str) or not rule[key].strip()
                              for key in ("find", "replace")) for rule in rules)):
            raise ValueError("invalid card text replacement")
        blocks = [block for section in draft["layout"]["sections"]
                  for block in section["blocks"] if block.get("card_id") == cid]
        if len(blocks) != 1:
            raise ValueError("card replacement must match exactly one visual block")
        # Checked once this batch's protected_terms are in place; card_before
        # moves the same block object, so the reference stays valid.
        card_rules.append((cid, blocks[0], rules))
    placements = data.get("card_before", {})
    if (not isinstance(placements, dict) or set(placements) - known_cards
            or any(target is not None and (not isinstance(target, str) or target not in current)
                   for target in placements.values())):
        raise ValueError("card_before must map known card IDs to a following segment ID or null")
    for card_id, target in placements.items():
        matches = []
        for section in draft["layout"]["sections"]:
            for index, block in enumerate(section["blocks"]):
                if block.get("card_id") == card_id:
                    matches.append((section, index, block))
        if len(matches) != 1:
            raise ValueError("card placement must match exactly one visual block")
        source_section, source_index, card_block = matches[0]
        del source_section["blocks"][source_index]
        draft["layout"]["sections"] = [section for section in draft["layout"]["sections"]
                                         if section["blocks"]]
        if target is None:
            draft["layout"]["sections"][-1]["blocks"].append(card_block)
        else:
            inserted = False
            for section in draft["layout"]["sections"]:
                for index, block in enumerate(section["blocks"]):
                    ids = block.get("segment_ids", [])
                    if target not in ids:
                        continue
                    position = ids.index(target)
                    if position:
                        left, right = ids[:position], ids[position:]
                        punctuation = block.get("punctuation", {})
                        replacement = [
                            {"segment_ids": left,
                             "punctuation": {key: value for key, value in punctuation.items() if key in left}},
                            card_block,
                            {"segment_ids": right,
                             "punctuation": {key: value for key, value in punctuation.items() if key in right}},
                        ]
                        section["blocks"][index:index + 1] = replacement
                    else:
                        section["blocks"].insert(index, card_block)
                    inserted = True
                    break
                if inserted:
                    break
            if not inserted:
                raise ValueError("card_before target is not present in the current layout")
        changed.append({"card_id": card_id, "before_segment_id": target})
    titles = data.get("section_titles", {})
    if not isinstance(titles, dict):
        raise ValueError("section_titles must map old title to new title")
    for old, new in titles.items():
        matches = [s for s in draft["layout"]["sections"] if s["title"] == old]
        if len(matches) != 1 or not isinstance(new, str) or not new.strip():
            raise ValueError("section title must match exactly once")
        matches[0]["title"] = new
        changed.append({"title_before": old, "title_after": new})
    protected = data.get("protected_terms", [])
    if (not isinstance(protected, list)
            or any(not isinstance(term, str) or not term.strip() for term in protected)):
        raise ValueError("protected_terms must be a list of nonempty strings")
    terms = draft["layout"].setdefault("protected_terms", [])
    if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
        raise ValueError("draft layout has invalid protected_terms")
    for term in protected:
        if term not in terms:
            terms.append(term)
            changed.append({"protected_term_added": term})
    cards = {card["id"]: card for card in draft["cards"]}
    for cid, block, rules in card_rules:
        # A rule must hit the text the reader saw and actually change it; one
        # that normalization would turn back into the same text is refused.
        body = reading.card_text(cards[cid], block, terms)
        for rule in rules:
            body = reading.replace_card_text(body, rule, cid, terms)
        block.setdefault("text_replacements", []).extend(copy.deepcopy(rules))
        changed.extend({"card_id": cid, "text_replacement": copy.deepcopy(rule)} for rule in rules)
    _render(draft)  # Local shape check, not a formal preflight or model review.
    draft["revision"] += 1
    draft["history"].append({"revision": draft["revision"], "changes": changed})
    return changed


def edit(directory, changes_path=None, *, find=None, replacement=None, segment_id=None,
         after=None, ending=None, task_id=None):
    api = _api()
    with api._lock():
        directory, draft = _load(directory)
        _ensure_open(draft)
        if _phase(draft) != "staging":
            raise ValueError("the final candidate is sealed; do not edit it piecemeal or start another full review")
        run, state, bundle = _live(draft)
        task_id = _task_id(api, task_id, draft)
        _bind_task(run, state, directory, draft, task_id)
        _, before_blocks = _render(draft)
        if changes_path:
            data = api._read(api._root_path(changes_path))
        elif after is not None:
            data = {"punctuation": {after: ending}}
        else:
            if not isinstance(find, str) or not find or not isinstance(replacement, str):
                raise ValueError("supply --find and --replace, or a changes file")
            transcript, _ = _working(draft)
            terms = draft["layout"].get("protected_terms", [])
            matches = []
            for segment in transcript["segments"]:
                if segment_id and segment["id"] != segment_id:
                    continue
                raw = segment["text"]
                display = reading.traditional(raw, terms).strip()
                text = raw if find in raw else display
                if find in text:
                    matches.append((segment, text))
            if len(matches) != 1 or matches[0][1].count(find) != 1:
                return {"ok": False, "error": "replacement must match once; select a segment or submit explicit local edits",
                        "matches": [{"segment_id": s["id"], "text": text} for s, text in matches[:8]],
                        "match_count": len(matches), "applied": False}
            segment, text = matches[0]
            data = {"edits": [{"segment_id": segment["id"], "old_text": segment["text"],
                               "new_text": text.replace(find, replacement, 1)}]}
        changed = _edit_data(draft, data, bundle["evidence_ids"])
        _save(directory, draft)
        _, after_blocks = _render(draft)
        local_render = [{"first": b["segment_ids"][0], "last": b["segment_ids"][-1], "text": b["text"]}
                        for b in after_blocks if b["kind"] == "speech" and b not in before_blocks] if draft["read_receipt"] else []
        return {"ok": True, "revision": draft["revision"], "changes": changed, "applied": False,
                "rendered_changes": local_render,
                "next": "Continue staging front-loaded changes, then seal-draft once."}


def seal(directory, task_id=None, accept_visual_uncertainties=False):
    """Freeze the only full-reading candidate after every upstream gate passes."""
    api = _api()
    with api._lock():
        directory, draft = _load(directory)
        _ensure_open(draft)
        run, state, _ = _live(draft)
        task_id = _task_id(api, task_id, draft)
        ledger, item = _bind_task(run, state, directory, draft, task_id)
        phase = _phase(draft)
        if phase == "final_candidate_ready":
            return {"ok": True, "already_sealed": True, "task_id": task_id,
                    "draft": api._portable(directory), "revision": draft["revision"],
                    "readiness": draft["seal_receipt"].get("checks"),
                    "next": "Use read-draft once."}
        if phase != "staging" or item.get("read_receipt") or item.get("finish_attempted_at"):
            raise ValueError("this task already passed the final-candidate boundary; it cannot seal or read another candidate")

        focus = _focused_readiness(run, draft, task_id)
        required_decisions = list(dict.fromkeys([*draft["suspect_ids"],
                                                 *focus.get("reviewed_segment_ids", [])]))
        missing = [sid for sid in required_decisions
                   if sid not in draft["prior_decisions"] and sid not in draft["decisions"]]
        if missing:
            shown = ", ".join(missing[:12])
            suffix = " ..." if len(missing) > 12 else ""
            raise ValueError(f"{len(missing)} local suspect decisions are still unstaged: {shown}{suffix}")

        uncertainty_count = state.get("visual_review", {}).get("uncertainty_count", 0) or 0
        accepted = item.get("accepted_visual_uncertainties", 0) or 0
        if uncertainty_count and accept_visual_uncertainties:
            accepted = uncertainty_count
            item["accepted_visual_uncertainties"] = accepted
        if uncertainty_count and accepted != uncertainty_count:
            raise ValueError("visual uncertainty changed or was not explicitly accepted before final-candidate sealing")

        domain_name, automatic_terms = _domain_terms(draft, focus.get("domain"))
        existing_terms = draft["layout"].setdefault("protected_terms", [])
        added_terms = [term for term in automatic_terms if term not in existing_terms]
        if added_terms:
            _edit_data(draft, {"protected_terms": added_terms}, set())

        content, blocks = _render(draft)
        boundary_errors = _layout_boundary_errors(blocks)
        if boundary_errors:
            detail = ", ".join(f"{e['previous_segment_id']} -> {e['card_id']} -> {e['next_segment_id']}"
                               for e in boundary_errors[:8])
            raise ValueError("visual cards interrupt unfinished speech; fix these layout boundaries before sealing: " + detail)
        speech_blocks = [block for block in blocks if block.get("kind") == "speech"]
        visual_blocks = [block for block in blocks if block.get("kind") == "visual"]
        focus_ids = focus.get("reviewed_segment_ids", [])
        focus_summary = {key: value for key, value in focus.items()
                         if key != "reviewed_segment_ids"}
        focus_summary.update(reviewed_segment_count=len(focus_ids),
                             reviewed_segment_ids_sha256=api._json_hash(focus_ids))
        checks = {
            "focused_review": focus_summary,
            "local_decisions": {"suspect_count": len(required_decisions),
                                "all_decided": True,
                                "staged_decision_count": len(draft["decisions"])},
            "visual_review": {"sha256": draft["basis"]["visual_review_sha256"],
                              "card_count": len(draft["cards"]),
                              "uncertainty_count": uncertainty_count,
                              "accepted_unreadable_count": accepted},
            "layout": {"speech_block_count": len(speech_blocks),
                       "visual_block_count": len(visual_blocks),
                       "layout_sha256": api._json_hash(draft["layout"]),
                       "all_source_items_covered": True,
                       "unfinished_visual_boundaries": 0},
            "normalization": {**_normalization_receipt(draft),
                              "domain": domain_name,
                              "domain_auto_detected": bool(domain_name and not focus.get("domain"))},
        }
        receipt = {"sealed_at": api._now(), "task_id": task_id,
                   "revision": draft["revision"], "candidate_sha256": _hash_text(content),
                   "basis": copy.deepcopy(draft["basis"]), "checks": checks}
        draft["seal_receipt"] = receipt
        draft["phase"] = "final_candidate_ready"
        _save(directory, draft)
        item.update(status="final_candidate_ready", readiness_receipt=receipt)
        _save_tasks(run, ledger)
        return {"ok": True, "task_id": task_id, "draft": api._portable(directory),
                "revision": draft["revision"], "phase": draft["phase"],
                "readiness": checks, "applied": False,
                "next": "Use read-draft once. After reading, submit every remaining issue in one finish-review changes file."}


def _ensure_open(draft):
    api = _api()
    receipt = api._root_path(draft["run_dir"]) / "review_commits" / (draft["id"] + ".json")
    if receipt.is_file():
        raise ValueError("draft already published; no further automatic editing or rereading")


def read_once(directory, task_id=None):
    api = _api()
    with api._lock():
        directory, draft = _load(directory)
        _ensure_open(draft)
        run, state, _ = _live(draft)
        task_id = _task_id(api, task_id, draft)
        ledger, item = _bind_task(run, state, directory, draft, task_id)
        receipt = item.get("read_receipt") or _draft_task_receipt(draft, task_id)
        if receipt:
            return {"ok": True, "already_shown": True, "revision": draft["revision"],
                    "task_id": task_id,
                    "next": "The full candidate is never returned again. Submit the one complete issue batch with finish-review."}
        if _phase(draft) != "final_candidate_ready":
            raise ValueError("seal-draft must complete every upstream readiness check before the one full read")
        seal = draft.get("seal_receipt")
        if (not seal or seal.get("revision") != draft["revision"]
                or seal.get("candidate_sha256") != draft.get("candidate_sha256")):
            raise ValueError("the final candidate changed after sealing")
        content, blocks = _render(draft)
        # Display paragraph-level source ranges, never raw ASR token arrays.
        paragraphs = [{"paragraph": i+1, "first": b["segment_ids"][0], "last": b["segment_ids"][-1]}
                      for i, b in enumerate(blocks) if b["kind"] == "speech"]
        receipt = {"revision": draft["revision"], "candidate_sha256": _hash_text(content),
                   "shown_at": api._now(), "semantic_review_verified": False,
                   "task_id": task_id}
        draft["read_receipt"] = receipt
        draft.setdefault("read_receipts", {})[task_id] = receipt
        draft["phase"] = "full_read_open"
        api._write(directory / "draft.json", draft)
        item.update(status="full_read_open", read_receipt=receipt)
        _save_tasks(run, ledger)
        return {"ok": True, "task_id": task_id, "revision": draft["revision"], "candidate": content,
                "paragraph_ranges": paragraphs,
                "next": "Mark every remaining issue from this single reading, then submit them together with finish-review --changes."}


def excerpt(directory, segment_ids, task_id=None):
    api = _api()
    with api._lock():
        directory, draft = _load(directory)
        if not segment_ids:
            raise ValueError("select at least one segment; full source dumps are not supported")
        transcript, _ = _working(draft)
        known = {s["id"] for s in transcript["segments"]}
        if set(segment_ids) - known or len(segment_ids) != len(set(segment_ids)):
            raise ValueError("choose unique known segment IDs")
        run, state, _ = _live(draft)
        task_id = _task_id(api, task_id, draft)
        ledger, item = _bind_task(run, state, directory, draft, task_id)
        previous = item.get("excerpted_segment_ids", [])
        combined = list(dict.fromkeys([*previous, *segment_ids]))
        if len(combined) > 12:
            raise ValueError("draft-excerpt is limited to 12 distinct segments for the entire task; it cannot reconstruct a second full view")
        item["excerpted_segment_ids"] = combined
        _save_tasks(run, ledger)
        return {"revision": draft["revision"], "task_id": task_id,
                "task_excerpt_count": len(combined), "segments": [
                    {"id": s["id"], "text": s["text"]}
                    for s in transcript["segments"] if s["id"] in segment_ids]}


def finish(directory, reviewed=False, revision=None, task_id=None, changes_path=None):
    api = _api()
    with api._lock():
        directory, draft = _load(directory)
        run, state = api._load_run(draft["run_dir"], allow_pending_commit=True)
        task_id = _task_id(api, task_id, draft)
        receipt_path = run / "review_commits" / (draft["id"] + ".json")
        if receipt_path.is_file():
            pending = run / "review_commit_pending.json"
            if pending.is_file() and api._read(pending).get("draft_id") == draft["id"]:
                pending.unlink()
            return {**api._read(receipt_path), "already_committed": True}
        ledger, item = _bind_task(run, state, directory, draft, task_id)
        pending_path = run / "review_commit_pending.json"
        if pending_path.is_file():
            pending = api._read(pending_path)
            if pending["draft_id"] != draft["id"]:
                raise ValueError("a different final review must finish recovery first")
            result = _publish(run, state, pending, receipt_path)
            _mark_task_published(run, state, directory, draft, task_id, result)
            return result
        task_receipt = item.get("read_receipt") or _draft_task_receipt(draft, task_id)
        checkpoint_receipt = item.get("checkpoint_read_receipt")
        review_receipt = task_receipt or checkpoint_receipt
        if not reviewed or revision != draft["revision"] or not review_receipt:
            raise ValueError("read the candidate once or resume a reviewed checkpoint, then explicitly attest --reviewed with the current --revision")
        if _phase(draft) != "full_read_open":
            raise ValueError("finish-review requires the sealed candidate's single full read; piecemeal edits cannot replace it")
        seal = draft.get("seal_receipt")
        if (seal and (review_receipt.get("revision") != seal.get("revision")
                      or review_receipt.get("candidate_sha256") != seal.get("candidate_sha256"))):
            raise ValueError("the full-read receipt does not match the sealed final candidate")
        if not changes_path:
            raise ValueError("finish-review requires one --changes file containing review_issues and every remaining correction")
        _, state, bundle = _live(draft)
        payload = api._read(api._root_path(changes_path))
        issues, edit_data = _review_issues(payload, draft)
        if any(bool(value) for value in edit_data.values()) and not issues:
            raise ValueError("nonempty final corrections require corresponding review_issues")
        working = copy.deepcopy(draft)
        batch_changes = []
        if edit_data:
            batch_changes = _edit_data(working, edit_data, bundle["evidence_ids"])
        decisions = copy.deepcopy(working["decisions"])
        original = {s["id"]: s["text"] for s in working["base"]["segments"]}
        missing = [sid for sid in working["suspect_ids"]
                   if sid not in decisions and sid not in working["prior_decisions"]]
        if missing:
            raise ValueError("the sealed candidate lost local decisions; rebuild is forbidden and publication stopped")
        patches = [{**decisions[s["id"]], "old_text": s["text"]}
                   for s in working["base"]["segments"] if s["id"] in decisions]
        content, _ = _render(working)
        batch = {"completed_at": api._now(), "task_id": task_id,
                 "input_revision": revision,
                 "input_candidate_sha256": review_receipt["candidate_sha256"],
                 "review_issues": issues, "issue_count": len(issues),
                 "changes_sha256": api._json_hash(payload),
                 "change_count": len(batch_changes),
                 "final_revision": working["revision"],
                 "final_candidate_sha256": _hash_text(content)}
        working["final_review_batch"] = batch
        working["phase"] = "review_complete"
        context = {"transcript_sha256": bundle["transcript_sha256"], "visual_review_sha256": bundle["visual_review_sha256"],
                   "reviewed_segment_ids": list(original), "reviewed_card_ids": [c["id"] for c in draft["cards"]],
                   "method": ("sealed_candidate_once_then_single_issue_batch" if task_receipt
                              else "resumed_sealed_candidate_checkpoint_then_single_issue_batch"),
                   "read_receipt": review_receipt, "review_task_id": task_id,
                   "readiness_receipt": seal,
                   "final_review_batch": batch,
                   "final_candidate_sha256": _hash_text(content),
                   "reviewed_revision": revision,
                   "final_revision": working["revision"], "audio_verified": False}
        if checkpoint_receipt and not task_receipt:
            context["checkpoint_task_id"] = item.get("checkpoint_task_id")
        document = {"run_id": working["run_id"], "transcript_sha256": bundle["transcript_sha256"],
                    "patches": patches, "context_review": context}
        if item.get("finish_attempted_at"):
            raise ValueError("this session already used its one final preflight; a new session may rerun the downstream finish from this checkpoint")
        item["finish_attempted_at"] = api._now()
        item["finish_revision"] = revision
        item["final_review_batch_sha256"] = batch["changes_sha256"]
        _save_tasks(run, ledger)
        checked = preflight.preflight(working["base"], bundle, document, working["layout"], working["cards"])
        if not checked["ok"]:
            item["finish_result"] = {"ok": False, "error_count": len(checked.get("errors", []))}
            _save_tasks(run, ledger)
            return {**checked, "applied": False,
                    "next": "Keep the sealed reading checkpoint. Fix only these machine errors in a new session without another full read."}
        corrected, changes = review.apply_patches(working["base"], document, set(bundle["evidence_ids"]))
        record = copy.deepcopy(api._record(run))
        patch_hash = api._json_hash(document)
        for change in changes:
            change.update(patch_sha256=patch_hash, recorded_at=api._now())
            record.setdefault("decisions", {})[change["segment_id"]] = change
        record.setdefault("audit", []).extend(changes)
        record.setdefault("applied_documents", []).append(patch_hash)
        record.update(run_id=draft["run_id"], transcript=corrected)
        record["context_review"] = {**context, "input_transcript_sha256": context["transcript_sha256"],
                                    "transcript_sha256": api._json_hash(corrected), "recorded_at": api._now(),
                                    "patch_sha256": patch_hash}
        pending = {"draft_id": working["id"], "run_id": working["run_id"],
                    "review_task_id": task_id, "record": record,
                    "document": document, "patch_sha256": patch_hash, "layout": working["layout"],
                    "candidate_sha256": _hash_text(content), "normalization_sha256": working["basis"]["normalization_sha256"],
                    "visual_review_sha256": working["basis"]["visual_review_sha256"],
                    "original_record_sha256": working["basis"]["record_sha256"],
                    "original_layout_sha256": working["basis"]["layout_sha256"],
                    "patch_count": len(patches), "corrections": sum(c["applied"] for c in changes)}
        api._write(pending_path, pending)
        _save(directory, working)
        result = _publish(run, state, pending, receipt_path)
        _mark_task_published(run, state, directory, working, task_id, result)
        return result


def _publish(run, state, pending, receipt_path):
    """Recoverable single publication. Retry reuses the same patch and audit."""
    api = _api()
    if (state["run_id"] != pending["run_id"]
            or api._hash(reading.TAIWAN_TERMS) != pending["normalization_sha256"]
            or api._context_basis(run)["visual_review_sha256"] != pending["visual_review_sha256"]):
        raise ValueError("publication inputs changed during recovery")
    record = api._record(run)
    if api._json_hash(record) not in (pending["original_record_sha256"], api._json_hash(pending["record"])):
        raise ValueError("newer decisions found during recovery")
    current_layout = run / "reading_layout.json"
    if (_layout_hash(run) != pending["original_layout_sha256"]
            and (not current_layout.is_file() or api._read(current_layout) != pending["layout"])):
        raise ValueError("newer layout found during recovery")
    api._write(current_layout, pending["layout"])
    api._write(run / "review_state.json", pending["record"])
    api._write(run / "patches" / (pending["patch_sha256"] + ".json"), pending["document"])
    api._write(run / "transcript.corrected.json", pending["record"]["transcript"])
    api._write(run / "correction_audit.json", pending["record"]["audit"])
    state.setdefault("notes", {})["stale"] = True
    api._refresh(run, state)  # One export; no apply -> resume loop.
    output = api._root_path(state["primary_transcript"])
    if _hash_text(output.read_text(encoding="utf-8")) != pending["candidate_sha256"]:
        raise ValueError("published output differs from the accepted candidate; recovery required")
    result = {"ok": True, "run_id": pending["run_id"], "status": state["status"],
              "primary_transcript": state["primary_transcript"], "patch_count": pending["patch_count"],
              "corrections": pending["corrections"], "preflight_passes": 1,
              "unresolved_ids": state["review"]["unresolved_ids"],
              "next": "Deliver this file. Do not reread raw ASR, rerun resume, or dump catalog history."}
    api._write(receipt_path, result)
    (run / "review_commit_pending.json").unlink()
    return result
