"""Data-card and handout workflows; synthetic runs only, no private material."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "system/src"))

import domains  # noqa: E402
from datacard import build as card  # noqa: E402
from derived_common import mermaid, source, text  # noqa: E402
from derived_common import primary as user_primary  # noqa: E402
from handout import parse as hp  # noqa: E402
from handout.build import check as handout_check  # noqa: E402
from handout.render_docx import load_theme, render  # noqa: E402

PRIMARY = """# 示範課｜陰陽

正文保留口述內容；辨識錯字依上下文與畫面校正。〔？〕表示尚待核對的辨識；畫面補充另列。

## 開場

大家好，OK，今天講陰陽。

## 原局一

這個八字丙子、辛丑，在2025年應驗〔？〕。

> **畫面補充：原局**
>
> 原局<BR>
> 舉例
>
> | 畫面位置 | 左一 | 左二 |
> | --- | --- | --- |
> | 上排 | 丙 | 辛 |
> | 下排 | 子 | 丑 |

丙火在年上，先熄滅。
""".replace("<BR>", "  ")  # Markdown hard line break, as the reading edition writes it


def make_doc():
    blocks = {
        "b001": {"id": "b001", "kind": "speech", "text": "大家好，OK，今天講陰陽。", "section": "S01", "start": 0.0, "end": 4.0},
        "b002": {"id": "b002", "kind": "speech", "text": "這個八字丙子、辛丑，在2025年應驗〔？〕。", "section": "S02", "start": 5.0, "end": 9.0},
        "screen-001": {"id": "screen-001", "kind": "visual", "title": "原局", "card_kind": "table", "section": "S02",
                       "text": "原局  \n舉例\n\n| 畫面位置 | 左一 | 左二 |\n| --- | --- | --- |\n| 上排 | 丙 | 辛 |\n| 下排 | 子 | 丑 |",
                       "start": 6.0, "end": 8.0, "frames": []},
        "b003": {"id": "b003", "kind": "speech", "text": "丙火在年上，先熄滅。", "section": "S02", "start": 10.0, "end": 12.0},
    }
    return {"run_id": "test-run", "run_dir": "x", "status": "completed", "source_video": "v.mp4", "duration": 12.0,
            "title": "示範課｜陰陽", "primary": "工作資料/輸出/逐字稿/示範.md", "primary_sha256": "abc",
            "sections": [{"id": "S01", "title": "開場", "start": 0.0, "end": 4.0, "blocks": ["b001"]},
                         {"id": "S02", "title": "原局一", "start": 5.0, "end": 12.0, "blocks": ["b002", "screen-001", "b003"]}],
            "blocks": blocks, "order": ["b001", "b002", "screen-001", "b003"]}


def make_plan():
    plan = card.draft(make_doc())
    plan["domain"] = "bazi"
    plan["keywords"] = ["陰陽"]
    plan["sections"][0]["summary"] = ["今天講陰陽。"]
    plan["sections"][1]["summary"] = ["丙火在年上，先熄滅。"]
    plan["sections"][1]["cases"] = [{"name": "原局一", "fields": {"四柱": "丙子 辛丑"}, "quotes": ["丙火在年上，先熄滅。"]}]
    return plan


class SourceTests(unittest.TestCase):
    def test_reading_edition_splits_sections_speech_and_visuals(self):
        title, sections = source.parse_reading_edition(PRIMARY)
        self.assertEqual(title, "示範課｜陰陽")
        self.assertEqual([s["title"] for s in sections], ["開場", "原局一"])
        kinds = [c["kind"] for c in sections[1]["chunks"]]
        self.assertEqual(kinds, ["speech", "visual", "speech"])
        self.assertIn("| 上排 | 丙 | 辛 |", sections[1]["chunks"][1]["text"])
        self.assertTrue(sections[1]["chunks"][1]["text"].startswith("原局  \n舉例"))

    def test_load_run_requires_mapping_to_match_reading_edition(self):
        base = ROOT / ".runtime/tmp"
        base.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(dir=base))
        try:
            primary = run / "primary.md"
            primary.write_text(PRIMARY, encoding="utf-8")
            doc = make_doc()
            mapping = [{"kind": "speech", "segment_ids": ["s1"], "text": doc["blocks"]["b001"]["text"]},
                       {"kind": "speech", "segment_ids": ["s2"], "text": doc["blocks"]["b002"]["text"]},
                       {"kind": "visual", "card_id": "screen-001", "text": doc["blocks"]["screen-001"]["text"]},
                       {"kind": "speech", "segment_ids": ["s3"], "text": doc["blocks"]["b003"]["text"]}]
            files = {
                "state.json": {"run_id": "test-run", "status": "completed", "source": "v.mp4", "primary_transcript": str(primary)},
                "reading_map.json": {"blocks": mapping},
                "transcript.corrected.json": {"segments": [{"id": "s1", "start": 0, "end": 4}, {"id": "s2", "start": 5, "end": 9},
                                                           {"id": "s3", "start": 10, "end": 12}]},
                "visual_review.json": {"cards": [{"id": "screen-001", "kind": "table", "start": 6, "end": 8, "frame_times": []}]},
                "probe.json": {"duration": "12.0"},
            }
            for name, data in files.items():
                (run / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            loaded = source.load_run(run)
            self.assertEqual(loaded["order"], ["b001", "b002", "screen-001", "b003"])
            self.assertEqual(loaded["sections"][1]["id"], "S02")
            self.assertEqual((loaded["blocks"]["b002"]["start"], loaded["blocks"]["b002"]["end"]), (5, 9))
            # User edits are authoritative without any flag: they keep the original
            # run, are snapshotted, and stay the text through prepare, build and stale detection.
            recorded = {name: (run / name).read_bytes() for name in files}
            manual = PRIMARY.replace("2025", "2026")
            primary.write_text(manual, encoding="utf-8")
            edited = source.load_run(run)
            self.assertIn("2026", edited["blocks"]["b002"]["text"])
            self.assertEqual((edited["blocks"]["b002"]["start"], edited["blocks"]["b002"]["end"]), (5, 9))
            self.assertEqual(edited["primary_edits"][0]["paragraph"], 2)
            history = list((run / user_primary.HISTORY).glob("*.md"))
            self.assertEqual([p.read_text(encoding="utf-8") for p in history], [manual])
            source.load_run(run)  # the same content is never stored twice
            self.assertEqual(len(list((run / user_primary.HISTORY).glob("*.md"))), 1)
            folder = run / "datacard-task"
            folder.mkdir()
            with patch("derived_common.tasks.task_dir", return_value=folder), \
                    patch("derived_common.tasks.output_path", return_value=run / "datacard.md"):
                card.prepare(str(run))
                plan = card.draft(edited)
                plan["sections"][0]["summary"] = ["今天講陰陽。"]
                plan["sections"][1]["summary"] = ["丙火在年上，先熄滅。"]
                (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                report = card.build(str(run))
                self.assertEqual(report["errors"], [])
                self.assertIn("2026", (run / "datacard.md").read_text(encoding="utf-8"))
                self.assertEqual(primary.read_text(encoding="utf-8"), manual)
                self.assertEqual({name: (run / name).read_bytes() for name in files}, recorded)
                primary.write_text(manual.replace("2026", "2027"), encoding="utf-8")
                from publish_cli import _status
                self.assertEqual(_status("datacard", str(run))["status"], "stale")
            # A full rewrite of one paragraph is still the user's call.
            rewritten = PRIMARY.replace(doc["blocks"]["b002"]["text"], "完全改成無關內容。")
            primary.write_text(rewritten, encoding="utf-8")
            self.assertEqual(source.load_run(run)["blocks"]["b002"]["text"], "完全改成無關內容。")
            # Cutting the ending keeps every remaining paragraph on its timing, so it is the user's call too.
            cut = PRIMARY.replace(doc["blocks"]["b003"]["text"] + "\n", "")
            primary.write_text(cut, encoding="utf-8")
            trimmed = source.load_run(run)
            self.assertEqual(trimmed["order"], ["b001", "b002", "screen-001"])
            self.assertEqual([(e["paragraph"], e.get("removed")) for e in trimmed["primary_edits"]], [(4, True)])
            cut_section = PRIMARY.split("## 原局一")[0] + "## 原局一\n"  # a heading left with nothing under it
            primary.write_text(cut_section, encoding="utf-8")
            self.assertEqual([s["id"] for s in source.load_run(run)["sections"]], ["S01"])
            # Other structural changes are refused: timings would no longer line up.
            for unsupported in [PRIMARY.replace(doc["blocks"]["b002"]["text"], ""),
                                PRIMARY + "\n新加的一段。\n",
                                PRIMARY.replace("> **畫面補充：原局**", "其他文字")]:
                primary.write_text(unsupported, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "段落結構被改動"):
                    source.load_run(run)
            primary.write_text(PRIMARY, encoding="utf-8")
            files["state.json"]["status"] = "awaiting_context_review"
            (run / "state.json").write_text(json.dumps(files["state.json"]), encoding="utf-8")
            with self.assertRaises(ValueError):
                source.load_run(run)
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_generic_transcript_title_falls_back_to_the_video_name(self):
        base = ROOT / ".runtime/tmp"
        base.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(dir=base))
        try:
            # Older runs titled every reading edition 逐字稿與畫面; the file carries the video's name.
            primary = run / "示例课程#八字 #易经_720p__test-run.md"
            primary.write_text(PRIMARY.replace("# 示範課｜陰陽", "# 逐字稿與畫面"), encoding="utf-8")
            doc = make_doc()
            mapping = [{"kind": "speech", "segment_ids": ["s1"], "text": doc["blocks"]["b001"]["text"]},
                       {"kind": "speech", "segment_ids": ["s2"], "text": doc["blocks"]["b002"]["text"]},
                       {"kind": "visual", "card_id": "screen-001", "text": doc["blocks"]["screen-001"]["text"]},
                       {"kind": "speech", "segment_ids": ["s3"], "text": doc["blocks"]["b003"]["text"]}]
            files = {
                "state.json": {"run_id": "test-run", "status": "completed", "source": "v.mp4", "primary_transcript": str(primary)},
                "reading_map.json": {"blocks": mapping},
                "transcript.corrected.json": {"segments": [{"id": "s1", "start": 0, "end": 4}, {"id": "s2", "start": 5, "end": 9},
                                                           {"id": "s3", "start": 10, "end": 12}]},
                "visual_review.json": {"cards": [{"id": "screen-001", "kind": "table", "start": 6, "end": 8, "frame_times": []}]},
            }
            for name, data in files.items():
                (run / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            loaded = source.load_run(run)
            self.assertEqual(loaded["title"], "示例課程")
            plan = card.draft(loaded)
            errors, _, resolved = card.validate(loaded, plan)
            self.assertEqual(errors, [])
            text_out = card.assemble(loaded, plan, resolved)
            self.assertIn("title: 示例課程\n", text_out)
            self.assertIn("\n# 示例課程\n", text_out)
            from handout.build import template
            self.assertTrue(template(loaded).startswith("---\ntitle: 示例課程\n"))
            # A title the user wrote into the primary is theirs.
            primary.write_text(PRIMARY.replace("# 示範課｜陰陽", "# 我的標題"), encoding="utf-8")
            self.assertEqual(source.load_run(run)["title"], "我的標題")
        finally:
            shutil.rmtree(run, ignore_errors=True)


class TextTests(unittest.TestCase):
    def test_unresolved_clause_keeps_marker_intact(self):
        self.assertEqual(text.unresolved_clauses("前句。在患大精神層面上呢〔？〕，就會亢奮。後句"),
                         ["在患大精神層面上呢〔？〕，就會亢奮。"])

    def test_protected_tokens_skip_spoken_fillers(self):
        tokens = text.protected_tokens("OK，在2025年用AI。")
        self.assertEqual(tokens, {"2025", "AI"})

    def test_meta_phrase_allowed_when_speaker_said_it(self):
        self.assertEqual(text.meta_hits("本講義整理者註", source=""), ["整理者", "本講義"])
        self.assertEqual(text.meta_hits("以下為重點", source="講者說：以下為重點"), [])

    def test_mermaid_labels(self):
        code = 'flowchart TD\n  %% note\n  A["土<br/>中性"] -->|木火成勢| B[跟著木火]\n  C{"好嗎？"}'
        self.assertEqual(mermaid.labels(code), ["土中性", "木火成勢", "跟著木火", "好嗎？"])


class DataCardTests(unittest.TestCase):
    def test_valid_plan_assembles_verbatim_card(self):
        doc, plan = make_doc(), make_plan()
        errors, warnings, resolved = card.validate(doc, plan)
        self.assertEqual(errors, [])
        output = card.assemble(doc, plan, resolved)
        for piece in ("doc_type: video_datacard", "## 目錄", "| S02 | 00:05–00:12 | 原局一 | 丙火在年上，先熄滅。 |",
                      "四柱: 丙子 辛丑", "這個八字丙子、辛丑，在2025年應驗〔？〕。", "> 畫面：原局", "> | 上排 | 丙 | 辛 |",
                      "unresolved:"):
            self.assertIn(piece, output)
        self.assertNotIn("**", output)
        self.assertNotIn("made_by", output)

    def test_screen_note_moves_from_the_card_text_to_front_matter(self):
        shown, notes = source.screen_notes("原局  \n舉例\n\n畫面註：右上角小字被遮住")
        self.assertEqual((shown, notes), ("原局  \n舉例", ["右上角小字被遮住"]))
        doc, plan = make_doc(), make_plan()
        doc["blocks"]["screen-001"]["notes"] = notes
        errors, _, resolved = card.validate(doc, plan)
        self.assertEqual(errors, [])
        output = card.assemble(doc, plan, resolved)
        self.assertNotIn("畫面註", output)
        self.assertIn("畫面: 原局\n  疑點: 右上角小字被遮住", output)

    def test_made_by_is_front_matter_only(self):
        doc, plan = make_doc(), make_plan()
        plan["made_by"] = "Claude Opus 5.5"
        errors, _, resolved = card.validate(doc, plan)
        self.assertEqual(errors, [])
        output = card.assemble(doc, plan, resolved)
        front, body = output.split("\n---\n", 1)
        self.assertIn("made_by: Claude Opus 5.5", front)
        self.assertNotIn("Claude Opus", body)
        for bad in ("", "   ", 5, "x" * 61):
            plan["made_by"] = bad
            self.assertTrue(card.validate(doc, plan)[0], bad)

    def test_plan_cannot_paraphrase_reorder_or_add_commentary(self):
        cases = []
        plan = make_plan(); plan["sections"][1]["summary"] = ["丙火先滅。"]; cases.append(plan)
        plan = make_plan(); plan["sections"][1]["blocks"] = ["screen-001", "b002", "b003"]; cases.append(plan)
        plan = make_plan(); plan["sections"][0]["title"] = "整理者說明"; cases.append(plan)
        plan = make_plan(); plan["keywords"] = ["五行"]; cases.append(plan)
        plan = make_plan(); plan["differences"] = [{"speech": "丙火在年上", "screen": "不在畫面"}]; cases.append(plan)
        for bad in cases:
            with self.subTest(plan=bad):
                errors, _, _ = card.validate(make_doc(), bad)
                self.assertTrue(errors)

    def test_bazi_case_fields_must_be_real_pillars_from_source(self):
        for value, ok in (("丙子 辛丑", True), ("甲丑", False), ("壬午", False)):
            plan = make_plan()
            plan["sections"][1]["cases"][0]["fields"]["四柱"] = value
            errors, _, _ = card.validate(make_doc(), plan)
            self.assertEqual(not errors, ok, (value, errors))

    def test_bazi_reads_pillars_from_screen_table_columns(self):
        table = "| 畫面位置 | 左一 | 左二 |\n| --- | --- | --- |\n| 上排 | 戊 | 辛 |\n| 下排 | 子 | 酉 |"
        self.assertIsNone(domains.load("bazi").validate_case_field("四柱", "戊子 辛酉", table))


class SeriesTests(unittest.TestCase):
    """Several finished runs read as one document: one card, parts kept in order."""

    def make_series(self, name=None):
        first = make_doc()
        first.update(run_id="run-1", primary="工作資料/輸出/逐字稿/示範一.md", stem="示範一", primary_sha256="aaa")
        second = make_doc()
        second.update(run_id="run-2", title="示範課（二）｜五行", primary="工作資料/輸出/逐字稿/示範二.md",
                      stem="示範二", primary_sha256="bbb")
        second["blocks"]["b001"] = dict(second["blocks"]["b001"], text="第二部開場，講五行。")
        second["sections"][0] = dict(second["sections"][0], title="五行開場")
        loaded = {"run-1": first, "run-2": second}
        original = source.load_run
        source.load_run = lambda value: copy.deepcopy(loaded[value])
        try:
            return source.load_series(["run-1", "run-2"], name)
        finally:
            source.load_run = original

    def make_series_plan(self, doc):
        plan = card.draft(doc)
        plan["domain"] = "bazi"
        plan["keywords"] = ["陰陽", "五行"]
        plan["sections"][0]["summary"] = ["今天講陰陽。"]
        plan["sections"][1]["summary"] = ["丙火在年上，先熄滅。"]
        plan["sections"][2]["summary"] = ["第二部開場，講五行。"]
        plan["sections"][1]["cases"] = [{"name": "原局一", "fields": {"四柱": "丙子 辛丑"},
                                         "quotes": ["丙火在年上，先熄滅。"]}]
        return plan

    def test_parts_keep_order_own_clocks_and_distinct_block_ids(self):
        doc = self.make_series()
        self.assertEqual(doc["order"][:2], ["p1-b001", "p1-b002"])
        self.assertEqual(doc["order"][4], "p2-b001")
        self.assertEqual([s["id"] for s in doc["sections"]], ["S01", "S02", "S03", "S04"])
        self.assertEqual([s["part"] for s in doc["sections"]], [1, 1, 2, 2])
        self.assertEqual(doc["title"], "示範課")  # the shared start of both part titles
        self.assertEqual(doc["run_id"], source.series_id(["run-1", "run-2"]))
        self.assertEqual(doc["parts"][1]["sections"], ["S03", "S04"])
        self.assertEqual(doc["stem"], "示範課")
        self.assertEqual(self.make_series(name="自訂: 名/稱")["stem"], "自訂 名稱")

    def test_series_needs_two_runs_and_refuses_repeats(self):
        for values in (["run-1"], ["run-1", "run-1"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                original = source.load_run
                source.load_run = lambda value: make_doc()
                try:
                    source.load_series(values)
                finally:
                    source.load_run = original

    def test_card_lists_parts_and_nests_their_sections(self):
        doc = self.make_series()
        plan = self.make_series_plan(doc)
        errors, _, resolved = card.validate(doc, plan)
        self.assertEqual(errors, [])
        output = card.assemble(doc, plan, resolved)
        for piece in ("doc_type: video_datacard_series", "transcript_run: run-2", "sections: S03–S04",
                      "| 節 | 部 | 時間 | 標題 | 摘要 |", "| S03 | 2 | 00:00–00:04 | 五行開場 |",
                      "## 第 2 部｜示範課（二）｜五行", "### S01｜開場｜00:00–00:04", "第二部開場，講五行。"):
            self.assertIn(piece, output)
        self.assertNotIn("source:", output)

    def test_sections_may_not_be_merged_across_parts(self):
        doc = self.make_series()
        plan = self.make_series_plan(doc)
        plan["sections"][1]["blocks"] = plan["sections"][1]["blocks"] + plan["sections"][2]["blocks"]
        del plan["sections"][2]
        errors, _, _ = card.validate(doc, plan)
        self.assertTrue(any("跨部" in e for e in errors))

    def test_output_is_named_after_the_series(self):
        from derived_common import tasks as derived_tasks
        doc = self.make_series()
        self.assertEqual(derived_tasks.output_path("datacard", doc).name, "示範課.md")


class HandoutTests(unittest.TestCase):
    SOURCE = """---
