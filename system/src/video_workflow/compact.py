"""Small model-facing views. Full evidence stays on disk, byte-for-byte intact."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path


def segment_view(segment):
    return {key: copy.deepcopy(segment[key]) for key in ("id", "start", "end", "text")}


def recheck_view(result, source_path, display_path=None):
    """Preserve every hypothesis and its provenance, omitting decoder arrays only."""
    fields = ("run_id", "segment_id", "id", "kind", "engine", "language",
              "clip_start", "clip_end", "independent_evidence", "audio_verified",
              "note", "audio_path", "paths_relative_to", "beam_size", "prompt",
              "parameters_sha256")
    view = {key: copy.deepcopy(result[key]) for key in fields if key in result}
    if result.get("review_glossary"):
        view["review_glossary"] = copy.deepcopy(result["review_glossary"])
    view["segments"] = [segment_view(segment) for segment in result["segments"]]
    view["evidence_path"] = str(display_path or source_path)
    view["source_sha256"] = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    view["details_omitted"] = "Decoder tokens/scores and runtime details remain in evidence_path; read them if needed."
    return view


def status_view(state):
    fields = ("run_id", "status", "message", "error", "primary_transcript", "review",
              "context_review", "review_task", "completion_requirements", "visuals_sha256", "exports",
              "primary_user_edits", "primary_user_edits_replaced")
    view = {key: copy.deepcopy(state[key]) for key in fields if key in state}
    visual = state.get("visual_review")
    if isinstance(visual, dict):
        view["visual_review"] = {key: copy.deepcopy(visual[key]) for key in
                                 ("status", "card_count", "uncertainty_count", "visuals_sha256",
                                  "output", "structured_output", "woven_output") if key in visual}
    # Retain stage/recheck failures, but not their large identity/artifact inventories.
    failures = []
    for group in ("stages", "rechecks"):
        for name, operation in state.get(group, {}).items():
            if operation.get("status") == "failed":
                failures.append({"group": group, "name": name, "error": operation.get("error")})
    if failures:
        view["failures"] = failures
    return view
