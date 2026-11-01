import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import cli


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root_patch = patch.object(cli, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        config = {"tools": {name: "tools/" + name + ".exe" for name in ("ffmpeg", "ffprobe", "whisper")},
                  "model": "model.bin", "language": "zh", "device": "cuda", "threads": 4,
                  "frame_interval": 10, "chapter_seconds": 300, "review": {}}
        cli._write(self.root / "system/config/default.json", config)
        for name in config["tools"].values():
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"executable-placeholder")
        (self.root / "model.bin").write_bytes(b"model-placeholder")
        (self.root / "source.mp4").write_bytes(b"source-placeholder")
        self.calls = {"audio": 0, "asr": 0, "visuals": 0}
        self.patches = [patch.object(cli.media, "probe", return_value={"duration": 310, "streams": []}),
                        patch.object(cli.media, "extract_audio", side_effect=self.audio),
                        patch.object(cli.media, "transcribe", side_effect=self.asr),
                        patch.object(cli.media, "extract_visuals", side_effect=self.visuals)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def audio(self, ffmpeg, source, out):
        self.calls["audio"] += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 16000)

    def asr(self, whisper, model, audio, out, *args, **kwargs):
        self.calls["asr"] += 1
        out.mkdir(parents=True, exist_ok=True)
        cli._write(out / "whisper.json", {"native": "evidence"})
        return {"engine": "whisper.cpp", "language": "zh", "raw_json": str(out / "whisper.json"), "actual_device": "cpu",
                "segments": [{"id": "s000001", "start": 0.0, "end": 1.0,
                              "text": "软件错字", "tokens": [{"text": "错", "p": 0.1, "start": 0.2, "end": 0.4}]},
                             {"id": "s000002", "start": 305.0, "end": 310.0,
                              "text": "第二章有 3 個選項。", "tokens": []}]}

    def visuals(self, ffmpeg, source, out, *args, ocr_device="cpu"):
        self.calls["visuals"] += 1
        out.mkdir(parents=True, exist_ok=True)
        frames = []
        for i, time in enumerate((0.0, 305.0), 1):
            path = out / f"畫面-{i}.png"
            path.write_bytes(b"frame-placeholder" + bytes([i]))
            frames.append({"id": f"f{i:06d}", "time": time, "path": str(path), "text": "資料內容",
                           "confidence": 0.9, "duplicate_of": None})
        return {"frames": frames, "coverage": {"exhaustive": False}}

    def run_process(self):
        result = cli.process("source.mp4")
        self.run = Path(result["run_dir"])
        self.run_id = result["run_id"]
        return result

    def document(self, value, name="document.json"):
        path = self.root / name
        cli._write(path, value)
        return path

    def correction(self, old="软件错字", new="軟體正字", decision="correct"):
        return {"run_id": self.run_id, "patches": [{"segment_id": "s000001", "old_text": old,
                "new_text": new, "decision": decision, "evidence_ids": ["audio:s000001"],
                "reason": "局部聽證確認。"}]}

    def notes(self):
        return {"run_id": self.run_id, "transcript_sha256": cli._json_hash(cli._base(self.run)), "chapters": [
            {"id": "chapter-0001", "title": "第一章", "summary": "第一章摘要。",
             "bullets": [{"text": "第一章重點。", "segment_ids": ["s000001"], "frame_ids": []}],
             "visual_notes": [], "needs_review": []},
            {"id": "chapter-0002", "title": "第二章", "summary": "第二章摘要。",
             "bullets": [{"text": "第二章重點。", "segment_ids": ["s000002"], "frame_ids": []}],
             "visual_notes": [], "needs_review": []}]}

    def visual_cards(self):
        return {"run_id": self.run_id, "visuals_sha256": cli._json_hash(cli._read(self.run / "visuals.json")),
                "cards": [{"id": "screen-001", "kind": "chart", "title": "命盤畫面",
                           "frame_ids": ["f000001"], "verbatim_text": "年柱：甲子\n月柱：乙丑",
                           "structure": "| 欄位 | 年柱 | 月柱 |\n|---|---|---|\n| 天干 | 甲 | 乙 |",
                           "uncertainties": [], "reviewed": True},
                          {"id": "screen-002", "kind": "caption", "title": "第二段字幕",
                           "frame_ids": ["f000002"], "verbatim_text": "字幕原文。", "structure": "字幕位於底部。",
                           "uncertainties": [], "reviewed": True}]}

    def test_status_shows_the_device_speech_recognition_ran_on(self):
        self.run_process()
        self.assertEqual(cli.compact.status_view(cli._read(self.run / "state.json"))["asr_device"], "cpu")

    def test_process_announces_its_estimate_and_each_stage_on_stderr(self):
        import contextlib
        import io
        lines = io.StringIO()
        with contextlib.redirect_stderr(lines):
            self.run_process()
        text = lines.getvalue().splitlines()
        self.assertEqual(len(text), 3)
        self.assertIn("影片 5 分鐘", text[0])
        self.assertIn("依預設值", text[0])
        self.assertIn("最長單次等待", text[0])
        self.assertTrue(text[1].startswith("語音辨識完成"))
        self.assertTrue(text[2].startswith("抽圖與 OCR 完成"))
        with contextlib.redirect_stderr(io.StringIO()) as again:
            cli.process("source.mp4")
        self.assertEqual(again.getvalue(), "")

    def test_visual_omission_requires_review_and_separate_basis(self):
        self.run_process()
        document = self.visual_cards()
        document["cards"] = document["cards"][:1]
        omission = {"frame_ids": ["f000002"], "decision": "subtitle_indexed",
                    "reason": "字幕留在本機索引，已依代表畫面與逐字稿判斷不需重複收錄",
                    "basis_frame_ids": ["f000001"], "reviewed": False}
        document["omissions"] = [omission]
        with self.assertRaises(ValueError):
            cli.finalize_visuals(self.run, self.document(document))
        omission["reviewed"] = True
        cli.finalize_visuals(self.run, self.document(document))
        result = cli._read(self.run / "visual_review.json")
        self.assertEqual(result["coverage"]["omitted_frame_count"], 1)
        self.assertFalse(result["coverage"]["all_sampled_frames_reviewed"])

    def test_visual_omission_cannot_double_count_or_invent_basis(self):
        self.run_process()
        document = self.visual_cards()
        document["omissions"] = [{"frame_ids": ["f000002"], "decision": "decorative", "reason": "test",
                                  "basis_frame_ids": ["f000001"], "reviewed": True}]
        with self.assertRaises(ValueError):
            cli.finalize_visuals(self.run, self.document(document))
        document["cards"] = document["cards"][:1]
        document["omissions"][0]["basis_frame_ids"] = ["f000099"]
        with self.assertRaises(ValueError):
            cli.finalize_visuals(self.run, self.document(document))
        # A repeat must point at the transcribed frame it repeats ...
        document["omissions"][0].update(decision="redundant", basis_frame_ids=["f000002"])
        with self.assertRaises(ValueError):
            cli.finalize_visuals(self.run, self.document(document))
        # ... while a frame with nothing worth transcribing is judged from itself.
        document["omissions"][0]["decision"] = "decorative"
        cli.finalize_visuals(self.run, self.document(document))

    def decide_all(self, first_decision="keep"):
        patches = []
        for index, segment in enumerate(cli._base(self.run)["segments"]):
            patches.append({"segment_id": segment["id"], "old_text": segment["text"], "new_text": segment["text"],
                            "decision": first_decision if index == 0 else "keep",
                            "evidence_ids": ["audio:" + segment["id"]], "reason": "保留原句並記錄複核決議。"})
        return cli.apply(self.run, self.document({"run_id": self.run_id, "patches": patches}, "decisions.json"))

    def context_document(self, patches=None):
        # Legacy context assembly remains available only to a local, model-free
        # compatibility caller. Production model sessions use final_review.
        with patch.object(cli, "_review_task_id", return_value=None):
            cli._context_bundle(self.run, cli._read(self.run / "state.json"))
        bundle = cli._read(self.run / "context_bundle.json")
        return {"run_id": self.run_id, "patches": patches or [],
                "context_review": bundle["context_review_schema"]}

    def finish_context(self):
        return cli.apply(self.run, self.document(self.context_document(), "context-review.json"))

    def test_cached_process_preserves_evidence_and_regenerates_exports(self):
        result = self.run_process()
        self.assertEqual(result["status"], "awaiting_review")
        raw = self.run / "transcript.raw.json"
        original = raw.read_bytes()
        output = self.run / "exports/transcripts" / "raw.txt"
        output.unlink()
        again = cli.process("source.mp4")
        self.assertEqual(again["run_id"], self.run_id)
        self.assertEqual(self.calls, {"audio": 1, "asr": 1, "visuals": 1})
        self.assertEqual(raw.read_bytes(), original)
        self.assertTrue(output.is_file())
        normalized = self.run / "exports/transcripts" / "raw.zh-TW.txt"
        self.assertIn("軟體", normalized.read_text(encoding="utf-8"))
        self.assertIn("软件", cli._read(raw)["segments"][0]["text"])
        self.assertTrue((self.run / "visuals.md").is_file())
        self.assertFalse((self.root / "AI筆記與資料卡輸出").exists())

    def test_decision_assembly_preflight_and_apply_share_current_basis(self):
        self.run_process()
        original = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        with patch.object(cli, "_review_task_id", return_value=None):
            bundle = cli.decision_bundle(self.run, ["s000001"])
        decisions = {"run_id": self.run_id, "transcript_sha256": bundle["transcript_sha256"],
                     "decisions": [{"segment_id": "s000001", "new_text": "軟體正字",
                                    "decision": "correct", "evidence_ids": ["text:s000002"],
                                    "reason": "依相鄰文字判斷，不聲稱聽音。"}]}
        source = self.document(decisions, "minimal.json")
        target = self.root / "assembled.json"
        self.assertTrue(cli.assemble_patch(self.run, source, target)["ok"])
        checked = cli.preflight_patch(self.run, target)
        self.assertTrue(checked["ok"], checked["errors"])
        self.assertEqual(original, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})
        self.assertEqual(self.calls, {"audio": 1, "asr": 1, "visuals": 1})
        with self.assertRaises(FileExistsError):
            cli.assemble_patch(self.run, source, target)
        self.assertEqual(cli._read(target)["patches"][0]["old_text"], "软件错字")
        self.assertNotIn("context_review", cli._read(target))
        self.assertEqual(cli.apply(self.run, target)["applied"], 1)
        decisions["decisions"][0]["reason"] += " updated"
        stale = self.document(decisions, "stale.json")
        result = cli.assemble_patch(self.run, stale, self.root / "must-not-exist.json")
        self.assertFalse(result["ok"])
        self.assertFalse((self.root / "must-not-exist.json").exists())

    def test_hash_bound_patch_rejects_unrelated_changes_and_preflight_exit_code(self):
        self.run_process()
        document = self.correction()
        document["transcript_sha256"] = cli._json_hash(cli._base(self.run))
        # Modify another segment through the supported apply path.
        second = {"run_id": self.run_id, "patches": [{"segment_id": "s000002",
                  "old_text": "第二章有 3 個選項。", "new_text": "第二章有 4 個選項。",
                  "decision": "correct", "evidence_ids": ["text:s000001"], "reason": "合成測試"}]}
        cli.apply(self.run, self.document(second, "second.json"))
        stale_path = self.document(document, "stale-patch.json")
        before = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValueError, "stale"):
            cli.apply(self.run, stale_path)
        self.assertEqual(before, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(["preflight", str(self.run), str(stale_path)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_preflight_detects_visual_record_modified_outside_workflow(self):
        self.run_process()
        self.decide_all()
        cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        document = self.context_document()
        cards_path = self.run / "visual_review.json"
        record = cli._read(cards_path)
        record["cards"][0]["verbatim_text"] = "tampered"
        cli._write(cards_path, record)
        document["context_review"]["visual_review_sha256"] = cli._json_hash(record)
        target = self.document(document, "tampered-context.json")
        before = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        result = cli.preflight_patch(self.run, target)
        self.assertFalse(result["ok"])
        self.assertIn("outside finalize-visuals", str(result["errors"]))
        self.assertEqual(before, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})

    def test_single_target_inspection_asks_without_selecting_a_sibling(self):
        for name in ("課_360p.mp4", "課_720p.mp4", "別課.mp4"):
            (self.root / name).write_bytes(name.encode())
        with patch.object(cli.media, "probe", return_value={"duration": 310, "streams": [{"codec_type": "video"}]}) as probe:
            result = cli.inspect_targets(["課_360p.mp4"])
        self.assertEqual(result["status"], "needs_decision")
        self.assertEqual(result["targets"], ["課_360p.mp4"])
        self.assertEqual(result["possible_matches"][0]["paths"], ["課_360p.mp4", "課_720p.mp4"])
        self.assertEqual([f["path"] for f in result["files"] if f["selected"]], ["課_360p.mp4"])
        self.assertEqual(probe.call_count, 2)  # no unrelated video decoding or inspection
        self.assertNotIn("preferred", result)
        self.assertFalse((self.root / cli.locations.RUNS).exists())
        self.assertEqual(self.calls, {"audio": 0, "asr": 0, "visuals": 0})

    def test_assemble_review_candidate_does_not_change_completed_source(self):
        self.run_process()
        self.decide_all()
        cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        self.finish_context()
        baseline = {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        state = cli._read(self.run / "state.json")
        primary = cli._root_path(state["primary_transcript"])
        old_primary = primary.read_bytes()
        with patch.object(cli, "_review_task_id", return_value=None):
            basis = cli.review_plan_bundle(self.run)
        plan = {k: basis[k] for k in ("run_id", "transcript_sha256", "visual_review_sha256")}
        plan["decisions"] = [{"segment_id": "s000001", "decision": "correct", "new_text": "軟體正字",
                              "evidence_ids": ["text:s000002"], "reason": "合成校字決議"}]
        plan["layout"] = {"title": "候選", "sections": [{"title": "正文", "blocks": [
            {"card_id": "screen-001"}, {"start_segment_id": "s000001", "end_segment_id": "s000002",
                                      "punctuation": {"s000001": "，", "s000002": "。"}},
            {"card_id": "screen-002"}]}]}
        source = self.document(plan, "plan.json")
        target = self.root / "candidate"
        result = cli.assemble_review(self.run, source, target)
        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(result["applied"])
        self.assertTrue(cli._read(target / "manifest.json")["complete"])
        self.assertEqual(cli._read(target / "reading_layout.json")["sections"][0]["blocks"][1]["segment_ids"],
                         ["s000001", "s000002"])
        self.assertIn("軟體正字", (target / "preview.md").read_text(encoding="utf-8"))
        self.assertEqual(baseline, {p: p.read_bytes() for p in self.run.rglob("*") if p.is_file()})
        self.assertEqual(old_primary, primary.read_bytes())
        for name, digest in cli._read(target / "manifest.json")["files"].items():
            self.assertEqual(cli._hash(target / name), digest)
        with self.assertRaises(FileExistsError):
            cli.assemble_review(self.run, source, target)
        with self.assertRaisesRegex(ValueError, "outside transcription runs"):
            cli.assemble_review(self.run, source, self.run / "candidate")
        plan["visual_review_sha256"] = "stale"
        bad = self.document(plan, "stale-plan.json")
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["assemble-review", str(self.run), str(bad), "--output-dir", str(self.root / "bad")])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "bad").exists())

    def test_target_matches_require_both_name_and_duration(self):
        for name in ("課_360p.mp4", "課_720p.mp4", "別課.mp4", "課.wav"):
            (self.root / name).write_bytes(name.encode())
        durations = {"課_360p.mp4": 310, "課_720p.mp4": 312, "別課.mp4": 310, "課.wav": 310}
        probe = lambda ffprobe, source: {"duration": durations[source.name], "streams":
                                         [{"codec_type": "video"}] if source.suffix == ".mp4" else []}
        with patch.object(cli.media, "probe", side_effect=probe):
            self.assertEqual(cli.inspect_targets(list(durations))["possible_matches"], [])
            durations["課_720p.mp4"] = 311
            self.assertEqual(len(cli.inspect_targets(list(durations))["possible_matches"]), 1)
            durations["課_720p.mp4"] = 311.001
            self.assertEqual(cli.inspect_targets(list(durations))["status"], "ready")
            self.assertEqual(cli.inspect_targets(["課.wav"])["status"], "ready")

    def test_batch_inspection_preserves_explicit_order_and_compares_across_directories(self):
        targets = ["乙/課.mp4", "甲/課.mp4"]
        for name in targets:
            path = self.root / name
            path.parent.mkdir()
            path.write_bytes(name.encode())
        output = io.StringIO()
        with patch.object(cli.media, "probe", return_value={"duration": 310, "streams": [{"codec_type": "video"}]}), redirect_stdout(output):
            self.assertEqual(cli.main(["inspect-targets", *targets]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["targets"], targets)
        self.assertEqual(result["status"], "needs_decision")
        self.assertEqual(len(result["possible_matches"]), 1)
        self.assertFalse((self.root / cli.locations.RUNS).exists())

    def test_invalid_target_lists_do_not_start_work(self):
        for targets in ([], ["source.mp4", "./source.mp4"]):
            with self.assertRaises(ValueError):
                cli.inspect_targets(targets)
        with self.assertRaises(FileNotFoundError):
            cli.inspect_targets(["source.mp4", "missing.mp4"])
        with patch.object(cli.media, "probe", side_effect=cli.media.MediaError("invalid media")):
            with self.assertRaises(cli.media.MediaError):
                cli.inspect_targets(["source.mp4"])
        self.assertFalse((self.root / cli.locations.RUNS).exists())

    def test_process_honors_selected_resolution_without_inspecting_other_targets(self):
        for name in ("課_360p.mp4", "課_720p.mp4"):
            (self.root / name).write_bytes(name.encode())
        with patch.object(cli.media, "probe", return_value={"duration": 310, "streams": []}) as probe:
            for name in ("課_360p.mp4", "課_720p.mp4"):
                result = cli.process(name)
                self.assertEqual(result["status"], "awaiting_review")
                self.assertEqual(cli._read(Path(result["run_dir"]) / "state.json")["source"], name)
        self.assertEqual([call.args[1].name for call in probe.call_args_list], ["課_360p.mp4", "課_720p.mp4"])
        self.assertEqual(self.calls, {"audio": 2, "asr": 2, "visuals": 2})

    def test_tampered_evidence_is_not_silently_overwritten(self):
        self.run_process()
        (self.run / "audio.wav").write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "雜湊"):
            cli.process("source.mp4")
        self.assertEqual(self.calls["asr"], 1)
        self.assertEqual(cli._read(self.run / "state.json")["status"], "failed")

    def test_failed_stage_resumes_without_redoing_audio(self):
        with patch.object(cli.media, "transcribe", side_effect=RuntimeError("interrupted GPU")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                cli.process("source.mp4")
        state_path = next((self.root / cli.locations.RUNS).glob("*/state.json"))
        self.assertEqual(cli._read(state_path)["stages"]["asr"]["status"], "failed")
        self.run_process()
        self.assertEqual(self.calls["audio"], 1)
        self.assertEqual(cli._read(self.run / "state.json")["stages"]["asr"]["attempt"], 2)

    def test_patch_rejects_wrong_run_and_old_text_without_changing_raw(self):
        self.run_process()
        before = (self.run / "transcript.raw.json").read_bytes()
        wrong = self.correction()
        wrong["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run_id"):
            cli.apply(self.run, self.document(wrong))
        with self.assertRaisesRegex(ValueError, "old_text"):
            cli.apply(self.run, self.document(self.correction(old="wrong original")))
        self.assertEqual((self.run / "transcript.raw.json").read_bytes(), before)
        self.assertFalse((self.run / "review_state.json").exists())

    def test_sequential_patches_accumulate_and_duplicate_is_idempotent(self):
        self.run_process()
        first = self.document(self.correction(), "first.json")
        cli.apply(self.run, first)
        second = self.document(self.correction("軟體正字", "軟體確認"), "second.json")
        cli.apply(self.run, second)
        self.assertEqual(cli.apply(self.run, second)["status"], "already_applied")
        record = cli._read(self.run / "review_state.json")
        self.assertEqual(len(record["audit"]), 2)
        self.assertEqual(record["transcript"]["segments"][0]["text"], "軟體確認")
        self.assertEqual(cli._read(self.run / "transcript.raw.json")["segments"][0]["text"], "软件错字")

    def test_finalize_rejects_missing_chapter_unknown_evidence_and_missing_refs(self):
        self.run_process()
        for scenario in ("missing", "unknown", "empty"):
            notes = self.notes()
            if scenario == "missing":
                notes["chapters"].pop()
            elif scenario == "unknown":
                notes["chapters"][0]["bullets"][0]["frame_ids"] = ["f999999"]
            else:
                notes["chapters"][0]["bullets"][0]["segment_ids"] = []
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                cli.finalize(self.run, self.document(notes))
        self.assertFalse((self.run / "exports/legacy-notes" / "講義.md").exists())

    def test_finalize_marks_unresolved_and_later_patch_stales_notes(self):
        self.run_process()
        old_notes = self.notes()
        result = cli.finalize(self.run, self.document(old_notes))
        self.assertEqual(result["status"], "awaiting_review")
        self.assertTrue(result["legacy_notes_export"])
        output = Path(result["output"]).read_text(encoding="utf-8")
        self.assertIn("未解疑點", output)
        self.assertNotIn(str(self.root), output)
        cli.apply(self.run, self.document(self.correction()))
        self.assertTrue(cli._read(self.run / "state.json")["notes"]["stale"])
        with self.assertRaisesRegex(ValueError, "transcript_sha256"):
            cli.finalize(self.run, self.document(old_notes))

    def test_manual_needs_review_outside_triage_remains_visible(self):
        self.run_process()
        # Simulate the valid outcome of a detector that did not flag the first segment.
        triage = cli._read(self.run / "triage.json")
        triage["suspects"] = [item for item in triage["suspects"] if item["segment_id"] != "s000001"]
        cli._write(self.run / "triage.json", triage)
        document = self.correction(new="软件错字", decision="needs_review")
        document["patches"][0]["reason"] = "人工發現的額外疑點。"
        result = cli.apply(self.run, self.document(document))
        self.assertIn("s000001", result["review"]["unresolved_ids"])
        items = cli._read(self.run / "review_bundle.json")["items"]
        suspect = next(item for item in items if item["segment_id"] == "s000001")
        self.assertTrue(any(reason.get("detail") == "人工發現的額外疑點。" for reason in suspect["reasons"]))

    def test_workspace_move_retains_cache_and_portable_frame_links(self):
        self.run_process()
        moved = self.root / "moved"
        shutil.copytree(self.root, moved, ignore=shutil.ignore_patterns("moved"))
        with patch.object(cli, "ROOT", moved):
            result = cli.process("source.mp4")
            self.assertEqual(result["run_id"], self.run_id)
            self.assertEqual(self.calls["asr"], 1)
            frame = cli._read(Path(result["run_dir"]) / "visuals.json")["frames"][0]
            self.assertFalse(Path(frame["path"]).is_absolute())
            self.assertTrue(cli._frame_path(Path(result["run_dir"]), frame).is_file())

    def test_reading_packets_preserve_source_and_unicode_paths(self):
        self.run_process()
        packet = (self.run / "review_packet.md").read_text(encoding="utf-8")
        self.assertIn("软件错字", packet)
        self.assertIn("audio:s000001", packet)
        self.assertIn("畫面-1.png", packet)
        self.assertNotIn('"tokens":', packet)
        self.assertFalse((self.run / "manifest.json").exists())
        manifest = cli._chapters(self.run, cli._read(self.run / "state.json"))
        for entry in manifest["chapters"]:
            reading = self.run / entry["reading_path"]
            self.assertTrue(reading.is_file())
            self.assertEqual(cli._hash(reading), entry["reading_sha256"])
            content = reading.read_text(encoding="utf-8")
            self.assertIn(manifest["transcript_sha256"], content)
            for segment in cli._base(self.run)["segments"]:
                if segment["id"] in entry["segment_ids"]:
                    self.assertIn(segment["text"], content)
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(cli.main(["review-bundle", str(self.run)]), 1)
        self.assertIn("disabled in model sessions", json.loads(output.getvalue())["error"])
        output = io.StringIO()
        with patch.object(cli, "_review_task_id", return_value=None), redirect_stdout(output):
            self.assertEqual(cli.main(["review-bundle", str(self.run)]), 0)
        response = json.loads(output.getvalue())
        self.assertIn("review_packet", response)
        self.assertIn("stats", response)
        self.assertNotIn("items", response)

    def test_visual_cards_reject_stale_incomplete_duplicate_and_unreviewed_sources(self):
        self.run_process()
        for scenario in ("stale", "missing", "duplicate", "unknown", "unreviewed"):
            document = self.visual_cards()
            if scenario == "stale":
                document["visuals_sha256"] = "wrong-version"
            elif scenario == "missing":
                document["cards"].pop()
            elif scenario == "duplicate":
                document["cards"][1]["frame_ids"] = ["f000001", "f000002"]
            elif scenario == "unknown":
                document["cards"][0]["frame_ids"] = ["f999999"]
            else:
                document["cards"][0]["reviewed"] = False
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                cli.finalize_visuals(self.run, self.document(document))
        self.assertFalse((self.run / "exports/visuals").exists())

    def test_visual_completion_requires_asr_decisions_and_keeps_uncertainties(self):
        self.run_process()
        raw_hash = cli._hash(self.run / "transcript.raw.json")
        ocr_hash = cli._hash(self.run / "visuals.json")
        document = self.visual_cards()
        document["cards"][0]["uncertainties"] = ["右上角小字仍不可辨認。"]
        result = cli.finalize_visuals(self.run, self.document(document))
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["uncertainty_count"], 1)
        decision_result = self.decide_all("needs_review")
        self.assertEqual(decision_result["review"]["pending_count"], 0)
        self.assertEqual(decision_result["status"], "awaiting_context_review")
        self.assertEqual(self.finish_context()["status"], "completed_with_unresolved")
        self.assertEqual(cli._hash(self.run / "transcript.raw.json"), raw_hash)
        self.assertEqual(cli._hash(self.run / "visuals.json"), ocr_hash)
        output = Path(result["woven_output"]).read_text(encoding="utf-8")
        self.assertIn("畫面補充：", output)
        self.assertIn("軟體錯字〔？〕", output)
        self.assertNotIn("查看", output)
        self.assertNotIn("口述｜", output)
        self.assertNotIn("編輯註", output)
        self.assertNotIn("](../../", output)
        self.assertNotIn("screen_id:", output)
        self.assertNotIn("screen-001", output)
        self.assertNotIn("f000001", output)
        self.assertNotIn("source:", output)
        self.assertNotIn("畫面補充另列", output)  # no note on how the text was made
        from video_workflow import reading
        for line in reading.traditional(document["cards"][0]["structure"]).splitlines():
            self.assertIn("> " + line, output)
        for segment in cli._base(self.run)["segments"]:
            self.assertIn(reading.traditional(segment["text"]), output)
        self.assertLess(output.index("軟體錯字"), output.index("第二章有 3 個選項。"))

    def test_visual_deliverables_follow_later_correction_without_requiring_notes(self):
        self.run_process()
        self.decide_all()
        self.assertEqual(cli._read(self.run / "state.json")["status"], "awaiting_visuals")
        result = cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        self.assertEqual(result["status"], "awaiting_context_review")
        self.assertEqual(Path(result["woven_output"]), self.root / "工作資料/輸出/逐字稿/source.md")
        self.assertEqual(self.finish_context()["status"], "completed")
        self.assertFalse((self.run / "notes.json").exists())
        corrected = cli.apply(self.run, self.document(self.correction()))
        self.assertEqual(corrected["status"], "awaiting_context_review")
        self.assertEqual(self.finish_context()["status"], "completed")
        self.assertIn("軟體正字", Path(result["woven_output"]).read_text(encoding="utf-8"))
        data = cli._read(Path(result["structured_output"]))
        self.assertEqual(data["cards"][0]["id"], "screen-001")
        self.assertEqual(data["cards"][0]["frame_ids"], ["f000001"])
        self.assertEqual(data["cards"][0]["frame_times"][0]["time"], 0)
        self.assertEqual(data["cards"][1]["frame_times"][0]["time"], 305)

    def test_user_edits_to_primary_are_accepted_snapshotted_and_recoverable(self):
        self.run_process()
        self.decide_all()
        result = cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        self.assertEqual(self.finish_context()["status"], "completed")
        primary = Path(result["woven_output"])
        edited = primary.read_text(encoding="utf-8").replace("第二章有 3 個選項。", "第二章有三個選項。")
        primary.write_text(edited, encoding="utf-8")
        history = self.run / "primary_history"
        # A hand edit with the same paragraph structure stays completed, not "awaiting visuals".
        state = cli._status(self.run, cli._read(self.run / "state.json"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["primary_user_edits"]["paragraphs"]), 1)
        self.assertEqual([p.read_text(encoding="utf-8") for p in history.glob("*.md")], [edited])
        cli._status(self.run, cli._read(self.run / "state.json"))
        self.assertEqual(len(list(history.glob("*.md"))), 1)
        # Cutting the last paragraph is accepted and recorded as removed.
        paragraphs = edited.rstrip("\n").split("\n\n")
        primary.write_text("\n\n".join(paragraphs[:-1]) + "\n", encoding="utf-8")
        state = cli._status(self.run, cli._read(self.run / "state.json"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(state["primary_user_edits"]["removed_paragraphs"]), 1)
        # Other broken structures are refused, and say so instead of blaming the visuals.
        primary.write_text(edited + "\n新加的一段。\n", encoding="utf-8")
        state = cli._status(self.run, cli._read(self.run / "state.json"))
        self.assertEqual(state["status"], "primary_structure_changed")
        self.assertIn("時間無法對應", state["message"])
        # A system re-export over user edits keeps them in history and records the replacement.
        primary.write_text(edited, encoding="utf-8")
        cli.apply(self.run, self.document(self.correction()))
        state = cli._read(self.run / "state.json")
        self.assertIn("primary_user_edits_replaced", state)
        self.assertNotIn("第二章有三個選項。", primary.read_text(encoding="utf-8"))
        saved = self.run / state["primary_user_edits_replaced"]["snapshot"]
        self.assertEqual(saved.read_text(encoding="utf-8"), edited)
        ledger = [json.loads(line) for line in (history / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(entry["reason"] for entry in ledger))

    def test_same_named_sources_keep_separate_stable_transcript_files(self):
        self.run_process()
        first = cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        first_path = Path(first["woven_output"])
        original = first_path.read_bytes()
        other = self.root / "other/source.mp4"
        other.parent.mkdir()
        other.write_bytes(b"different-source")
        second = cli.process(other)
        self.run, self.run_id = Path(second["run_dir"]), second["run_id"]
        cards = self.document(self.visual_cards(), "second-visuals.json")
        result = cli.finalize_visuals(self.run, cards)
        second_path = Path(result["woven_output"])
        self.assertEqual(second_path.name, "source__" + self.run_id + ".md")
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(cli.finalize_visuals(self.run, cards)["woven_output"], str(second_path))
        calls = dict(self.calls)
        resumed = cli.resume(self.run)
        self.assertEqual(resumed["primary_transcript"], second_path.relative_to(self.root).as_posix())
        self.assertEqual(self.calls, calls)
        self.assertEqual(first_path.read_bytes(), original)

    def test_context_pass_reaches_high_score_unflagged_words_and_preserves_raw(self):
        with patch.object(cli.media, "transcribe", return_value={"segments": [
                {"id": "s000001", "start": 0, "end": 1, "text": "人子", "tokens": [{"text": "人子", "p": 0.99}]},
                {"id": "s000002", "start": 1, "end": 2, "text": "這是壬水的例子", "tokens": []}]}):
            self.run_process()
        self.assertEqual(cli._read(self.run / "triage.json")["suspects"], [])
        raw_hash = cli._hash(self.run / "transcript.raw.json")
        cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        doc = self.context_document([{"segment_id": "s000001", "old_text": "人子", "new_text": "壬子",
              "decision": "correct", "evidence_ids": ["text:s000002"],
              "reason": "相鄰句已指明壬水，修正同音天干；未聲稱聽音。"}])
        bundle = cli._read(self.run / "context_bundle.json")
        self.assertEqual(len(bundle["segments"]), 2)
        self.assertNotIn("tokens", bundle["segments"][0])
        result = cli.apply(self.run, self.document(doc))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(cli._base(self.run)["segments"][0]["text"], "壬子")
        self.assertEqual(cli._hash(self.run / "transcript.raw.json"), raw_hash)
        self.assertEqual(self.calls["visuals"], 1)
        # A visual revision invalidates the previous semantic review.
        cards = self.visual_cards()
        cards["cards"][0]["verbatim_text"] += "\n新增可見文字"
        self.assertEqual(cli.finalize_visuals(self.run, self.document(cards))["status"], "awaiting_context_review")

    def test_context_pass_rejects_stale_or_incomplete_coverage_atomically(self):
        self.run_process()
        self.decide_all()
        cli.finalize_visuals(self.run, self.document(self.visual_cards()))
        before = (self.run / "review_state.json").read_bytes()
        for scenario in ("stale", "missing", "duplicate", "missing_card"):
            doc = self.context_document(self.correction()["patches"])
            if scenario == "stale":
                doc["context_review"]["transcript_sha256"] = "wrong"
            elif scenario == "missing":
                doc["context_review"]["reviewed_segment_ids"].pop()
            elif scenario == "duplicate":
                doc["context_review"]["reviewed_segment_ids"][1] = "s000001"
            else:
                doc["context_review"]["reviewed_card_ids"].pop()
            with self.subTest(scenario=scenario), self.assertRaisesRegex(ValueError, "context_review"):
                cli.apply(self.run, self.document(doc))
            self.assertEqual((self.run / "review_state.json").read_bytes(), before)

    def test_resume_existing_identity_after_cli_change_does_not_redo_media(self):
        code = self.root / "src/video_workflow/cli.py"
        code.parent.mkdir(parents=True)
        code.write_text("old CLI", encoding="utf-8")
        self.run_process()
        identity = cli._read(self.run / "state.json")["identity"]
        code.write_text("new visual workflow CLI", encoding="utf-8")
        with patch.object(cli, "_identity", side_effect=AssertionError("must not calculate new run identity")):
            result = cli.resume(self.run)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(self.calls, {"audio": 1, "asr": 1, "visuals": 1})
        self.assertEqual(cli._read(self.run / "state.json")["identity"], identity)
        (self.root / "source.mp4").write_bytes(b"different source")
        with self.assertRaisesRegex(RuntimeError, "影片已變更"):
            cli.resume(self.run)


if __name__ == "__main__":
    unittest.main()
