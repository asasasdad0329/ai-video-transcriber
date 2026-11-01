import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import cli, final_review as final, preflight, reading
import test_cli as fixtures


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated run fixture, not its test methods.
        self.fixture = fixtures.WorkflowContractTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        task_patch = patch.object(cli, "_review_task_id",
                                  side_effect=lambda explicit=None: explicit or "final-review-test")
        task_patch.start()
        self.addCleanup(task_patch.stop)
        self.fixture.run_process()
        self.run, self.root = self.fixture.run, self.fixture.root
        cli.finalize_visuals(self.run, self.fixture.document(self.fixture.visual_cards()))
        suspects = [item["segment_id"] for item in cli._read(self.run / "triage.json")["suspects"]]
        cli._write(self.run / "focused_review_tasks.json", {
            "version": 1, "run_id": self.fixture.run_id, "tasks": {"final-review-test": {
                "recorded_at": cli._now(), "segment_ids": None, "scope": None, "domain": None,
                "transcript_sha256": cli._json_hash(cli._base(self.run)),
                "triage_sha256": cli._hash(self.run / "triage.json"),
                "visuals_sha256": cli._json_hash(cli._read(self.run / "visuals.json")),
                "reviewed_segment_ids": suspects, "result_sha256": "test-focused-review"}}})
        self.draft = self.root / "candidate"

    def changes(self, data, name="changes.json"):
        return self.fixture.document(data, name)

    def start(self, data=None):
        data = copy.deepcopy(data or {})
        staged = {item["segment_id"] for item in data.get("edits", [])}
        decided = set(cli._record(self.run).get("decisions", {}))
        current = {segment["id"]: segment["text"] for segment in cli._base(self.run)["segments"]}
        for suspect in cli._read(self.run / "triage.json")["suspects"]:
            sid = suspect["segment_id"]
            if sid not in staged and sid not in decided:
                data.setdefault("edits", []).append({
                    "segment_id": sid, "new_text": current[sid], "decision": "keep",
                    "reason": "測試已完成局部複核並保留原文。", "evidence_ids": ["text:" + sid]})
        return final.start(self.run, self.draft,
                           self.changes(data) if data else None, task_id="final-review-test")

    def seal_and_read(self):
        final.seal(self.draft, task_id="final-review-test")
        return final.read_once(self.draft, task_id="final-review-test")

    def final_changes(self, edits=None, issues=None, name="final-changes.json", **extra):
        data = {"review_issues": issues or []}
        if edits is not None:
            data["edits"] = edits
        data.update(extra)
        return self.changes(data, name)

    def test_one_patch_one_preflight_one_export_and_no_repeat_read(self):
        baseline = (self.run / "transcript.raw.json").read_bytes()
        self.start({"edits": [{"segment_id": "s000001", "new_text": "軟體初稿"}]})
        self.assertFalse(list((self.run / "patches").glob("*.json")))
        candidate = self.seal_and_read()
        self.assertIn("軟體初稿", candidate["candidate"])
        self.assertNotIn("tokens", candidate)
        self.assertNotIn("candidate", final.read_once(self.draft, task_id="final-review-test"))
        changes = self.final_changes(
            edits=[{"segment_id": "s000001", "old_text": "軟體初稿", "new_text": "軟體正字"}],
            issues=[{"id": "wording-1", "kind": "text", "summary": "初稿仍有錯字。",
                     "resolution": "改為正字。", "segment_ids": ["s000001"]}])
        with patch.object(preflight, "preflight", wraps=preflight.preflight) as check, \
             patch.object(cli, "_refresh", wraps=cli._refresh) as publish:
            result = final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                                  task_id="final-review-test", changes_path=changes)
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(check.call_count, 1)
            self.assertEqual(publish.call_count, 1)
            retry = final.finish(self.draft)
            self.assertTrue(retry["already_committed"])
            self.assertEqual(check.call_count, 1)
            self.assertEqual(publish.call_count, 1)
        files = list((self.run / "patches").glob("*.json"))
        self.assertEqual(len(files), 1)
        document = cli._read(files[0])
        first = next(p for p in document["patches"] if p["segment_id"] == "s000001")
        self.assertEqual((first["old_text"], first["new_text"]), ("软件错字", "軟體正字"))
        self.assertEqual(document["context_review"]["method"], "sealed_candidate_once_then_single_issue_batch")
        self.assertEqual(baseline, (self.run / "transcript.raw.json").read_bytes())
        with self.assertRaises(ValueError):
            final.edit(self.draft, find="正字", replacement="又改")

    def test_card_correction_shares_single_finish_and_preserves_evidence(self):
        baseline = (self.run / "visual_review.json").read_bytes()
        self.start()
        candidate = self.seal_and_read()
        changes = self.final_changes(
            issues=[{"id": "visual-word", "kind": "visual", "summary": "畫面卡字誤讀。",
                     "resolution": "依原圖校字。", "card_ids": ["screen-002"]}],
            card_replacements={"screen-002": [{"find": "字幕原文", "replace": "字幕正字"}]})
        with patch.object(preflight, "preflight", wraps=preflight.preflight) as check, \
             patch.object(cli, "_refresh", wraps=cli._refresh) as publish:
            result = final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                                  task_id="final-review-test", changes_path=changes)
        self.assertTrue(result["ok"])
        self.assertEqual(check.call_count, 1)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(baseline, (self.run / "visual_review.json").read_bytes())
        output = cli._root_path(result["primary_transcript"]).read_text(encoding="utf-8")
        self.assertIn("字幕正字", output)
        self.assertNotIn("字幕原文", output)
        with self.assertRaisesRegex(ValueError, "already published"):
            final.read_once(self.draft, task_id="final-review-test")

    def test_sections_are_renamed_by_position_with_all_targets_found_first(self):
        self.start()
        titles = [section["title"] for section in final._load(self.draft)[1]["layout"]["sections"]]
        self.assertGreaterEqual(len(titles), 2)
        self.assertNotEqual(titles[0], titles[1])
        # A swap only works when every target is found before any rename; #N reaches shared titles.
        final.edit(self.draft, self.changes({"section_titles": {titles[0]: titles[1], titles[1]: titles[0]}},
                                            "swap.json"), task_id="final-review-test")
        final.edit(self.draft, self.changes({"section_titles": {"#1": "第一節"}}, "position.json"),
                   task_id="final-review-test")
        renamed = [section["title"] for section in final._load(self.draft)[1]["layout"]["sections"]]
        self.assertEqual(renamed[:2], ["第一節", titles[0]])
        with self.assertRaisesRegex(ValueError, "use #N"):
            final.edit(self.draft, self.changes({"section_titles": {"#99": "不存在"}}, "missing.json"),
                       task_id="final-review-test")

    def test_card_punctuation_fix_appears_as_written(self):
        cards = self.fixture.visual_cards()
        cards["cards"][1]["verbatim_text"] = "First， introduce the plan."  # OCR read a full-width comma
        cli.finalize_visuals(self.run, self.fixture.document(cards))
        self.start()
        candidate = self.seal_and_read()
        self.assertIn("First， introduce", candidate["candidate"])
        changes = self.final_changes(
            issues=[{"id": "visual-comma", "kind": "punctuation", "summary": "英文句中誤用全形逗號。",
                     "resolution": "改回半形逗號。", "card_ids": ["screen-002"]}],
            card_replacements={"screen-002": [{"find": "First， introduce", "replace": "First, introduce"}]})
        result = final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                              task_id="final-review-test", changes_path=changes)
        output = cli._root_path(result["primary_transcript"]).read_text(encoding="utf-8")
        self.assertIn("First, introduce the plan.", output)
        self.assertNotIn("First，", output)

    def test_card_replacement_that_changes_nothing_is_refused(self):
        self.start()
        candidate = self.seal_and_read()
        baseline = (self.draft / "draft.json").read_bytes()
        changes = self.final_changes(
            issues=[{"id": "visual-word", "kind": "visual", "summary": "畫面卡字誤讀。",
                     "resolution": "依原圖校字。", "card_ids": ["screen-002"]}],
            card_replacements={"screen-002": [{"find": "字幕原文", "replace": "字幕原文"}]})
        with patch.object(preflight, "preflight", wraps=preflight.preflight) as check:
            with self.assertRaisesRegex(ValueError, "leaves card screen-002 unchanged"):
                final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                             task_id="final-review-test", changes_path=changes)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(baseline, (self.draft / "draft.json").read_bytes())
        self.assertFalse(list((self.run / "patches").glob("*.json")))

    def test_new_reading_edition_is_titled_after_the_video(self):
        state = cli._read(self.run / "state.json")
        self.assertTrue(cli._root_path(state["primary_transcript"]).read_text(encoding="utf-8")
                        .startswith("# source\n"))
        self.start()
        self.assertEqual(cli._read(self.draft / "draft.json")["layout"]["title"], "source")
        self.assertEqual(cli._video_title({"source": "工作資料/原檔/示例课程#八字 #易经_720p.mp4"}), "示例课程")

    def test_invalid_card_correction_stops_before_preflight_or_mutation(self):
        self.start()
        candidate = self.seal_and_read()
        baseline = (self.draft / "draft.json").read_bytes()
        changes = self.final_changes(
            issues=[{"id": "visual-word", "kind": "visual", "summary": "不存在的文字。",
                     "resolution": "拒絕。", "card_ids": ["screen-002"]}],
            card_replacements={"screen-002": [{"find": "未出現的文字", "replace": "新字"}]})
        with patch.object(preflight, "preflight", wraps=preflight.preflight) as check:
            with self.assertRaisesRegex(ValueError, "match exactly once"):
                final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                             task_id="final-review-test", changes_path=changes)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(baseline, (self.draft / "draft.json").read_bytes())
        self.assertFalse(list((self.run / "patches").glob("*.json")))

    def test_attestation_revision_and_source_staleness_guards(self):
        self.start()
        final.seal(self.draft, task_id="final-review-test")
        changes = self.final_changes()
        with self.assertRaises(ValueError):
            final.finish(self.draft, reviewed=True, revision=0,
                         task_id="final-review-test", changes_path=changes)
        candidate = final.read_once(self.draft, task_id="final-review-test")
        with self.assertRaises(ValueError):
            final.finish(self.draft, reviewed=False, revision=candidate["revision"],
                         task_id="final-review-test", changes_path=changes)
        with self.assertRaises(ValueError):
            final.edit(self.draft, find="错字", replacement="正字", task_id="final-review-test")
        cli._write(self.run / "review_state.json", {"decisions": {}, "audit": [], "applied_documents": [], "other_change": True})
        with self.assertRaises(ValueError):
            final.finish(self.draft, reviewed=True, revision=candidate["revision"],
                         task_id="final-review-test", changes_path=changes)
        self.assertFalse(list((self.run / "patches").glob("*.json")))

    def test_prior_unresolved_is_not_auto_kept(self):
        self.fixture.decide_all(first_decision="needs_review")
        self.start()
        candidate = self.seal_and_read()
        self.assertIn("〔？〕", candidate["candidate"])
        result = final.finish(self.draft, True, candidate["revision"],
                              task_id="final-review-test", changes_path=self.final_changes())
        self.assertEqual(result["status"], "completed_with_unresolved")
        self.assertIn("s000001", result["unresolved_ids"])

    def test_partial_fix_keeps_its_mark_and_stays_unresolved(self):
        with self.assertRaisesRegex(ValueError, "partial fix"):
            self.start({"edits": [{"segment_id": "s000001", "decision": "needs_review", "new_text": "軟體錯字"}]})
        self.start({"edits": [{"segment_id": "s000001", "decision": "needs_review", "new_text": "軟體〔?〕錯字"}]})
        candidate = self.seal_and_read()
        self.assertIn("軟體〔？〕錯字", candidate["candidate"])
        self.assertNotIn("錯字〔？〕", candidate["candidate"])
        result = final.finish(self.draft, True, candidate["revision"],
                              task_id="final-review-test", changes_path=self.final_changes())
        self.assertEqual(result["status"], "completed_with_unresolved")
        self.assertIn("s000001", result["unresolved_ids"])
        output = cli._root_path(result["primary_transcript"]).read_text(encoding="utf-8")
        self.assertIn("軟體〔？〕錯字", output)

    def test_direct_draft_change_cannot_reuse_read_receipt(self):
        self.start()
        candidate = self.seal_and_read()
        path = self.draft / "draft.json"
        draft = cli._read(path)
        draft["decisions"]["s000001"] = {
            "segment_id": "s000001", "new_text": "未展示的新文字", "decision": "correct",
            "reason": "manual edit", "evidence_ids": ["text:s000001"]}
        cli._write(path, draft)
        with patch.object(preflight, "preflight", wraps=preflight.preflight) as check, self.assertRaises(ValueError):
            final.finish(self.draft, True, candidate["revision"], task_id="final-review-test",
                         changes_path=self.final_changes())
        check.assert_not_called()
        self.assertFalse(list((self.run / "patches").glob("*.json")))

    def test_ambiguous_edits_and_bad_batch_leave_draft_unchanged(self):
        self.start({"edits": [{"segment_id": "s000001", "new_text": "面粉"},
                               {"segment_id": "s000002", "new_text": "面粉"}]})
        before = (self.draft / "draft.json").read_bytes()
        result = final.edit(self.draft, find="面粉", replacement="麵粉")
        self.assertFalse(result["ok"])
        self.assertEqual(result["match_count"], 2)
        with self.assertRaises(ValueError):
            final.edit(self.draft, self.changes({"edits": [
                {"segment_id": "s000001", "new_text": "麵粉"},
                {"segment_id": "s000002", "new_text": "", "decision": "correct"}]}))
        self.assertEqual(before, (self.draft / "draft.json").read_bytes())
        result = final.edit(self.draft, find="面粉", replacement="麵粉", segment_id="s000001")
        self.assertTrue(result["ok"])

    def test_punctuation_only_edit_keeps_transcript_and_updates_candidate(self):
        self.start()
        candidate = self.seal_and_read()
        raw_before = cli._base(self.run)
        changes = self.final_changes(
            issues=[{"id": "punctuation-1", "kind": "punctuation", "summary": "句尾需要問號。",
                     "resolution": "將第一段句尾改為問號。", "segment_ids": ["s000001"]}],
            punctuation={"s000001": "？"})
        final.finish(self.draft, True, candidate["revision"], task_id="final-review-test",
                     changes_path=changes)
        self.assertEqual(cli._base(self.run), raw_before)
        self.assertIn("？", cli._root_path(cli._read(self.run / "state.json")["primary_transcript"]).read_text(encoding="utf-8"))

    def test_interrupted_publish_recovers_same_patch_without_second_preflight(self):
        self.start({"edits": [{"segment_id": "s000001", "new_text": "軟體正字"}]})
        candidate = self.seal_and_read()
        with patch.object(cli, "_refresh", side_effect=RuntimeError("simulated export failure")), self.assertRaises(RuntimeError):
            final.finish(self.draft, True, candidate["revision"], task_id="final-review-test",
                         changes_path=self.final_changes())
        self.assertTrue((self.run / "review_commit_pending.json").is_file())
        with self.assertRaises(ValueError):
            cli._load_run(self.run)
        with patch.object(cli, "_refresh", wraps=cli._refresh) as refresh, self.assertRaises(ValueError):
            cli.process("source.mp4")
        refresh.assert_not_called()
        with patch.object(preflight, "preflight", side_effect=AssertionError("preflight must not repeat")):
            result = final.finish(self.draft)
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.run / "review_commit_pending.json").exists())
        self.assertEqual(len(list((self.run / "patches").glob("*.json"))), 1)
        self.assertEqual(len(cli._record(self.run)["applied_documents"]), 1)

    def test_receipt_written_before_cleanup_is_recoverable(self):
        self.start()
        candidate = self.seal_and_read()
        final.finish(self.draft, True, candidate["revision"], task_id="final-review-test",
                     changes_path=self.final_changes())
        draft = cli._read(self.draft / "draft.json")
        cli._write(self.run / "review_commit_pending.json", {"draft_id": draft["id"]})
        self.assertTrue(final.finish(self.draft)["already_committed"])
        self.assertFalse((self.run / "review_commit_pending.json").exists())

    def test_unknown_evidence_and_direct_source_dump_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            final.edit(self.draft, self.changes({"edits": [{"segment_id": "s000001", "new_text": "正字", "evidence_ids": ["invented"]}]}))
        with self.assertRaises(ValueError):
            final.excerpt(self.draft, [])
        self.assertEqual(len(final.excerpt(self.draft, ["s000001"])["segments"]), 1)


