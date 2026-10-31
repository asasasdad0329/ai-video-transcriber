"""Scoped, source-backed correction hints. Never automatic substitutions."""
from __future__ import annotations

from .evidence_index import digest


def entry(scope, term, variant, run_id, segment_id, decision, transcript_sha256):
    for name, value in (("scope", scope), ("term", term), ("variant", variant)):
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise ValueError(f"{name} must be nonempty text up to 120 characters")
    if term == variant:
        raise ValueError("term and variant must differ")
    if (decision.get("decision") != "correct" or not decision.get("evidence_ids")
            or variant not in decision.get("old_text", "")
            or decision["old_text"].replace(variant, term) != decision.get("new_text")):
        raise ValueError("term pair must exactly explain a recorded, evidence-backed correction")
    result = {"scope": scope, "term": term, "variant": variant, "run_id": run_id,
              "segment_id": segment_id, "decision_sha256": digest(decision),
              "transcript_sha256": transcript_sha256, "evidence_ids": decision["evidence_ids"],
              "source_text": decision["old_text"], "confirmed_text": decision["new_text"],
              "basis": "recorded_model_correction_not_independent_audio_verification",
              "auto_apply": False}
    result["id"] = "term:" + digest(result)[:20]
    return result


def hints(entries, scope, text):
    if not scope:
        return []
    return [{**{k: e[k] for k in ("id", "term", "variant", "scope", "run_id", "segment_id", "basis")},
             "patch_evidence": False, "auto_apply": False}
            for e in entries if e["scope"] == scope and (e["variant"] in text or e["term"] in text)]
