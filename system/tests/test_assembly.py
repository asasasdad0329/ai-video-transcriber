"""Synthetic review plans: exact coverage, source binding, and preview parity."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import assembly, reading, review


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"run_id": "synthetic", "segments": [
            {"id": "s000021", "start": 0, "end": 1, "text": "這是量子糾產"},
            {"id": "s000003", "start": 1, "end": 2, "text": "量子糾纏"},
            {"id": "tail", "start": 2, "end": 3, "text": "還有疑問"},
        ]}
        digest = hashlib.sha256(json.dumps(self.transcript, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.cards = [{"id": "card-one", "title": "原始畫面", "verbatim_text": "原字 X",
            "structure": "甲 → 乙", "uncertainties": ["小字不清"], "reviewed": True}]
        self.bundle = {"run_id": "synthetic", "transcript_sha256": digest,
            "visual_review_sha256": "current-visual-hash", "segments": copy.deepcopy(self.transcript["segments"]),
            "cards": copy.deepcopy(self.cards), "evidence_ids": ["text:s000003"]}
        self.plan = {"run_id": "synthetic", "transcript_sha256": digest,
            "visual_review_sha256": "current-visual-hash", "decisions": [{
                "segment_id": "s000021", "decision": "correct", "new_text": "這是量子糾纏",
                "evidence_ids": ["text:s000003"], "reason": "相鄰段落出現相同術語。"}],
            "layout": {"title": "測試逐字稿", "protected_terms": ["丑"], "sections": [
                {"title": "內容", "blocks": [
                    {"start_segment_id": "s000021", "end_segment_id": "tail",
                     "punctuation": {"s000003": "，", "tail": "。"}},
                    {"card_id": "card-one"}]}]}}
        self.prior = {"tail": {"decision": "needs_review", "reason": "仍有歧義。", "applied": False}}

    def compile(self):
        return assembly.compile_review(self.transcript, self.bundle, self.plan, self.cards, self.prior)

    def assert_failure(self, result, text=None):
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["errors"])
        for key in ("patch_document", "layout", "preview", "reading_map"):
            self.assertIsNone(result[key])
        if text:
            self.assertIn(text, str(result["errors"]))

    def test_nonconsecutive_ranges_follow_array_order_and_match_existing_renderer(self):
        result = self.compile()
        self.assertTrue(result["ok"], result)
        legacy = copy.deepcopy(self.plan["layout"])
        legacy["sections"][0]["blocks"][0] = {
            "segment_ids": ["s000021", "s000003", "tail"],
            "punctuation": {"s000003": "，", "tail": "。"}}
        corrected, audit = review.apply_patches(self.transcript, result["patch_document"], {"text:s000003"})
        decisions = {**self.prior, **{p["segment_id"]: p for p in audit}}
        preview, records = reading.render(corrected, self.cards, decisions, legacy)
        self.assertEqual(result["layout"], legacy)
        self.assertEqual((result["preview"], result["reading_map"]), (preview, records))
        self.assertIn("還有疑問〔？〕。", result["preview"])
        self.assertIn("這是量子糾纏", result["preview"])
        self.assertEqual(set(result["patch_document"]), {"run_id", "transcript_sha256", "patches"})

    def test_every_source_is_unchanged_and_results_do_not_alias_sources(self):
        originals = copy.deepcopy((self.transcript, self.bundle, self.plan, self.cards, self.prior))
        result = self.compile()
        self.assertTrue(result["ok"], result)
        result["layout"]["protected_terms"].append("新增")
        result["patch_document"]["patches"][0]["evidence_ids"].append("新增")
        result["reading_map"][0]["segment_ids"].append("新增")
        self.assertEqual((self.transcript, self.bundle, self.plan, self.cards, self.prior), originals)

    def test_unknown_reversed_missing_duplicate_and_out_of_order_ranges(self):
        original = copy.deepcopy(self.plan)
        variants = [
            [{"start_segment_id": "missing", "end_segment_id": "tail"}],
            [{"start_segment_id": "tail", "end_segment_id": "s000021"}],
            [{"start_segment_id": "s000003", "end_segment_id": "tail"}],
            [{"start_segment_id": "s000021", "end_segment_id": "tail"}] * 2,
            [{"start_segment_id": "tail", "end_segment_id": "tail"},
             {"start_segment_id": "s000021", "end_segment_id": "s000003"}],
            [{"start_segment_id": [], "end_segment_id": False}],
        ]
        for blocks in variants:
            with self.subTest(blocks=blocks):
                self.plan = copy.deepcopy(original)
                self.plan["layout"]["sections"][0]["blocks"] = blocks + [{"card_id": "card-one"}]
                self.assert_failure(self.compile())

    def test_card_coverage_and_nonboolean_structure_switch(self):
        for value in ("false", 0, 1, None, [], {}):
            with self.subTest(value=value):
                self.plan["layout"]["sections"][0]["blocks"][1]["include_structure"] = value
                self.assert_failure(self.compile(), "must be a boolean")
        blocks = self.plan["layout"]["sections"][0]["blocks"]
        blocks[1] = {"card_id": "card-one", "include_structure": False}
        self.assertTrue(self.compile()["ok"])
        blocks.append({"card_id": "card-one"})
        self.assert_failure(self.compile(), "every visual card")
        del blocks[1:]
        self.assert_failure(self.compile(), "every visual card")

    def test_strict_unknown_fields_at_every_plan_level(self):
        paths = [(), ("layout",), ("layout", "sections", 0),
                 ("layout", "sections", 0, "blocks", 0),
                 ("layout", "sections", 0, "blocks", 1), ("decisions", 0)]
        for path in paths:
            with self.subTest(path=path):
                plan = copy.deepcopy(self.plan)
                target = plan
                for key in path:
                    target = target[key]
                target["text"] = "未授權改寫"
                self.assert_failure(assembly.compile_review(self.transcript, self.bundle, plan), "unknown field")
        self.plan["layout"]["sections"][0]["blocks"][0]["card_id"] = "card-one"
        self.assert_failure(self.compile(), "unknown field")

    def test_punctuation_cannot_contain_text_or_apply_outside_range(self):
        for punctuation in ({"tail": "補充"}, {"tail": "「"}, {"tail": ""},
                            {"tail": False}, {"missing": "。"}, []):
            with self.subTest(punctuation=punctuation):
                self.plan["layout"]["sections"][0]["blocks"][0]["punctuation"] = punctuation
                self.assert_failure(self.compile())

    def test_version_binding_and_required_visual_basis(self):
        for key in ("run_id", "transcript_sha256", "visual_review_sha256"):
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan)
                plan[key] = "stale"
                self.assert_failure(assembly.compile_review(self.transcript, self.bundle, plan), "stale")
        self.transcript["segments"][0]["text"] = "來源已改"
        self.assert_failure(self.compile(), "stale")
        self.setUp()
        self.plan["visual_review_sha256"] = None
        self.assert_failure(self.compile(), "stale")
        self.bundle["visual_review_sha256"] = None
        self.assertTrue(self.compile()["ok"])
        del self.bundle["visual_review_sha256"]
        self.assert_failure(self.compile(), "required field")

    def test_new_explicit_decision_resolves_prior_uncertainty_only_for_that_segment(self):
        self.plan["decisions"].append({"segment_id": "tail", "decision": "keep",
            "evidence_ids": [], "reason": "明確保留原文。"})
        result = self.compile()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("還有疑問〔？〕", result["preview"])
        self.assertEqual(self.prior["tail"]["decision"], "needs_review")

    def test_bad_types_and_duplicate_source_cards_fail_closed(self):
        for transcript, bundle, plan in ((None, self.bundle, self.plan),
                                        (self.transcript, None, self.plan),
                                        (self.transcript, self.bundle, [])):
            self.assert_failure(assembly.compile_review(transcript, bundle, plan))
        for prior in ([], {"tail": "needs_review"}, {"unknown": {"decision": "keep"}}):
            self.assert_failure(assembly.compile_review(self.transcript, self.bundle, self.plan, prior_decisions=prior))
        self.cards.append(copy.deepcopy(self.cards[0]))
        self.assert_failure(self.compile(), "duplicate card")


if __name__ == "__main__":
    unittest.main()
