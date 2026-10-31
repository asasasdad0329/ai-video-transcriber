"""Evidence-preserving transcript review and export helpers.

This module flags possible errors; it never treats ASR scores as calibrated
correctness probabilities and never changes Chinese script while reviewing.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from pypinyin import lazy_pinyin
    from rapidfuzz.fuzz import ratio as fuzzy_ratio
except ImportError:  # Candidate suggestions are optional; validation is not.
    lazy_pinyin = None
    fuzzy_ratio = None

try:
    from opencc import OpenCC
except ImportError:
    OpenCC = None

_SCRIPT_COMPARISON = OpenCC("t2s") if OpenCC is not None else None


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,12}")
_NUMBER = re.compile(r"\d+(?:[.,:/%-]\d+)*|[零一二三四五六七八九十百千萬億兩〇]+(?:年|月|日|元|倍|成|個|次|%|％)")
_WHISPER_CONTROL = re.compile(r"(?:\[_[A-Z][A-Z0-9_]*\]|<\|[^|]+\|>)")
_DEFAULTS = {
    "word_probability_threshold": 0.5,
    "min_low_probability_tokens": 1,
    "review_numbers": True,
    "ocr_window_seconds": 20.0,
    "max_candidates_per_segment": 5,
    "max_terms": 250,
    "candidate_similarity": 90.0,
    "repeated_phrase_min_chars": 3,
    "repeated_phrase_count": 3,
}


def _finite_time(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite, nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be a finite, nonnegative number")
    return result


def _validate_segments(transcript: dict) -> list[dict]:
    segments = transcript.get("segments")
    if not isinstance(segments, list):
        raise ValueError("transcript.segments must be a list")
    ids: set[Any] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("each segment must be an object")
        identifier = segment.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)) or identifier == "":
            raise ValueError("segment.id must be a nonempty string or an integer")
        if identifier in ids:
            raise ValueError(f"duplicate segment id: {identifier}")
        ids.add(identifier)
        if not isinstance(segment.get("text"), str):
            raise ValueError(f"segment {identifier}: text must be a string")
        start = _finite_time(segment.get("start"), f"segment {identifier}.start")
        end = _finite_time(segment.get("end"), f"segment {identifier}.end")
        if end < start:
            raise ValueError(f"segment {identifier}: end precedes start")
    return segments


def _validate_frames(visuals: dict) -> list[dict]:
    frames = visuals.get("frames", [])
    if not isinstance(frames, list):
        raise ValueError("visuals.frames must be a list")
    seen = set()
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("id"), str) or not frame["id"]:
            raise ValueError("each frame must have a nonempty string id")
        if frame["id"] in seen:
            raise ValueError(f"duplicate frame id: {frame['id']}")
        seen.add(frame["id"])
        _finite_time(frame.get("time"), f"frame {frame['id']}.time")
        if not isinstance(frame.get("text", ""), str):
            raise ValueError(f"frame {frame['id']}: text must be a string")
    return frames


def _settings(config: dict) -> dict:
    supplied = config.get("review", config)
    if not isinstance(supplied, dict):
        raise ValueError("review config must be an object")
    settings = {**_DEFAULTS, **supplied}
    for key in ("word_probability_threshold", "ocr_window_seconds", "candidate_similarity"):
        settings[key] = _finite_time(settings[key], key)
    if settings["word_probability_threshold"] > 1 or settings["candidate_similarity"] > 100:
        raise ValueError("probability threshold must be <= 1 and candidate similarity <= 100")
    for key in ("min_low_probability_tokens", "max_candidates_per_segment", "max_terms", "repeated_phrase_min_chars", "repeated_phrase_count"):
        if isinstance(settings[key], bool) or not isinstance(settings[key], int) or settings[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not isinstance(settings["review_numbers"], bool):
        raise ValueError("review_numbers must be a boolean")
    return settings


def _near_frames(segment: dict, frames: list[dict], window: float) -> list[dict]:
    return [frame for frame in frames if segment["start"] - window <= frame["time"] <= segment["end"] + window]


def _is_text_token(token: dict) -> bool:
    """Whether a decoder token can provide evidence about lexical spelling.

    Whisper.cpp serializes timestamps as e.g. ``[_TT_167]``; OpenAI-style
    tokenizers use ``<|3.34|>``. Neither score measures a transcribed word.
    Literal bracketed words (e.g. ``[GPU]``) remain ordinary text.
    """
    text = token.get("text")
    if not isinstance(text, str) or token.get("special") is True or token.get("is_special") is True:
        return False
    stripped = text.strip()
    if not stripped or _WHISPER_CONTROL.fullmatch(stripped):
        return False
    return any(character.isalnum() for character in stripped)


def _segment_excerpt(segment: dict | None) -> dict | None:
    if segment is None:
        return None
    return {key: segment[key] for key in ("id", "start", "end", "text")}


def _frame_excerpt(frame: dict) -> dict:
    fields = ("id", "time", "path", "text", "confidence", "duplicate_of",
              "ocr_status", "text_source_frame", "text_source_time",
              "ocr_evidence_time", "perceptually_similar_to")
    return {**{key: copy.deepcopy(frame[key]) for key in fields if key in frame},
            "kind": "video_frame", "audio_verified": False}


def _phonetic_candidates(text: str, terms: list[tuple[str, str]], settings: dict) -> list[dict]:
    if lazy_pinyin is None or fuzzy_ratio is None or _SCRIPT_COMPARISON is None:
        return []
    candidates = []
    seen = set()
    # Compare only CJK spans of the same length: this is a candidate generator,
    # not a grammatical rewriter or simplified/traditional conversion stage.
    for term, evidence_id in terms[: settings["max_terms"]]:
        if not _CJK.fullmatch(term) or term in text:
            continue
        target_pinyin = " ".join(lazy_pinyin(term))
        for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text[:1000]):
            for offset in range(len(run) - len(term) + 1):
                original = run[offset : offset + len(term)]
                if _SCRIPT_COMPARISON.convert(original) == _SCRIPT_COMPARISON.convert(term):
                    # Script-only differences are normalization, not ASR errors.
                    continue
                # More than half the characters must already agree. This keeps
                # unrelated homophones from flooding long lecture transcripts.
                if sum(a == b for a, b in zip(original, term)) < max(1, math.ceil(len(term) / 2)):
                    continue
                score = float(fuzzy_ratio(" ".join(lazy_pinyin(original)), target_pinyin))
                key = (original, term, evidence_id)
                if score >= settings["candidate_similarity"] and key not in seen:
                    seen.add(key)
                    candidates.append({"original": original, "suggestion": term, "similarity": score, "evidence_id": evidence_id, "kind": "phonetic_candidate", "auto_apply": False})
    candidates.sort(key=lambda item: (-item["similarity"], item["suggestion"], item["original"]))
    return candidates[: settings["max_candidates_per_segment"]]


def triage(transcript: dict, visuals: dict, config: dict, glossary: list[str]) -> dict:
    """Return segment-level review flags without rewriting any source text."""
    segments = _validate_segments(transcript)
    frames = _validate_frames(visuals)
    settings = _settings(config)
    if not isinstance(glossary, list) or any(not isinstance(term, str) for term in glossary):
        raise ValueError("glossary must be a list of strings")
    glossary_terms = [(term.strip(), f"glossary:{index}") for index, term in enumerate(glossary) if term.strip()]
    suspects = []
    by_reason: Counter = Counter()
    ignored_nontext_tokens = 0
    ignored_low_score_nontext_tokens = 0
    previous_text = None
    for segment in segments:
        reasons = []
        tokens = segment.get("tokens", [])
        if not isinstance(tokens, list):
            tokens = []
        low_tokens = []
        for token in tokens:
            if not isinstance(token, dict):
                continue
            probability = token.get("p")
            low_score = isinstance(probability, (int, float)) and not isinstance(probability, bool) and math.isfinite(probability) and 0 <= probability < settings["word_probability_threshold"]
            if not _is_text_token(token):
                ignored_nontext_tokens += 1
                ignored_low_score_nontext_tokens += int(low_score)
                continue
            if low_score:
                low_tokens.append({key: token[key] for key in ("text", "p", "start", "end") if key in token})
        if len(low_tokens) >= settings["min_low_probability_tokens"]:
            reasons.append("low_asr_score")
        compact = re.sub(r"\W+", "", segment["text"], flags=re.UNICODE)
        min_chars = settings["repeated_phrase_min_chars"]
        repeated = re.search(rf"(.{{{min_chars},30}})\1{{{settings['repeated_phrase_count'] - 1},}}", compact)
        if repeated or (compact and len(compact) >= min_chars and compact == previous_text):
            reasons.append("repeated_text")
        previous_text = compact
        numbers = _NUMBER.findall(segment["text"])
        if numbers and settings["review_numbers"]:
            reasons.append("numbers_need_verification")
        nearby = _near_frames(segment, frames, settings["ocr_window_seconds"])
        terms = list(glossary_terms)
        for frame in nearby:
            # Duplicate frames do not count as independent corroboration.
            if not frame.get("duplicate_of"):
                terms.extend((match.group(), frame["id"]) for match in _CJK.finditer(frame.get("text", "")))
        candidates = _phonetic_candidates(segment["text"], terms, settings)
        if candidates:
            reasons.append("phonetic_term_candidate")
        if not segment["text"].strip() and segment["end"] > segment["start"]:
            reasons.append("empty_transcript_segment")
        if reasons:
            candidate_ids = [item["evidence_id"] for item in candidates]
            evidence_ids = list(dict.fromkeys([frame["id"] for frame in nearby] + candidate_ids))
            suspects.append({"segment_id": segment["id"], "start": segment["start"], "end": segment["end"], "text": segment["text"], "reasons": reasons, "candidates": candidates, "evidence_ids": evidence_ids, "low_score_tokens": low_tokens, "numbers": numbers, "auto_apply": False})
            by_reason.update(reasons)
    return {
        "suspects": suspects,
        "glossary_evidence": [{"id": evidence_id, "text": term, "kind": "user_glossary"} for term, evidence_id in glossary_terms],
        "settings": settings,
        "stats": {"segment_count": len(segments), "suspect_count": len(suspects), "suspect_fraction": len(suspects) / len(segments) if segments else 0.0, "reasons": dict(by_reason), "ignored_nontext_token_count": ignored_nontext_tokens, "ignored_low_score_nontext_token_count": ignored_low_score_nontext_tokens, "phonetic_candidates_available": lazy_pinyin is not None and fuzzy_ratio is not None and _SCRIPT_COMPARISON is not None, "phonetic_dependencies": ["pypinyin", "rapidfuzz", "opencc"], "scores_are_calibrated_probabilities": False},
    }


def make_review_bundle(transcript: dict, visuals: dict, triage_result: dict, run_id: str) -> dict:
    """Provide only suspect segments, one neighbor each side, and seen evidence."""
    segments = _validate_segments(transcript)
    frames = _validate_frames(visuals)
    index = {segment["id"]: position for position, segment in enumerate(segments)}
    frame_index = {frame["id"]: frame for frame in frames}
    glossary_evidence = {item["id"]: item for item in triage_result.get("glossary_evidence", [])}
    used_frames = set()
    used_glossary = set()
    source_ids = set()
    items = []
    seen = set()
    for suspect in triage_result.get("suspects", []):
        segment_id = suspect.get("segment_id")
        if segment_id not in index or segment_id in seen:
            raise ValueError(f"unknown or duplicate suspect segment id: {segment_id}")
        seen.add(segment_id)
        position = index[segment_id]
        context = segments[max(0, position - 1) : position + 2]
        source_ids.update(segment["id"] for segment in context)
        evidence_ids = []
        for evidence_id in suspect.get("evidence_ids", []):
            if evidence_id in frame_index:
                used_frames.add(evidence_id)
            elif evidence_id in glossary_evidence:
                used_glossary.add(evidence_id)
            else:
                raise ValueError(f"unknown suspect evidence id: {evidence_id}")
            evidence_ids.append(evidence_id)
        low_tokens = [{key: token[key] for key in ("text", "p", "start", "end") if key in token}
                      for token in suspect.get("low_score_tokens", [])
                      if isinstance(token, dict) and _is_text_token(token)]
        items.append({"segment_id": segment_id, "segment": _segment_excerpt(segments[position]), "previous": _segment_excerpt(segments[position - 1]) if position else None, "next": _segment_excerpt(segments[position + 1]) if position + 1 < len(segments) else None, "reasons": list(suspect.get("reasons", [])), "candidates": copy.deepcopy(suspect.get("candidates", [])), "low_score_tokens": low_tokens, "evidence_ids": evidence_ids})
    evidence = [_frame_excerpt(frame) for frame in frames if frame["id"] in used_frames]
    evidence.extend({key: copy.deepcopy(item[key]) for key in ("id", "text", "kind") if key in item} for identifier, item in glossary_evidence.items() if identifier in used_glossary)
    text_chars = sum(len(segment["text"]) for segment in segments if segment["id"] in source_ids)
    full_chars = sum(len(segment["text"]) for segment in segments)
    payload_chars = len(json.dumps({"items": items, "evidence": evidence}, ensure_ascii=False))
    return {"run_id": run_id, "task": "Review only listed suspect segments; return patches. Keep speech wording. Do not claim to have listened to audio. Frame text and glossary are clues, not proof that a phrase was spoken; unresolved cases must remain needs_review.", "items": items, "evidence": evidence, "evidence_ids": [item["id"] for item in evidence], "patch_schema": {"patches": [{"segment_id": "existing segment id", "old_text": "exact current text", "new_text": "corrected text or unchanged text", "decision": "correct|keep|needs_review", "evidence_ids": ["existing evidence id"], "reason": "specific explanation"}]}, "stats": {"suspect_count": len(items), "context_segment_count": len(source_ids), "source_segment_count": len(segments), "source_text_characters": full_chars, "selected_text_characters": text_chars, "selected_text_fraction": text_chars / full_chars if full_chars else 0.0, "serialized_payload_characters": payload_chars, "token_count": None, "token_count_note": "Count with the selected model tokenizer before submission; character counts are not token counts.", "frame_count": len(used_frames)}}


def apply_patches(transcript: dict, patch_document: dict, evidence_ids: set[str]) -> tuple[dict, list]:
    """Validate a complete batch first; return a copy and an auditable decision log."""
    segments = _validate_segments(transcript)
    if not isinstance(patch_document, dict) or not isinstance(patch_document.get("patches"), list):
        raise ValueError("patch_document.patches must be a list")
    if transcript.get("run_id") is not None and patch_document.get("run_id") is not None and transcript["run_id"] != patch_document["run_id"]:
        raise ValueError("patch run_id does not match transcript run_id")
    index = {segment["id"]: position for position, segment in enumerate(segments)}
    seen = set()
    validated = []
    for patch in patch_document["patches"]:
        if not isinstance(patch, dict):
            raise ValueError("each patch must be an object")
        segment_id = patch.get("segment_id")
        if isinstance(segment_id, bool) or not isinstance(segment_id, (str, int)) or segment_id not in index:
            raise ValueError(f"unknown segment id: {segment_id}")
        if segment_id in seen:
            raise ValueError(f"duplicate patch for segment: {segment_id}")
        seen.add(segment_id)
        original = segments[index[segment_id]]["text"]
        if patch.get("old_text") != original:
            raise ValueError(f"segment {segment_id}: old_text does not match exactly")
        if not isinstance(patch.get("decision"), str) or patch["decision"] not in {"correct", "keep", "needs_review"}:
            raise ValueError(f"segment {segment_id}: unknown decision")
        if not isinstance(patch.get("reason"), str) or not patch["reason"].strip():
            raise ValueError(f"segment {segment_id}: nonempty reason required")
        refs = patch.get("evidence_ids")
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in evidence_ids for ref in refs):
            raise ValueError(f"segment {segment_id}: unknown or malformed evidence ids")
        if len(refs) != len(set(refs)):
            raise ValueError(f"segment {segment_id}: duplicate evidence ids")
        replacement = patch.get("new_text")
        if not isinstance(replacement, str):
            raise ValueError(f"segment {segment_id}: new_text must be a string")
        if patch["decision"] == "correct":
            if not refs:
                raise ValueError(f"segment {segment_id}: correction requires evidence")
            if replacement == original or not replacement.strip():
                raise ValueError(f"segment {segment_id}: correction must contain a nonempty change")
        elif replacement != original:
            raise ValueError(f"segment {segment_id}: keep/needs_review must preserve the original text")
        validated.append(copy.deepcopy(patch))
    corrected = copy.deepcopy(transcript)
    audit = []
    for patch in validated:
        applied = patch["decision"] == "correct"
        if applied:
            target = corrected["segments"][index[patch["segment_id"]]]
            target["text"] = patch["new_text"]
            # Token strings/times belong to the original hypothesis. Preserve
            # them as evidence, while making their stale alignment explicit.
            target["token_alignment_matches_text"] = False
        audit.append({**patch, "applied": applied})
    return corrected, audit


def _timestamp(seconds: float, separator: str = ".") -> str:
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{milliseconds:03d}"


def export_transcripts(transcript: dict, out_dir: Path, basename: str = "transcript") -> dict:
    """Export the given hypothesis faithfully; script conversion is separate."""
    segments = _validate_segments(transcript)
    if not isinstance(basename, str) or not basename or basename in {".", ".."} or any(char in basename for char in '/\\<>:"|?*'):
        raise ValueError("basename must be a plain filename without path separators")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {extension: out_dir / f"{basename}.{extension}" for extension in ("txt", "srt", "md")}
    paths["txt"].write_text("\n".join(segment["text"] for segment in segments) + ("\n" if segments else ""), encoding="utf-8")
    srt = []
    markdown = ["# 逐字稿", ""]
    for position, segment in enumerate(segments, 1):
        srt.extend([str(position), f"{_timestamp(segment['start'], ',')} --> {_timestamp(segment['end'], ',')}", segment["text"], ""])
        markdown.extend([f"**{_timestamp(segment['start'])} – {_timestamp(segment['end'])}**", "", segment["text"], ""])
    paths["srt"].write_text("\n".join(srt) + ("\n" if srt else ""), encoding="utf-8")
    paths["md"].write_text("\n".join(markdown), encoding="utf-8")
    return {extension: str(path.resolve()) for extension, path in paths.items()}


def make_chapter_bundles(transcript: dict, visuals: dict, seconds: float = 300) -> list:
    """Partition source evidence for later authoring; never claim to summarize it."""
    segments = _validate_segments(transcript)
    frames = _validate_frames(visuals)
    seconds = _finite_time(seconds, "chapter seconds")
    if not seconds:
        raise ValueError("chapter seconds must be greater than zero")
    if not segments and not frames:
        return []
    duration = max([segment["end"] for segment in segments] + [frame["time"] for frame in frames])
    count = max(1, math.ceil(duration / seconds))
    chapters = []
    for index in range(count):
        start = index * seconds
        end = min(duration, (index + 1) * seconds)
        last = index == count - 1
        chapter_segments = [segment for segment in segments if (segment["start"] < end and segment["end"] > start) or (segment["start"] == segment["end"] and start <= segment["start"] and (segment["start"] < end or last and segment["start"] == end))]
        chapter_frames = [frame for frame in frames if start <= frame["time"] and (frame["time"] < end or last and frame["time"] == end)]
        chapters.append({"id": f"chapter-{index + 1:04d}", "start": start, "end": end, "status": "source_bundle_only", "segments": [_segment_excerpt(segment) for segment in chapter_segments], "frames": [_frame_excerpt(frame) for frame in chapter_frames], "coverage": {"segment_ids": [segment["id"] for segment in chapter_segments], "frame_ids": [frame["id"] for frame in chapter_frames], "overlapping_segments_are_repeated": True}, "authoring_instruction": "Create notes only from these sources; distinguish spoken transcript from visual additions, cite source IDs and timestamps, and mark unresolved transcript errors."})
    return chapters
