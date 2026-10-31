"""Read existing evidence and record build acceptance without changing a run."""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system/src"))
from workspace_paths import resolve, RUNS, RECORDS
from video_workflow import reading, cli


def content_characters(text):
    # Runs exported before 台 became the default still carry the converter's 臺.
    text = text.replace("〔？〕", "").replace("臺", "台")
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_visual_coverage(visuals, visual_review, supplemental=()):
    covered = [fid for card in visual_review["cards"] for fid in card["frame_ids"]]
    omitted = [fid for item in visual_review.get("omissions", []) for fid in item["frame_ids"]]
    assert len(covered) == len(set(covered))
    assert len(omitted) == len(set(omitted)) and not set(covered) & set(omitted)
    required = {frame["id"] for frame in visuals["frames"]}
    assert required <= set(covered) | set(omitted)
    assert set(covered) | set(omitted) <= required | set(supplemental)
    for item in visual_review.get("omissions", []):
        assert item["reviewed"] is True and item["reason"].strip()
        assert item["decision"] in ("redundant", "decorative", "subtitle_indexed")
        assert item["basis_frame_ids"] and set(item["basis_frame_ids"]) <= set(covered)
    if omitted:
        assert visual_review["coverage"]["all_sampled_frames_reviewed"] is False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_ids", nargs="+")
    args = parser.parse_args()
    results = []
    for run_id in args.run_ids:
        run = (ROOT / RUNS / run_id).resolve()
        assert run.parent == ROOT / RUNS, "Invalid run path"
        state = read(run / "state.json")
        raw = read(run / "transcript.raw.json")
        visuals = read(run / "visuals.json")
        visual_review = read(run / "visual_review.json")
        assert state["status"] in ("completed", "completed_with_unresolved")
        assert state.get("context_review", {}).get("completed"), "Full context review is missing or stale"
        visual_hash = hashlib.sha256(json.dumps(visuals, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert visual_review["visuals_sha256"] == visual_hash
        supplemental = {frame["id"] for frame in cli._indexed_evidence(run)}
        verify_visual_coverage(visuals, visual_review, supplemental)
        assert raw["actual_device"] == "cuda" and raw["backend_evidence"]
        verified_artifacts = 0
        for stage in state["stages"].values():
            assert stage["status"] == "done"
            for artifact in stage["artifacts"]:
                path = (run / artifact["path"]).resolve()
                assert path.is_relative_to(run) and sha(path) == artifact["sha256"]
                verified_artifacts += 1
        assert sha(resolve(state["source"])) == state["identity"]["source_sha256"]
        for frame in visuals["frames"]:
            assert (resolve(frame["path"])).is_file()
        for key in ("output", "structured_output", "woven_output"):
            output = resolve(state["visual_review"][key])
            assert output.is_file() and sha(output) == state["visual_review"][key + "_sha256"]
        record = read(run / "review_state.json")
        current = record["transcript"]
        context = record["context_review"]
        assert context["transcript_sha256"] == hashlib.sha256(json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert context["reviewed_segment_ids"] == [s["id"] for s in current["segments"]]
        assert context["reviewed_card_ids"] == [c["id"] for c in visual_review["cards"]]
        woven = (resolve(state["visual_review"]["woven_output"])).read_text(encoding="utf-8")
        reading_map = read(run / "reading_map.json")
        layout = read(run / "reading_layout.json") if (run / "reading_layout.json").is_file() else {}
        speech_blocks = [b for b in reading_map["blocks"] if b["kind"] == "speech"]
        assert [sid for b in speech_blocks for sid in b["segment_ids"]] == [s["id"] for s in current["segments"]]
        # Preserve source-segment boundaries during conversion: joining "就可以了"
        # with "然后" first would invent the cross-sentence OpenCC phrase "了然".
        expected = "".join(reading.traditional(s["text"], layout.get("protected_terms", [])) for s in current["segments"])
        assert content_characters("".join(b["text"] for b in speech_blocks)) == content_characters(expected)
        cursor = 0
        for block in speech_blocks:
            position = woven.find(block["text"], cursor)
            assert position >= cursor, "Speech missing or reordered"
            cursor = position + len(block["text"])
        assert not re.search(r"!?\[[^\n]*\]\([^\n]*\)", woven), "Reading edition has file dependencies"
        visual_blocks = [b for b in reading_map["blocks"] if b["kind"] == "visual"]
        assert {b["card_id"] for b in visual_blocks} == {c["id"] for c in visual_review["cards"]}
        for block in visual_blocks:
            for line in block["text"].splitlines():
                if line:
                    assert "> " + line in woven, "Visual content missing"
        audit = read(run / "correction_audit.json") if (run / "correction_audit.json").is_file() else []
        results.append({
            "run_id": run_id, "source": state["source"], "status": state["status"],
            "duration_seconds": read(run / "probe.json")["duration"],
            "asr_device": raw["actual_device"], "backend_evidence": raw["backend_evidence"],
            "segments": len(raw["segments"]), "frames": len(visuals["frames"]),
            "triage_stats": read(run / "triage.json")["stats"],
            "decisions": len(audit), "corrections": sum(bool(a["applied"]) for a in audit),
            "unresolved_ids": state["review"]["unresolved_ids"],
            "visual_cards": len(visual_review["cards"]), "visual_uncertainties": visual_review["uncertainty_count"], "verified_artifacts": verified_artifacts,
            "context_review": state["context_review"],
            "output": state["visual_review"]["woven_output"], "stage_times": {
                name: {"started_at": stage["started_at"], "completed_at": stage["completed_at"], "attempt": stage["attempt"]}
                for name, stage in state["stages"].items()},
        })
    baseline_path = ROOT / RECORDS / "external-dependencies-before.json"
    baseline = read(baseline_path) if baseline_path.is_file() else []
    changed = [item["Path"] for item in baseline if not Path(item["Path"]).is_file() or sha(item["Path"]).upper() != item["Hash"].upper()]
    assert not changed, f"External tracked files changed: {changed}"
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": True,
              "external_tracked_files_unchanged": len(baseline), "runs": results,
              "limitations": ["ASR token scores are uncalibrated; review is selective.",
                              "Scene and interval sampling is not exhaustive video inspection.",
                              "Cross-machine GPU compatibility is not tested by this report."]}
    destination = ROOT / RECORDS / "build-acceptance.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(destination), "runs": [r["run_id"] for r in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