class LayoutTests(unittest.TestCase):
    """Cards and sections on a small draft, without a run."""

    def draft(self, texts, sections):
        return {"base": {"segments": [{"id": f"s{n}", "text": text, "start": n, "end": n + 1}
                                      for n, text in enumerate(texts, 1)]},
                "prior_decisions": {}, "decisions": {},
                "cards": [{"id": card, "title": card, "verbatim_text": card, "start": 0}
                          for card in ("A", "B")],
                "layout": {"title": "片名", "sections": sections}, "revision": 0, "history": []}

    @staticmethod
    def shape(draft):
        return [(section["title"], [block.get("card_id") or block["segment_ids"] for block in section["blocks"]])
                for section in draft["layout"]["sections"]]

    def test_card_shown_mid_sentence_moves_with_its_section_to_the_sentence_end(self):
        draft = self.draft(["接下來看這個架構", "它分成三層", "第一層是介面", "第二層是資料"], [
            {"title": "開場", "blocks": [{"segment_ids": ["s1"], "punctuation": {"s1": "，"}}]},
            {"title": "A", "blocks": [{"card_id": "A"},
                                      {"segment_ids": ["s2", "s3"], "punctuation": {"s2": "，", "s3": "。"}}]},
            {"title": "B", "blocks": [{"card_id": "B"}, {"segment_ids": ["s4"], "punctuation": {"s4": "。"}}]}])
        moved = final._settle_cards(draft)
        self.assertEqual(moved, [{"card_id": "A", "moved_after_sentence_end": "s3"}])
        # The sentence the card cut is one paragraph again; the next card keeps its place.
        self.assertEqual(self.shape(draft), [("開場", [["s1", "s2", "s3"]]), ("A", ["A"]), ("B", ["B", ["s4"]])])
        content, _ = final._render(draft)
        self.assertIn("接下來看這個架構，它分成三層，第一層是介面。", content)
        self.assertEqual(final._settle_cards(draft), [])

    def test_cards_sharing_a_sentence_keep_their_order(self):
        draft = self.draft(["先看", "這張圖", "說完了"], [
            {"title": "開場", "blocks": [{"segment_ids": ["s1"], "punctuation": {"s1": "，"}}]},
            {"title": "A", "blocks": [{"card_id": "A"}, {"segment_ids": ["s2"], "punctuation": {"s2": "，"}}]},
            {"title": "B", "blocks": [{"card_id": "B"}, {"segment_ids": ["s3"], "punctuation": {"s3": "。"}}]}])
        final._settle_cards(draft)
        self.assertEqual(self.shape(draft), [("開場", [["s1", "s2", "s3"]]), ("A", ["A"]), ("B", ["B"])])

    def test_card_before_carries_the_heading_and_null_merges_a_section(self):
        draft = self.draft(["第一句", "第二句", "第三句"], [
            {"title": "開場", "blocks": [{"segment_ids": ["s1"], "punctuation": {"s1": "。"}}]},
            {"title": "A", "blocks": [{"card_id": "A"}, {"segment_ids": ["s2"], "punctuation": {"s2": "。"}}]},
            {"title": "B", "blocks": [{"card_id": "B"}, {"segment_ids": ["s3"], "punctuation": {"s3": "。"}}]}])
        final._edit_data(draft, {"card_before": {"A": "s3"}}, set())
        self.assertEqual(self.shape(draft), [("開場", [["s1"], ["s2"]]), ("A", ["A"]), ("B", ["B", ["s3"]])])
        final._edit_data(draft, {"section_titles": {"#3": None}}, set())
        self.assertEqual(self.shape(draft), [("開場", [["s1"], ["s2"]]), ("A", ["A", "B", ["s3"]])])
        with self.assertRaisesRegex(ValueError, "first section"):
            final._edit_data(draft, {"section_titles": {"#1": None}}, set())

    def test_default_layout_opens_a_slide_section_after_the_sentence(self):
        segments = [{"id": "s1", "text": "我們接下來看", "start": 0}, {"id": "s2", "text": "這個架構。", "start": 2},
                    {"id": "s3", "text": "它分成三層。", "start": 4}]
        layout = reading.default_layout(segments, [{"id": "A", "title": "架構", "start": 1}])
        self.assertEqual([(s["title"], [b.get("card_id") or b["segment_ids"] for b in s["blocks"]])
                          for s in layout["sections"]],
                         [("開場", [["s1", "s2"]]), ("架構", ["A", ["s3"]])])


if __name__ == "__main__":
    unittest.main()
