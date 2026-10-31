import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system/src"))
import catalog  # noqa: E402
from catalog import view  # noqa: E402
from catalog.db import connect, rows  # noqa: E402


class CatalogTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".runtime/tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "工作資料/原檔").mkdir(parents=True)
        for name in ("第一課#八字 #教學_720p.mp4", "第二課_720p.mp4", "第三課_360p.mp4", "筆記.txt"):
            (self.root / "工作資料/原檔" / name).write_bytes(b"x")
        self.transcript = self.write("工作資料/輸出/逐字稿/第一課.md", "# 第一課\n\n地支巳午未會火，財來壞印。\n")
        self.card = self.write("工作資料/輸出/資料卡/第一課.md", "# 第一課資料卡\n")
        self.make_run("run-1", "工作資料/原檔/第一課#八字 #教學_720p.mp4", "completed", "工作資料/輸出/逐字稿/第一課.md")
        self.make_run("run-3", "工作資料/原檔/第三課_720p.mp4", "completed_with_unresolved", None)
        self.make_run("run-4", "工作資料/原檔/已刪除_720p.mp4", "awaiting_review", None)
        self.task("資料卡", "run-1", "completed", self.sha(self.transcript), "工作資料/輸出/資料卡/第一課.md")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def make_run(self, run_id, source, status, primary):
        folder = self.root / "工作資料/任務/轉錄" / run_id
        folder.mkdir(parents=True)
        (folder / "state.json").write_text("{}", encoding="utf-8")
        (folder / "task.json").write_text(json.dumps({"task_id": run_id, "source": source, "status": status,
                                                      "primary_transcript": primary}, ensure_ascii=False), encoding="utf-8")

    def task(self, kind, run_id, status, sha, output):
        folder = self.root / "工作資料/任務" / kind / run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.json").write_text(json.dumps({"task_id": run_id, "status": status, "primary_sha256": sha,
                                                      "output": output}, ensure_ascii=False), encoding="utf-8")

    def index(self):
        return catalog.overview(self.root)

    def videos(self):
        return {v["name"]: v for v in self.index()["videos"]}

    def test_overview_lists_every_video_with_its_outputs(self):
        videos = self.videos()
        self.assertEqual(sorted(videos), ["第一課", "第三課", "第二課"])
        first = videos["第一課"]
        self.assertEqual((first["run_id"], first["transcript"]["label"]), ("run-1", "完成"))
        self.assertEqual(first["datacard"]["label"], "完成")
        self.assertIsNone(first["handout"])
        self.assertEqual(videos["第二課"]["transcript"]["label"], "未轉錄")
        self.assertIn("同名影片已轉錄", videos["第三課"]["note"])
        self.assertEqual([o["run_id"] for o in self.index()["orphans"]], ["run-4"])
        self.assertIn("| 第一課 | 完成 | 完成 | — |", view.markdown(self.index()))

    def test_new_version_of_an_output_keeps_the_old_one_as_superseded(self):
        self.index()
        first = self.sha(self.card)
        self.card.write_text("# 第一課資料卡（改過）\n", encoding="utf-8")
        self.task("資料卡", "run-1", "completed", self.sha(self.transcript), "工作資料/輸出/資料卡/第一課.md")
        self.assertEqual(self.videos()["第一課"]["datacard"]["versions"], 2)
        with connect(self.root) as connection:
            versions = rows(connection, "select sha256, status from artifacts where path=? order by id",
                            "工作資料/輸出/資料卡/第一課.md")
            events = rows(connection, "select event from events where key=? order by id", "工作資料/輸出/資料卡/第一課.md")
        self.assertEqual([v["status"] for v in versions], ["superseded", "current"])
        self.assertEqual(versions[0]["sha256"], first)
        self.assertIn("superseded", [e["event"] for e in events])

    def test_removed_source_and_output_change_status_but_stay_on_record(self):
        self.index()
        source = self.root / "工作資料/原檔/第一課#八字 #教學_720p.mp4"
        source.unlink()
        self.card.unlink()
        index = self.index()
        first = {v["name"]: v for v in index["videos"]}["第一課"]
        self.assertEqual(first["source_status"], "missing")
        self.assertEqual(first["datacard"]["label"], "成品遺失")
        self.assertEqual([s["name"] for s in index["removed"]["sources"]], ["第一課"])
        self.assertEqual([o["path"] for o in index["removed"]["outputs"]], ["工作資料/輸出/資料卡/第一課.md"])
        self.assertIn("原檔已移除", view.markdown(index))
        source.write_bytes(b"x")  # putting the file back restores it without losing the history
        self.assertEqual({v["name"]: v for v in self.index()["videos"]}["第一課"]["source_status"], "present")
        with connect(self.root) as connection:
            events = [e["event"] for e in rows(connection, "select event from events where key=? order by id",
                                               "工作資料/原檔/第一課#八字 #教學_720p.mp4")]
        self.assertEqual(events[:3], ["added", "missing", "changed"])

    def test_datacard_goes_stale_when_the_transcript_changes(self):
        self.index()
        self.transcript.write_text("# 第一課\n\n修訂過的正文。\n", encoding="utf-8")
        self.assertEqual(self.videos()["第一課"]["datacard"]["label"], "過期（逐字稿已更新）")

    def test_series_card_is_listed_against_each_part(self):
        second = self.write("工作資料/輸出/逐字稿/第三課.md", "# 第三課\n\n通關用神。\n")
        self.make_run("run-5", "工作資料/原檔/第三課_360p.mp4", "completed", "工作資料/輸出/逐字稿/第三課.md")
        self.write("工作資料/輸出/資料卡/課程系列.md", "# 課程系列\n")
        folder = self.root / "工作資料/任務/資料卡/series-abc123"
        folder.mkdir(parents=True)
        parts = [{"part": 1, "run_id": "run-1", "title": "第一課", "primary_sha256": self.sha(self.transcript)},
                 {"part": 2, "run_id": "run-5", "title": "第三課", "primary_sha256": self.sha(second)}]
        (folder / "task.json").write_text(json.dumps(
            {"task_id": "series-abc123", "status": "completed", "title": "課程系列", "parts": parts,
             "output": "工作資料/輸出/資料卡/課程系列.md"}, ensure_ascii=False), encoding="utf-8")
        index = self.index()
        listed = index["series"]
        self.assertEqual([s["name"] for s in listed], ["課程系列"])
        self.assertEqual(listed[0]["label"], "完成")
        self.assertEqual([p["run_id"] for p in listed[0]["parts"]], ["run-1", "run-5"])
        self.assertEqual([s["name"] for s in self.videos()["第三課"]["series"]], ["課程系列"])
        text = view.markdown(index)
        self.assertIn("## 系列資料卡", text)
        self.assertIn("1. 第一課；2. 第三課", text)
        self.assertIn("系列卡「課程系列」完成", text)  # the video row points at the series
        second.write_text("# 第三課\n\n改過的通關用神。\n", encoding="utf-8")
        self.assertEqual(self.index()["series"][0]["label"], "過期（逐字稿已更新）")

    def test_search_finds_videos_by_title_and_content(self):
        result = catalog.search("巳午未", self.root)
        self.assertEqual([h["kind"] for h in result["content_hits"]], ["transcript"])
        self.assertIn("巳午未", result["content_hits"][0]["snippet"])
        self.assertEqual([v["name"] for v in catalog.search("第一", self.root)["videos"]], ["第一課"])

    def test_find_run_accepts_ids_titles_paths_and_refuses_the_rest(self):
        self.assertEqual(catalog.find_run("run-1", self.root).name, "run-1")
        self.assertEqual(catalog.find_run("第一", self.root).name, "run-1")
        self.assertEqual(catalog.find_run("工作資料/原檔/第三課_360p.mp4", self.root).name, "run-3")
        for query, message in (("第二課", "尚未轉錄"), ("課", "符合多支"), ("不存在", "找不到"), ("已刪除", "尚未完成")):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, message):
                catalog.find_run(query, self.root)

    def test_display_name_drops_hashtags_and_resolution(self):
        self.assertEqual(catalog.display_name("示例课程#八字 #易经_720p.mp4"), "示例课程")
        self.assertEqual(catalog.display_name("访谈_320k.mp3"), "访谈")


if __name__ == "__main__":
    unittest.main()
