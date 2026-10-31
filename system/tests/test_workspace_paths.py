import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import workspace_paths as paths
from video_workflow import cli


class WorkspacePathsTests(unittest.TestCase):
    def test_legacy_paths_find_moved_files_without_rewriting_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            pairs = {
                "runs/job/visuals.json": "工作資料/任務/轉錄/job/visuals.json",
                "逐字稿輸出/job/raw.md": "工作資料/任務/轉錄/job/exports/transcripts/raw.md",
                "畫面轉錄輸出/job/畫面轉錄.md": "工作資料/任務/轉錄/job/exports/visuals/畫面轉錄.md",
                "逐字稿輸出/課.md": "工作資料/輸出/逐字稿/課.md",
                "原檔/課.mp4": "工作資料/原檔/課.mp4",
                ".tools/ffmpeg/ffmpeg.exe": ".runtime/tools/ffmpeg/ffmpeg.exe",
                "requirements.lock.txt": "system/config/requirements.lock.txt",
            }
            for old, new in pairs.items():
                destination = root / new
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"original-evidence")
                for source in (old, root / old, new, root / new):
                    self.assertEqual(paths.resolve(source, root), destination)
                    self.assertEqual(paths.resolve(source, root).read_bytes(), b"original-evidence")

    def test_aliases_do_not_rewrite_external_or_similarly_named_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "project"
            self.assertEqual(paths.resolve(root.parent / "runs/x", root), root.parent / "runs/x")
            self.assertEqual(paths.resolve("runs-old/x", root), root / "runs-old/x")
            self.assertEqual(paths.resolve("system/config/default.json", root), root / "system/config/default.json")

    def test_task_index_reports_current_locations_without_media_work(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, "ROOT", Path(folder).resolve()):
            run = cli.ROOT / paths.RUNS / "job"
            cli._write(run / "state.json", {"run_id": "job", "source": "原檔/課.mp4",
                       "status": "completed", "primary_transcript": "逐字稿輸出/課.md"})
            before = (run / "state.json").read_bytes()
            with patch.object(cli.media, "probe", side_effect=AssertionError("Unexpected probe")):
                item = cli.list_runs()["runs"][0]
            self.assertEqual(item["source"], "工作資料/原檔/課.mp4")
            self.assertEqual(item["primary_transcript"], "工作資料/輸出/逐字稿/課.md")
            self.assertEqual(item["run_dir"], "工作資料/任務/轉錄/job")
            self.assertEqual((run / "state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
