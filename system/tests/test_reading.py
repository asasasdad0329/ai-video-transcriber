import copy
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import reading


class ReadingEditionTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [{"id": "a", "start": 0, "end": 1, "text": "家里的东西"},
                                       {"id": "b", "start": 1, "end": 2, "text": "完全属于我"}]}
        self.cards = [{"id": "c", "start": 0, "title": "原局", "kind": "table", "frame_ids": ["f"],
                       "frame_times": [{"time": 0, "path": "C:/private/frame.png"}],
                       "verbatim_text": "命盘", "structure": "| 上排 | 甲 | 癸 |\n| --- | --- | --- |\n| 下排 | 戌 | 丑 |\n\n同幀字幕（OCR）：\n00:10 重複字幕",
                       "uncertainties": []}]
        self.layout = {"title": "例子", "protected_terms": ["丑"], "sections": [{"title": "家裡與家外", "blocks": [
            {"segment_ids": ["a", "b"], "punctuation": {"a": "，", "b": "。"}}, {"card_id": "c"}]}]}

    def test_portable_paragraphs_keep_all_speech_and_table_without_audit_links(self):
        before = copy.deepcopy((self.transcript, self.cards))
        text, mapping = reading.render(self.transcript, self.cards, {"b": {"decision": "needs_review"}}, self.layout)
        self.assertIn("家裡的東西，完全屬於我〔？〕。", text)
        self.assertIn("> | 下排 | 戌 | 丑 |", text)
        for unwanted in ("C:/", ".png", "00:10", "重複字幕", "![", "](../../"):
            self.assertNotIn(unwanted, text)
        self.assertEqual(mapping[0]["segment_ids"], ["a", "b"])
        self.assertEqual((self.transcript, self.cards), before)

    def test_layout_cannot_drop_duplicate_or_reorder_words_or_visuals(self):
        for ids in (["a"], ["b", "a"], ["a", "a", "b"]):
            layout = copy.deepcopy(self.layout)
            layout["sections"][0]["blocks"][0] = {"segment_ids": ids}
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                reading.render(self.transcript, self.cards, {}, layout)
        self.layout["sections"][0]["blocks"].pop()
        with self.assertRaises(ValueError):
            reading.render(self.transcript, self.cards, {}, self.layout)

    def test_punctuation_cannot_smuggle_new_speech_and_default_keeps_slide_structure(self):
        self.layout["sections"][0]["blocks"][0]["punctuation"]["a"] = "所以"
        with self.assertRaises(ValueError):
            reading.render(self.transcript, self.cards, {}, self.layout)
        self.cards[0]["kind"] = "slide"
        layout = reading.default_layout(self.transcript["segments"], self.cards)
        layout["protected_terms"] = ["丑"]
        text, _ = reading.render(self.transcript, self.cards, {}, layout)
        self.assertIn("| 下排 | 戌 | 丑 |", text)

    def test_taiwan_wording_changes_only_listed_context_free_words(self):
        self.assertEqual(reading.traditional("通过视频打开八字，局部显卡，电视频道，丑", ["丑"]),
                         "通過影片打開八字，局部顯示卡，電視頻道，丑")
        self.assertEqual(reading.traditional("很明显卡住了"), "很明顯卡住了")
        self.assertEqual(reading.traditional("软件,好吗?", punctuation=False), "軟體,好嗎?")

    def test_half_width_punctuation_widens_only_beside_chinese(self):
        for source, expected in (("First, introduce", "First, introduce"), ("Google Drive!", "Google Drive!"),
                                 ("1,000", "1,000"), ("Really?!", "Really?!"),
                                 ("好,我們", "好，我們"), ("真的?", "真的？"), ("OK,我們", "OK，我們"),
                                 ("真的?!", "真的？！"), ("好 , 我們", "好 ， 我們"), ("First,\n第二行", "First,\n第二行")):
            with self.subTest(source=source):
                self.assertEqual(reading.traditional(source), expected)
        # Held terms count by their own characters; literals keep theirs untouched.
        self.assertEqual(reading.traditional("丑,寅", ["丑"]), "丑，寅")
        # Segments meet their neighbours only once joined into a paragraph.
        transcript = {"segments": [{"id": "a", "start": 0, "end": 1, "text": "先打開 Google,"},
                                   {"id": "b", "start": 1, "end": 2, "text": "然后 Sign in,"},
                                   {"id": "c", "start": 2, "end": 3, "text": "Drive!"}]}
        layout = {"title": "例子", "sections": [{"title": "開場", "blocks": [
            {"segment_ids": ["a", "b", "c"]}, {"card_id": "c"}]}]}
        text, _ = reading.render(transcript, self.cards, {}, {**layout, "protected_terms": ["丑"]})
        self.assertIn("先打開 Google，然後 Sign in,Drive!", text)
        transcript["segments"][0]["text"] = "看 https://example.com/a,"
        text, _ = reading.render(transcript, self.cards, {}, {**layout, "protected_terms": ["丑"]})
        self.assertIn("看 https://example.com/a,然後", text)
        self.assertEqual(reading.traditional("執行 `a,b?` 後,再打開 https://example.com/?q=1!"),
                         "執行 `a,b?` 後，再打開 https://example.com/?q=1!")

    def test_tai_stays_tai_unless_the_source_or_the_word_says_otherwise(self):
        for source, expected in (("台湾", "台灣"), ("电视台", "電視台"), ("台湾台北市", "台灣台北市"),
                                 ("一台电脑", "一台電腦"), ("臺灣", "臺灣"), ("台风", "颱風"),
                                 ("台灯", "檯燈"), ("柜台", "櫃檯"), ("台風", "台風")):
            with self.subTest(source=source):
                self.assertEqual(reading.traditional(source), expected)
        self.assertEqual(reading.traditional("台湾", ["台湾"]), "台湾")

    def test_word_list_still_matches_converter_output_around_tai(self):
        pairs = {"臺式機": "桌上型電腦", "電視臺": "電視臺"}  # a replacement and a shield line
        pattern = re.compile("|".join(map(re.escape, sorted(pairs, key=len, reverse=True))))
        with patch.object(reading, "_taiwan_terms", return_value=(pattern, pairs)):
            self.assertEqual(reading.traditional("台式机和电视台"), "桌上型電腦和電視台")
            self.assertEqual(reading.traditional("臺式機和電視臺"), "桌上型電腦和電視臺")

    def test_tai_alignment_survives_a_length_changing_converter(self):
        real = reading._CC
        fake = type("Converter", (), {"convert": staticmethod(lambda text: real.convert(text).replace("甲", "甲乙"))})
        with patch.object(reading, "_CC", fake):
            self.assertEqual(reading.traditional("台湾甲台北,电视台"), "台灣甲乙台北，電視台")
        sample = "".join(map(chr, range(0x4E00, 0xA000)))
        self.assertEqual(len(real.convert(sample)), len(sample))

    def test_card_replacement_is_written_as_given_and_must_change_the_text(self):
        body = "First， introduce 软件"
        rule = {"find": "First， introduce", "replace": "First, introduce"}
        self.assertEqual(reading.replace_card_text(reading.traditional(body), rule, "c"), "First, introduce 軟體")
        for find, replace in (("软件", "軟體"), ("軟體", "軟體")):
            with self.subTest(find=find), self.assertRaisesRegex(ValueError, "leaves card c unchanged"):
                reading.replace_card_text("First, introduce 軟體", {"find": find, "replace": replace}, "c")
        # Alone, "OK," has no Chinese neighbour; inside the card it reads "OK，".
        self.assertEqual(reading.replace_card_text("OK，我們走", {"find": "OK,", "replace": "好，"}, "c"), "好，我們走")
        with self.assertRaisesRegex(ValueError, "match exactly once: c"):
            reading.replace_card_text("甲，甲", {"find": "甲", "replace": "乙"}, "c")

    def test_recorded_card_replacements_still_apply_after_normalization_changes(self):
        # Stored when every comma was widened and 台 became 臺; the body now keeps both.
        card = {"id": "c", "verbatim_text": "First, introduce\n台北捷运站", "uncertainties": []}
        block = {"card_id": "c", "text_replacements": [
            {"find": "First， introduce", "replace": "First, introduce"},
            {"find": "臺北捷運站", "replace": "臺北捷運站出口"}]}
        self.assertEqual(reading.card_text(card, block), "First, introduce  \n臺北捷運站出口")
        block["text_replacements"].append({"find": "不存在", "replace": "新字"})
        with self.assertRaisesRegex(ValueError, "match exactly once: c"):
            reading.card_text(card, block)

    def test_default_layout_is_titled_after_the_video(self):
        layout = reading.default_layout(self.transcript["segments"], self.cards, "示例课程")
        text, _ = reading.render(self.transcript, self.cards, {}, {**layout, "protected_terms": ["丑"]})
        self.assertTrue(text.startswith("# 示例課程\n"))
        self.assertEqual(reading.default_layout([], [])["title"], reading.DEFAULT_TITLE)

    def test_screen_literals_keep_original_characters(self):
        self.assertEqual(reading.traditional("文件位置：D:\\视频\\软件说明.txt，已下载"),
                         "文件位置：D:\\视频\\软件说明.txt，已下載")
        self.assertEqual(reading.traditional("执行 `视频工具 --help` 或打开 https://example.com/视频"),
                         "執行 `视频工具 --help` 或打開 https://example.com/视频")
        self.assertEqual(reading.traditional("共享：\\\\server\\视频\\a.mp4"), "共享：\\\\server\\视频\\a.mp4")

    def test_schedule_times_stay_and_only_capture_notes_are_dropped(self):
        card = {"verbatim_text": "排程", "uncertainties": [],
                "structure": "| 09:00 | 開工 |\n| 10:30 | 完成 |\n擷取註記：00:44 新增第二列（f000012）"}
        body = reading.visual_body(card)
        self.assertIn("| 09:00 | 開工 |\n| 10:30 | 完成 |", body)
        for unwanted in ("擷取註記", "00:44", "f000012"):
            self.assertNotIn(unwanted, body)
        legacy = {"verbatim_text": "", "uncertainties": [], "frame_times": [{"time": 340.0}],
                  "structure": "05:40 畫面左二下排改為紅色『X』，09:00 仍顯示\n| 05:40 | 開工 |"}
        self.assertEqual(reading.visual_body(legacy), legacy["structure"])

    def test_only_line_leading_full_timestamp_is_removed(self):
        card = {"frame_times": [{"time": 340}], "structure":
                "00:05:40 左二改為 X，00:05:40 開工\n  01:02:03 新增箭頭\n"
                "活動在 00:05:40 開始，05:40 結束\n00:05:40"}
        self.assertEqual(reading.visual_body(card),
                         "左二改為 X，00:05:40 開工\n  新增箭頭\n活動在 00:05:40 開始，05:40 結束")
        for text in ("05:40 文字", "0:05:40 文字", "00:60:40 文字", "00:05:60 文字",
                     "00:05:400 文字", "00:05:40.123 文字", "00:05:40的排程", "[00:05:40] 文字"):
            with self.subTest(text=text):
                self.assertEqual(reading.visual_body({"structure": text}), text)

    def test_full_timestamps_in_tables_and_code_are_preserved(self):
        structure = ("| 00:05:40 | 開始 |\n00:05:40 | 結束\n"
                     "```mermaid\nflowchart LR\nA[00:05:40] --> B[完成]\n00:05:40 code\n"
                     "擷取註記：literal\n```\n"
                     "~~~~text\n00:05:40 code\n~~~\n00:05:40 still code\n~~~~\n"
                     "00:05:40 說明")
        self.assertEqual(reading.visual_body({"structure": structure}), structure.rsplit("00:05:40 ", 1)[0] + "說明")
        for structure in ("    00:05:40 code", "\t00:05:40 code"):
            self.assertEqual(reading.visual_body({"structure": structure}), structure)


if __name__ == "__main__":
    unittest.main()
