"""Synthetic fixtures only: read-only review assembly and contract parity."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import preflight, review


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"run_id": "synthetic", "segments": [
            {"id": "a", "start": 0, "end": 1, "text": "量子糾產"},
            {"id": "b", "start": 1, "end": 2, "text": "繼續說明"},
        ]}
        digest = hashlib.sha256(json.dumps(self.transcript, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.bundle = {"run_id": "synthetic", "transcript_sha256": digest,
                       "segments": copy.deepcopy(self.transcript["segments"]),
                       "evidence": [{"id": "text:b"}, {"id": "f1"}]}
        self.decisions = {"run_id": "synthetic", "transcript_sha256": digest,
            "decisions": [{"segment_id": "a", "decision": "correct", "new_text": "量子糾纏",
                "evidence_ids": ["text:b"], "reason": "後文使用同一術語，依上下文推論。"}]}
        self.layout = {"title": "合成測試", "sections": [{"title": "內容", "blocks": [
            {"segment_ids": ["a", "b"], "punctuation": {"b": "。"}}]}]}

    def assembled(self):
        result = preflight.assemble(self.bundle, self.decisions)
        self.assertTrue(result["ok"], result["errors"])
        return result["patch_document"]

    def test_assembly_preserves_hash_and_does_not_claim_review(self):
        self.decisions["decisions"].append({"segment_id": "b", "decision": "needs_review",
            "evidence_ids": [], "reason": "沒有足夠證據排除歧義。"})
        original = copy.deepcopy((self.bundle, self.decisions))
        document = self.assembled()
        self.assertEqual(set(document), {"run_id", "transcript_sha256", "patches"})
        self.assertEqual(document["patches"][0]["old_text"], "量子糾產")
        self.assertEqual(document["patches"][1]["new_text"], "繼續說明")
        self.assertEqual((self.bundle, self.decisions), original)
        self.assertTrue(preflight.preflight(self.transcript, self.bundle, document)["ok"])

    def test_stale_decisions_and_stale_bundle_are_rejected(self):
        self.decisions["transcript_sha256"] = "outdated"
        result = preflight.assemble(self.bundle, self.decisions)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["patch_document"])
        self.assertIn("transcript_sha256", str(result["errors"]))
        self.decisions["transcript_sha256"] = self.bundle["transcript_sha256"]
        document = self.assembled()
        self.transcript["segments"][1]["text"] = "已經更新"
        result = preflight.preflight(self.transcript, self.bundle, document)
        self.assertFalse(result["ok"])
        self.assertIn("stale", str(result["errors"]))

    def test_accumulates_independent_errors_in_one_decision_and_batch(self):
        entry = self.decisions["decisions"][0]
        entry.update(new_text="量子糾產", reason=" ", evidence_ids=["invented"], audio_verified=True)
        self.decisions["run_id"] = "different-run"
        self.decisions["decisions"] += [copy.deepcopy(entry), {"segment_id": [], "decision": {},
            "new_text": 123, "evidence_ids": {}, "reason": False}, "bad"]
        result = preflight.assemble(self.bundle, self.decisions)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["patch_document"])
        errors = str(result["errors"])
        for text in ("unknown field", "stale", "nonempty change", "nonempty reason",
                     "evidence ids", "duplicate", "unknown decision", "must be an object"):
            self.assertIn(text, errors)

    def test_keep_cannot_change_text_and_correct_requires_text_and_evidence(self):
        for changes in ({"decision": "keep"}, {"new_text": ""}, {"evidence_ids": []},
                        {"evidence_ids": ["f1", "f1"]}, {"new_text": None}):
            with self.subTest(changes=changes):
                document = copy.deepcopy(self.decisions)
                document["decisions"][0].update(changes)
                self.assertFalse(preflight.assemble(self.bundle, document)["ok"])
        del self.decisions["decisions"][0]["new_text"]
        self.assertFalse(preflight.assemble(self.bundle, self.decisions)["ok"])

    def test_parity_with_existing_apply_validation(self):
        document = self.assembled()
        for changes in ({}, {"old_text": "wrong"}, {"new_text": "量子糾產"},
                        {"decision": "keep"}, {"reason": ""}, {"evidence_ids": []},
                        {"segment_id": "missing"}, {"evidence_ids": ["f1", "f1"]}):
            with self.subTest(changes=changes):
                candidate = copy.deepcopy(document)
                candidate["patches"][0].update(changes)
                try:
                    review.apply_patches(self.transcript, candidate, {"text:b", "f1"})
                    expected = True
                except ValueError:
                    expected = False
                result = preflight.preflight(self.transcript, self.bundle, candidate)
                self.assertEqual(result["ok"], expected, result)

    def test_preflight_preserves_every_input_and_accepts_valid_layout(self):
        document = self.assembled()
        before = copy.deepcopy((self.transcript, self.bundle, document, self.layout))
        result = preflight.preflight(self.transcript, self.bundle, document, self.layout, [])
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.transcript, self.bundle, document, self.layout), before)

    def test_multiple_layout_errors_include_punctuation_order_and_card_coverage(self):
        self.layout["sections"][0]["blocks"] = [
            {"segment_ids": ["b", "a"], "punctuation": {"b": "補寫文字", "outside": "「"}},
            {"segment_ids": ["a"], "punctuation": {"a": 3}},
            {"card_id": "unknown"}]
        result = preflight.preflight(self.transcript, self.bundle, self.assembled(), self.layout,
            [{"id": "c", "title": "圖卡", "verbatim_text": "測試"}])
        self.assertFalse(result["ok"])
        errors = str(result["errors"])
        for text in ("only punctuation", "outside paragraph", "original order", "every visual", "unknown card"):
            self.assertIn(text, errors)

    def test_bundle_and_bad_root_types_fail_without_throwing(self):
        for bundle, decisions in ((None, self.decisions), (self.bundle, []),
                                  ({**self.bundle, "evidence": [123]}, self.decisions)):
            with self.subTest(bundle=bundle, decisions=decisions):
                self.assertFalse(preflight.assemble(bundle, decisions)["ok"])
        document = self.assembled()
        self.assertFalse(preflight.preflight(None, self.bundle, document)["ok"])
        self.assertFalse(preflight.preflight(self.transcript, None, document)["ok"])
        self.assertFalse(preflight.preflight(self.transcript, self.bundle, [document])["ok"])

    def test_explicit_context_review_requires_matching_basis_and_full_coverage(self):
        document = self.assembled()
        document["context_review"] = {"transcript_sha256": self.bundle["transcript_sha256"],
            "visual_review_sha256": "visual-hash", "reviewed_segment_ids": ["a", "b"],
            "reviewed_card_ids": []}
        self.assertFalse(preflight.preflight(self.transcript, self.bundle, document)["ok"])
        self.bundle["visual_review_sha256"] = "visual-hash"
        self.assertTrue(preflight.preflight(self.transcript, self.bundle, document)["ok"])


if __name__ == "__main__":
    unittest.main()
