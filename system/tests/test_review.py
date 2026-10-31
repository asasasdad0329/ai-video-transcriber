import copy
import json
import tempfile
import unittest
from pathlib import Path

from video_workflow import review


def transcript():
    return {"run_id": "test-run", "segments": [
        {"id": "s1", "start": 0.0, "end": 5.0, "text": "這是開場。", "tokens": []},
        {"id": "s2", "start": 5.0, "end": 12.0, "text": "請安裝套件二十次。", "tokens": [{"text": "套件", "p": 0.3, "start": 6.0, "end": 7.0}]},
        {"id": "s3", "start": 12.0, "end": 20.0, "text": "下一步執行。", "tokens": []},
        {"id": "s4", "start": 20.0, "end": 25.0, "text": "謝謝觀看。", "tokens": []},
    ]}


def visuals():
    return {"frames": [{"id": "f1", "time": 8.0, "path": "frames/one.jpg", "text": "套件 安裝", "confidence": 0.95}, {"id": "f2", "time": 120.0, "path": "frames/two.jpg", "text": "其他資料", "confidence": 0.9}], "coverage": {}}


def patch(**changes):
    result = {"segment_id": "s2", "old_text": "請安裝套件二十次。", "new_text": "請安裝套件一次。", "decision": "correct", "evidence_ids": ["verified-audio-clip"], "reason": "The independently verified audio says 一次."}
    result.update(changes)
    return result


