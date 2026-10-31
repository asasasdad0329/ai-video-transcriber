import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import cli, evidence_index as ei, vocabulary
from PIL import Image, ImageDraw


class EvidenceFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def visuals(self):
        result = {"frames": [], "coverage": {"exhaustive": False}}
        for i in range(4):
            im = Image.new("RGB", (320, 180), "white")
            draw = ImageDraw.Draw(im)
            draw.rectangle((20, 20, 120, 60), outline="black")
            if i >= 2:
                draw.line((121, 40, 170, 40), fill="black", width=2)
            path = self.root / f"frame-{i}.png"
            im.save(path)
            result["frames"].append({"id": f"f{i}", "time": float(i), "path": str(path),
                "text": f"身強\n字幕{i}", "boxes": [
                    {"text": "身強", "box": [[20,20],[60,20],[60,45],[20,45]], "confidence": .9},
                    {"text": f"字幕{i}", "box": [[90,145],[230,145],[230,165],[90,165]], "confidence": .9}]})
        return result


class EvidenceIndexTests(EvidenceFixtures):
    def test_acceptance_checker_preserves_honest_omission_coverage(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_build", Path(__file__).resolve().parents[1] / "bootstrap/verify_build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        visuals = {"frames": [{"id": "a"}, {"id": "b"}]}
        reviewed = {"cards": [{"frame_ids": ["a", "extra"]}], "omissions": [
            {"frame_ids": ["b"], "basis_frame_ids": ["a"], "reviewed": True,
             "reason": "字幕另存", "decision": "subtitle_indexed"}],
             "coverage": {"all_sampled_frames_reviewed": False}}
        module.verify_visual_coverage(visuals, reviewed, ["extra"])
        with self.assertRaises(AssertionError):
            module.verify_visual_coverage(visuals, reviewed)
        reviewed["coverage"]["all_sampled_frames_reviewed"] = True
        with self.assertRaises(AssertionError):
            module.verify_visual_coverage(visuals, reviewed, ["extra"])

    def test_arrow_change_retained_and_every_frame_retrievable(self):
        data = self.visuals()
        before = copy.deepcopy(data)
        index = ei.build(data, "r", Path)
        self.assertEqual(data, before)
        self.assertEqual(len(index["groups"]), 1)
        self.assertEqual(index["groups"][0]["state_change_ids"], ["f0", "f2"])
        selected = ei.select(index, group_id="g0001")
        self.assertIn("f2", [f["id"] for f in selected])
        self.assertEqual(len(ei.select(index, group_id="g0001", full=True)), 4)
        self.assertFalse(index["semantic_reviewed"])

    def test_no_text_is_unknown_not_discardable(self):
        data = self.visuals()
        for f in data["frames"]:
            f.update(text="", boxes=[])
        index = ei.build(data, "r", Path)
        self.assertTrue(all(g["label"] == "scenario_or_unknown" and g["requires_semantic_review"] for g in index["groups"]))

    def test_bottom_arrow_intermediate_state_retained(self):
        data = self.visuals()
        path = Path(data["frames"][1]["path"])
        with Image.open(path) as im:
            im = im.convert("RGB")
        ImageDraw.Draw(im).line((10, 170, 60, 170), fill="black", width=2)
        im.save(path)
        index = ei.build(data, "r", Path)
        self.assertIn("f1", index["groups"][0]["state_change_ids"])

    def test_bottom_label_not_lost_by_subtitle_heuristic(self):
        index = ei.build(self.visuals(), "r", Path)
        frame = ei.select(index, frame_ids=["f0"])[0]
        self.assertIn("字幕0", frame["text"])
        self.assertIn("身強", frame["data_text"])

    def test_focus_pins_candidates_and_merges_context(self):
        index = ei.build(self.visuals(), "r", Path)
        transcript = {"segments": [{"id": f"s{i}", "start": i, "end": i+.5, "text": "深強"} for i in range(3)]}
        triage = {"suspects": [{"segment_id": "s0", "candidates": [{"evidence_id": "f3"}]},
                               {"segment_id": "s1"}]}
        bundle = ei.focused_bundle(transcript, triage, index)
        self.assertIn("f3", bundle["items"][0]["evidence_ids"])
        self.assertEqual(len(bundle["segments"]), 3)
        self.assertEqual(len(bundle["items"]), 2)
        self.assertFalse(bundle["selection_is_exhaustive"])

    def test_bad_selection_and_times_rejected(self):
        data = self.visuals()
        index = ei.build(data, "r", Path)
        for kwargs in ({"start": float("nan"), "end": 1}, {"start": 2, "end": 1},
                       {"frame_ids": ["not-found"]}, {"frame_ids": ["f0", "f0"]}, {"group_id": "missing"}):
            with self.assertRaises(ValueError):
                ei.select(index, **kwargs)
        data["frames"][0]["time"] = -1
        with self.assertRaises(ValueError):
            ei.build(data, "r", Path)

    def test_visual_batches_cover_every_group_once_without_images_in_summary(self):
        index = {"run_id": "r", "groups": [
            {"id": f"g{i:04d}", "start": float(i), "end": float(i),
             "label": "scenario_or_unknown", "representative_id": f"f{i}",
             "frame_ids": [f"f{i}"], "state_change_ids": [f"f{i}"]}
            for i in range(1, 26)]}
        for n in range(1, 4):
            (self.root / f"overview-{n:03d}.jpg").write_bytes(b"sheet")
        summary = ei.review_batches(index, self.root, sheets_per_batch=2)
        self.assertEqual([b["group_count"] for b in summary["batches"]], [24, 1])
        self.assertNotIn("contact_sheets", str(summary))
        details = [ei.review_batches(index, self.root, sheets_per_batch=2,
                                     batch_id=b["batch_id"]) for b in summary["batches"]]
        self.assertEqual([g["id"] for batch in details for g in batch["groups"]],
                         [g["id"] for g in index["groups"]])
        self.assertEqual([len(batch["contact_sheets"]) for batch in details], [2, 1])
        for bad in (0, 5, True, 1.5):
            with self.assertRaises(ValueError):
                ei.review_batches(index, self.root, sheets_per_batch=bad)
        with self.assertRaises(ValueError):
            ei.review_batches(index, self.root, sheets_per_batch=2, batch_id="vb003")
        (self.root / "overview-003.jpg").unlink()
        with self.assertRaises(ValueError):
            ei.review_batches(index, self.root, sheets_per_batch=2)

    def test_vocab_requires_recorded_exact_pair_and_scope(self):
        decision = {"decision": "correct", "old_text": "談深強深弱", "new_text": "談身強身弱", "evidence_ids": ["f1"]}
        entry = vocabulary.entry("八字", "身強身弱", "深強深弱", "r", "s", decision, "hash")
        self.assertEqual(len(vocabulary.hints([entry], "八字", "談深強深弱")), 1)
        self.assertEqual(vocabulary.hints([entry], "建築", "談深強深弱"), [])
        self.assertEqual(vocabulary.hints([entry], None, "談深強深弱"), [])
        self.assertFalse(entry["auto_apply"])
        for changes in ({"decision": "needs_review"}, {"evidence_ids": []}, {"new_text": "談其他內容"}):
            with self.assertRaises(ValueError):
                vocabulary.entry("八字", "身強身弱", "深強深弱", "r", "s", {**decision, **changes}, "hash")


class EvidenceIndexCLITests(EvidenceFixtures):
    # A separate fixture uses actual files but never invokes ASR or downloads.
    def prepare(self):
        p = patch.object(cli, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        task_patch = patch.object(cli, "_review_task_id",
                                  side_effect=lambda explicit=None: explicit or "evidence-test")
        task_patch.start()
        self.addCleanup(task_patch.stop)
        self.run = self.root / cli.locations.RUNS / "r"
        (self.root / "source.mp4").write_bytes(b"source")
        cli._write(self.run / "state.json", {"run_id": "r", "source": str(self.root / "source.mp4"),
                   "identity": {"source_sha256": cli._hash(self.root / "source.mp4"), "source_size": 6}})
        cli._write(self.run / "visuals.json", self.visuals())
        cli._write(self.run / "transcript.raw.json", {"run_id": "r", "segments": [
            {"id": "s0", "start": 0, "end": 2, "text": "談深強深弱"}]})
        cli._write(self.run / "triage.json", {"suspects": [{"segment_id": "s0", "candidates": []}]})

    def test_index_does_not_touch_run_and_rejects_overwrite(self):
        self.prepare()
        before = {str(p): p.read_bytes() for p in self.run.glob("*.json")}
        dest = self.root / "index"
        result = cli.index_evidence(self.run, dest)
        self.assertEqual(result["frame_count"], 4)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.glob("*.json")})
        with self.assertRaises(FileExistsError):
            cli.index_evidence(self.run, dest)
        with self.assertRaises(ValueError):
            cli.index_evidence(self.run, self.run / "bad")

    def test_visual_batch_cli_reads_existing_index_without_changing_run(self):
        self.prepare()
        dest = self.root / "index"
        cli.index_evidence(self.run, dest)
        before = {str(p): p.read_bytes() for p in self.run.glob("*.json")}
        plan = cli.visual_batches(dest)
        self.assertEqual(plan["batch_count"], 1)
        batch = cli.visual_batches(dest, batch_id="vb001")
        self.assertEqual(batch["groups"][0]["id"], "g0001")
        self.assertEqual(len(batch["contact_sheets"]), 1)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.glob("*.json")})

    def test_source_and_frame_tampering_rejected(self):
        self.prepare()
        dest = self.root / "index"
        cli.index_evidence(self.run, dest)
        raw = self.run / "transcript.raw.json"
        original = raw.read_bytes()
        raw.write_bytes(original + b" ")
        with self.assertRaises(ValueError):
            cli.focused_review(self.run, dest)
        raw.write_bytes(original)
        (self.root / "frame-0.png").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            cli.evidence_query(dest, frame_ids=["f0"])

    def test_supplemental_registration_does_not_claim_review(self):
        self.prepare()
        cli._write(self.root / "system/config/default.json", {"tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "whisper": "whisper"}})
        extra = self.visuals()
        with patch.object(cli.media, "extract_visuals", return_value=extra):
            cli.index_evidence(self.run, self.root / "index", interval=1, start=0, end=4)
        self.assertEqual(cli._indexed_evidence(self.run), [])
        self.assertTrue(cli.focused_review(self.run, self.root / "index",
                                           task_id="before-registration")["unregistered_evidence_ids"])
        result = cli.register_evidence(self.run, self.root / "index")
        self.assertFalse(result["semantic_reviewed"])
        self.assertEqual(len(cli._indexed_evidence(self.run)), 4)
        self.assertFalse(cli.focused_review(self.run, self.root / "index",
                                            task_id="after-registration")["unregistered_evidence_ids"])

    def test_changed_source_rejected_before_sampling(self):
        self.prepare()
        (self.root / "source.mp4").write_bytes(b"other video")
        with patch.object(cli.media, "extract_visuals") as sample, self.assertRaises(ValueError):
            cli.index_evidence(self.run, self.root / "index", interval=1)
        sample.assert_not_called()

    def test_query_paginated_and_unresolved_reason_preserved(self):
        self.prepare()
        decision = {"decision": "needs_review", "reason": "前次證據有矛盾", "evidence_ids": ["f3"]}
        cli._write(self.run / "review_state.json", {"decisions": {"s0": decision}})
        cli.index_evidence(self.run, self.root / "index")
        page = cli.evidence_query(self.root / "index", group_id="g0001", full=True, limit=2)
        self.assertEqual(len(page["frames"]), 2)
        self.assertEqual(page["next_offset"], 2)
        bundle = cli.focused_review(self.run, self.root / "index", task_id="initial-review")
        self.assertEqual(bundle["items"][0]["current_decision"], decision)
        self.assertIn("f3", bundle["items"][0]["evidence_ids"])
        cli._write(self.run / "triage.json", {"suspects": []})
        explicit = cli.focused_review(self.run, self.root / "index", ["s0"],
                                      task_id="explicit-audit")
        self.assertEqual(explicit["items"][0]["current_decision"], decision)

    def test_resolved_items_not_reopened_and_stale_terms_not_reused(self):
        self.prepare()
        decision = {"segment_id": "s0", "decision": "correct", "old_text": "談深強深弱",
                    "new_text": "談身強身弱", "evidence_ids": ["f0"], "reason": "字幕與上下文"}
        cli._write(self.run / "review_state.json", {"decisions": {"s0": decision}})
        cli.remember_term(self.run, "s0", "身強身弱", "深強深弱", "八字")
        self.assertEqual(len(cli._vocabulary_entries()), 1)
        cli.index_evidence(self.run, self.root / "index")
        self.assertEqual(cli.focused_review(self.run, self.root / "index",
                                            task_id="resolved-filter")["items"], [])
        self.assertEqual(len(cli.focused_review(self.run, self.root / "index", ["s0"], "八字",
                                                task_id="explicit-resolved-audit")["items"]), 1)
        cli._write(self.run / "review_state.json", {"decisions": {"s0": {**decision, "decision": "needs_review"}}})
        self.assertEqual(cli._vocabulary_entries(), [])


if __name__ == "__main__":
    unittest.main()