title: 示範課
subtitle: 陰陽
domain: bazi
---

## 一、開場 {src=b001}

今天講陰陽。

## 二、原局一 {src=b002-b003}

這個八字丙子、辛丑，在2025年應驗〔？〕。

| 位置 | 年 | 月 |
| --- | --- | --- |
| 天干 | 丙 | 辛 |

> 丙火在年上，先熄滅。

```mermaid 圖說
flowchart TD
  A["丙火"] --> B["先熄滅"]
```
"""

    def parse(self, text=None):
        return hp.parse(text or self.SOURCE, ["b001", "b002", "b003"])

    def test_parse_elements_and_ranges(self):
        meta, chapters = self.parse()
        self.assertEqual(meta["title"], "示範課")
        self.assertEqual(chapters[1]["src"], ["b002", "b003"])
        self.assertEqual([e["type"] for e in chapters[1]["elements"]], ["paragraph", "table", "callout", "mermaid"])
        self.assertEqual(chapters[1]["elements"][3]["caption"], "圖說")

    def test_grammar_rejects_free_layout(self):
        bad = [self.SOURCE.replace(" {src=b001}", ""), self.SOURCE.replace("## 一、開場", "# 一、開場"),
               self.SOURCE.replace("```mermaid 圖說", "```python"), self.SOURCE.replace("{src=b002-b003}", "{src=b003-b002}"),
               self.SOURCE + "\n![x](frames/a.png)\n", "## 無 front matter {src=b001}\n"]
        for text_ in bad:
            with self.subTest(text=text_[:40]), self.assertRaises(hp.HandoutError):
                self.parse(text_)

    def test_check_passes_and_ignores_spoken_filler(self):
        meta, chapters = self.parse()
        errors, warnings, stats = handout_check(make_doc(), meta, chapters, domains.load("bazi"))
        self.assertEqual(errors, [])
        self.assertIn("ratio", stats)

    def test_check_catches_dropped_facts_markers_coverage_and_commentary(self):
        variants = {
            "number": self.SOURCE.replace("在2025年", "那年"),
            "pillar": self.SOURCE.replace("這個八字丙子、辛丑", "這個八字"),
            "marker": self.SOURCE.replace("〔？〕", ""),
            "meta": self.SOURCE.replace("今天講陰陽。", "今天講陰陽。整理者註：以下為重點。"),
            "coverage": self.SOURCE.replace("{src=b002-b003}", "{src=b002}"),
        }
        for name, variant in variants.items():
            with self.subTest(name=name):
                meta, chapters = self.parse(variant)
                errors, _, _ = handout_check(make_doc(), meta, chapters, domains.load("bazi"))
                self.assertTrue(errors, name)

    def test_render_docx_uses_plain_google_docs_friendly_parts(self):
        from docx import Document
        meta, chapters = self.parse()
        chapters[1]["elements"] = [e for e in chapters[1]["elements"] if e["type"] != "mermaid"]
        base = ROOT / ".runtime/tmp"
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(dir=base))
        try:
            output = folder / "out.docx"
            render(meta, chapters, {}, load_theme(), output)
            document = Document(str(output))
            texts = [p.text for p in document.paragraphs]
            self.assertEqual(texts[0], "示範課")
            self.assertIn("二、原局一", texts)
            self.assertEqual(document.paragraphs[0].style.name, "Title")
            self.assertEqual(len(document.tables), 2)  # data table + callout box
            self.assertEqual(document.core_properties.author, "")
            xml = document.element.xml
            for unsupported in ("w:txbxContent", "wp:anchor", "w:footnoteReference"):
                self.assertNotIn(unsupported, xml)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def _browser_available():
    try:
        with mermaid.Mermaid() as renderer:
            return renderer.parse("flowchart TD\n A-->B") is None
    except Exception:
        return False


@unittest.skipUnless(_browser_available(), "system Edge/Chrome or mermaid asset unavailable")
class MermaidTests(unittest.TestCase):
    def test_parse_and_render_with_locked_asset(self):
        base = ROOT / ".runtime/tmp"
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(dir=base))
        try:
            with mermaid.Mermaid(load_theme()["mermaid"]) as renderer:
                self.assertIsNotNone(renderer.parse("flowchart TD\n A-->"))
                result = renderer.render('flowchart TD\n A["土"] -->|成勢| B["跟隨"]', folder / "d.png")
                self.assertTrue(result["ok"])
                self.assertTrue((folder / "d.png").stat().st_size > 1000)
                self.assertFalse(renderer.render("flowchart TD\n A-->", folder / "bad.png")["ok"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