class TriageTests(unittest.TestCase):
    def test_multiple_signals_leave_source_unchanged(self):
        source = transcript()
        original = copy.deepcopy(source)
        result = review.triage(source, visuals(), {}, [])
        self.assertEqual(source, original)
        self.assertEqual([item["segment_id"] for item in result["suspects"]], ["s2"])
        self.assertIn("low_asr_score", result["suspects"][0]["reasons"])
        self.assertIn("numbers_need_verification", result["suspects"][0]["reasons"])
        self.assertEqual(result["suspects"][0]["evidence_ids"], ["f1"])

    def test_repetition_and_missing_scores(self):
        source = transcript()
        source["segments"][1]["text"] = "這個部分這個部分這個部分"
        source["segments"][1]["tokens"] = [{"text": "missing"}, {"p": float("nan")}, {"p": -1}]
        result = review.triage(source, {"frames": []}, {"review_numbers": False}, [])
        self.assertEqual(result["suspects"][0]["reasons"], ["repeated_text"])

    def test_whisper_cpp_timestamp_is_not_a_low_confidence_word(self):
        source = {"segments": [{"id": "s000001", "start": 0.0, "end": 3.34,
                                "text": "大家好歡迎上課。", "tokens": [
                                    {"text": "大家", "p": 0.99, "start": 0.0, "end": 0.6},
                                    {"text": "[_TT_167]", "p": 0.38, "start": 3.34, "end": 3.34}]}]}
        original = copy.deepcopy(source)
        result = review.triage(source, {"frames": []}, {}, [])
        self.assertEqual(result["suspects"], [])
        self.assertEqual(result["stats"]["ignored_low_score_nontext_token_count"], 1)
        self.assertEqual(source, original)

    def test_special_and_punctuation_tokens_do_not_hide_real_low_score_words(self):
        controls = ["[_SOT_]", "[_EOT_]", "[_TT_167]", "<|startoftranscript|>", "<|3.34|>", "<|zh|>", " ", "。", "�"]
        source = {"segments": [{"id": "s", "start": 0, "end": 3, "text": "檢查GPU套件。", "tokens": [{"text": text, "p": 0.1} for text in controls] + [{"text": "套件", "p": 0.3}, {"text": "[GPU]", "p": 0.2}]}]}
        result = review.triage(source, {"frames": []}, {}, [])
        self.assertEqual(result["suspects"][0]["reasons"], ["low_asr_score"])
        self.assertEqual([token["text"] for token in result["suspects"][0]["low_score_tokens"]], ["套件", "[GPU]"])
        self.assertEqual(result["stats"]["ignored_low_score_nontext_token_count"], len(controls))

    @unittest.skipIf(review.lazy_pinyin is None or review._SCRIPT_COMPARISON is None, "optional pypinyin/rapidfuzz/opencc unavailable")
    def test_phonetic_candidate_is_only_a_suggestion(self):
        source = {"segments": [{"id": "s", "start": 1, "end": 2, "text": "研究量子糾產", "tokens": []}]}
        result = review.triage(source, {"frames": []}, {}, ["量子糾纏"])
        candidates = result["suspects"][0]["candidates"]
        self.assertTrue(any(candidate["suggestion"] == "量子糾纏" for candidate in candidates))
        self.assertFalse(candidates[0]["auto_apply"])
        self.assertEqual(source["segments"][0]["text"], "研究量子糾產")

    @unittest.skipIf(review.lazy_pinyin is None or review._SCRIPT_COMPARISON is None, "optional pypinyin/rapidfuzz/opencc unavailable")
    def test_script_conversion_is_not_reported_as_asr_correction(self):
        source = {"segments": [{"id": "s", "start": 1, "end": 2, "text": "這台电脑", "tokens": []}]}
        result = review.triage(source, {"frames": []}, {}, ["電腦"])
        self.assertEqual(result["suspects"], [])
        self.assertEqual(source["segments"][0]["text"], "這台电脑")

    def test_bundle_uses_only_neighbors_and_real_nearby_evidence(self):
        source = transcript()
        result = review.triage(source, visuals(), {}, [])
        bundle = review.make_review_bundle(source, visuals(), result, "run")
        self.assertEqual(bundle["items"][0]["previous"]["id"], "s1")
        self.assertEqual(bundle["items"][0]["next"]["id"], "s3")
        self.assertEqual(bundle["evidence_ids"], ["f1"])
        self.assertEqual(bundle["stats"]["context_segment_count"], 3)
        self.assertIsNone(bundle["stats"]["token_count"])
        self.assertFalse(bundle["evidence"][0]["audio_verified"])

    def test_bundle_rejects_invented_evidence(self):
        result = {"suspects": [{"segment_id": "s2", "evidence_ids": ["imaginary"]}]}
        with self.assertRaisesRegex(ValueError, "unknown suspect evidence"):
            review.make_review_bundle(transcript(), visuals(), result, "run")

    def test_bundle_keeps_only_text_excerpts_and_required_frame_evidence(self):
        source = transcript()
        frames = visuals()
        for segment in source["segments"]:
            segment["debug_payload"] = "do-not-inline" * 1000
            segment["tokens"].extend([{"text": "正常", "p": 0.99, "debug": "large" * 100}] * 100)
        frames["frames"][0].update({"boxes": [[0, 1, 2, 3]] * 10000,
                                     "pixel_sha256": "unused-for-review",
                                     "ocr_evidence_time": 8.0})
        original = copy.deepcopy(source)
        triaged = review.triage(source, frames, {}, [])
        bundle = review.make_review_bundle(source, frames, triaged, "run")
        item = bundle["items"][0]
        for excerpt in (item["segment"], item["previous"], item["next"]):
            self.assertEqual(set(excerpt), {"id", "start", "end", "text"})
        self.assertEqual(item["low_score_tokens"], [{"text": "套件", "p": 0.3, "start": 6.0, "end": 7.0}])
        evidence = bundle["evidence"][0]
        self.assertNotIn("boxes", evidence)
        self.assertNotIn("pixel_sha256", evidence)
        self.assertEqual(evidence["path"], "frames/one.jpg")
        self.assertEqual(evidence["ocr_evidence_time"], 8.0)
        self.assertLess(len(json.dumps(bundle, ensure_ascii=False)), 5000)
        self.assertEqual(source, original)
        chapters = review.make_chapter_bundles(source, frames)
        for chapter in chapters:
            for segment in chapter["segments"]:
                self.assertEqual(set(segment), {"id", "start", "end", "text"})
            for frame in chapter["frames"]:
                self.assertNotIn("boxes", frame)
                self.assertNotIn("pixel_sha256", frame)
        self.assertLess(len(json.dumps(chapters, ensure_ascii=False)), 5000)


class PatchTests(unittest.TestCase):
    def test_valid_patch_and_pending_review_are_auditable(self):
        source = transcript()
        original = copy.deepcopy(source)
        pending = patch(segment_id="s3", old_text="下一步執行。", new_text="下一步執行。", decision="needs_review", evidence_ids=[], reason="Audio evidence unavailable.")
        result, audit = review.apply_patches(source, {"run_id": "test-run", "patches": [patch(), pending]}, {"verified-audio-clip"})
        self.assertEqual(source, original)
        self.assertEqual(result["segments"][1]["text"], "請安裝套件一次。")
        self.assertFalse(result["segments"][1]["token_alignment_matches_text"])
        self.assertEqual(result["segments"][1]["tokens"], source["segments"][1]["tokens"])
        self.assertEqual([entry["applied"] for entry in audit], [True, False])

    def test_invalid_batch_never_changes_original(self):
        bad_patches = [
            patch(segment_id="unknown"), patch(old_text="近似原文"),
            patch(evidence_ids=[]), patch(evidence_ids=["invented"]),
            patch(evidence_ids=["verified-audio-clip", "verified-audio-clip"]),
            patch(reason=" "), patch(decision="rewrite"), patch(new_text=""),
            patch(decision=[]),
            patch(decision="keep"), patch(segment_id=True),
        ]
        for bad in bad_patches:
            with self.subTest(bad=bad):
                source = transcript()
                original = copy.deepcopy(source)
                with self.assertRaises(ValueError):
                    review.apply_patches(source, {"patches": [bad]}, {"verified-audio-clip"})
                self.assertEqual(source, original)
        source = transcript()
        original = copy.deepcopy(source)
        with self.assertRaises(ValueError):
            review.apply_patches(source, {"patches": [patch(), patch()]}, {"verified-audio-clip"})
        self.assertEqual(source, original)

    def test_later_invalid_patch_blocks_earlier_valid_patch(self):
        source = transcript()
        original = copy.deepcopy(source)
        with self.assertRaises(ValueError):
            review.apply_patches(source, {"patches": [patch(), patch(segment_id="not-a-segment")]}, {"verified-audio-clip"})
        self.assertEqual(source, original)

    def test_run_identity_must_match(self):
        with self.assertRaisesRegex(ValueError, "run_id"):
            review.apply_patches(transcript(), {"run_id": "different", "patches": []}, set())


class ExportAndChapterTests(unittest.TestCase):
    def test_export_preserves_script_and_valid_timestamps(self):
        source = {"segments": [{"id": "x", "start": 59.9996, "end": 3661.025, "text": "软件與軟體皆保留。"}]}
        with tempfile.TemporaryDirectory() as directory:
            paths = review.export_transcripts(source, Path(directory))
            self.assertEqual(Path(paths["txt"]).read_text(encoding="utf-8"), "软件與軟體皆保留。\n")
            srt = Path(paths["srt"]).read_text(encoding="utf-8")
            self.assertIn("00:01:00,000 --> 01:01:01,025", srt)
            self.assertIn("软件與軟體皆保留。", srt)

    def test_invalid_timestamps_fail_before_writing(self):
        for start, end in [(-1, 1), (2, 1), (0, float("nan")), (0, float("inf"))]:
            with self.subTest(start=start, end=end), tempfile.TemporaryDirectory() as directory:
                source = {"segments": [{"id": "x", "start": start, "end": end, "text": "x"}]}
                with self.assertRaises(ValueError):
                    review.export_transcripts(source, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_chapters_cover_crossing_segments_and_boundary_frames(self):
        source = {"segments": [{"id": "crossing", "start": 290, "end": 310, "text": "跨章句"}, {"id": "last", "start": 500, "end": 600, "text": "尾句"}, {"id": "point", "start": 600, "end": 600, "text": "結尾註記"}]}
        frames = {"frames": [{"id": "a", "time": 0}, {"id": "b", "time": 300}, {"id": "c", "time": 600}]}
        chapters = review.make_chapter_bundles(source, frames)
        self.assertEqual(len(chapters), 2)
        self.assertEqual({segment["id"] for chapter in chapters for segment in chapter["segments"]}, {"crossing", "last", "point"})
        self.assertEqual([frame["id"] for chapter in chapters for frame in chapter["frames"]], ["a", "b", "c"])
        self.assertTrue(all(chapter["segments"][0]["id"] == "crossing" for chapter in chapters))
        self.assertTrue(all(chapter["status"] == "source_bundle_only" for chapter in chapters))

    def test_zero_length_sources_and_empty_sources(self):
        self.assertEqual(review.make_chapter_bundles({"segments": []}, {"frames": []}), [])
        chapters = review.make_chapter_bundles({"segments": []}, {"frames": [{"id": "zero", "time": 0}]})
        self.assertEqual(chapters[0]["coverage"]["frame_ids"], ["zero"])
        with self.assertRaises(ValueError):
            review.make_chapter_bundles(transcript(), visuals(), 0)


if __name__ == "__main__":
    unittest.main()
