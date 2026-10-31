import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import cli, compact


class CompactTests(unittest.TestCase):
    def test_all_hypotheses_and_provenance_survive_without_mutating_source(self):
        result = {"run_id": "r", "segment_id": "s1", "id": "recheck:s1",
                  "independent_evidence": False, "audio_verified": False,
                  "note": "Same model, not listened", "clip_start": 0, "clip_end": 4,
                  "review_glossary": ["量子糾纏"],
                  "segments": [{"id": "r1", "start": 0, "end": 2, "text": "甲",
                                "tokens": [{"text": "甲", "p": .2}]},
                               {"id": "r2", "start": 2, "end": 4, "text": "乙"}]}
        before = copy.deepcopy(result)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "result.json"
            source.write_text(json.dumps(result), encoding="utf-8")
            raw = source.read_bytes()
            view = compact.recheck_view(result, source, "evidence/result.json")
            self.assertEqual(view["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(result, before)
        self.assertEqual(view["segments"], [{k: s[k] for k in ("id", "start", "end", "text")}
                                             for s in result["segments"]])
        self.assertFalse(view["independent_evidence"])
        self.assertFalse(view["audio_verified"])
        self.assertEqual(view["review_glossary"], result["review_glossary"])
        self.assertEqual(view["evidence_path"], "evidence/result.json")

    def test_status_keeps_errors_and_unresolved_without_identity(self):
        state = {"run_id": "r", "status": "failed", "error": "OCR failed",
                 "identity": {"large": "secretless large metadata"},
                 "review": {"unresolved_ids": ["s1"]},
                 "stages": {"visuals": {"status": "failed", "error": "timeout"}}}
        result = compact.status_view(state)
        self.assertNotIn("identity", result)
        self.assertEqual(result["error"], "OCR failed")
        self.assertEqual(result["review"], state["review"])
        self.assertEqual(result["failures"][0]["error"], "timeout")

    def test_saved_view_is_read_only_and_full_escape_hatch_matches_original(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, "ROOT", Path(folder)):
            run = Path(folder) / "run"
            cli._write(run / "state.json", {"run_id": "r"})
            cli._write(run / "transcript.raw.json", {"segments": [{"id": "s1"}]})
            data = {"run_id": "r", "segment_id": "s1", "id": "recheck:s1",
                    "segments": [{"id": "a", "start": 0, "end": 1, "text": "test", "tokens": [1]}]}
            source = run / "rechecks/s1/result.json"
            cli._write(source, data)
            before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
            with patch.object(cli.media, "transcribe", side_effect=AssertionError("must not rerun")):
                self.assertEqual(cli.saved_recheck_view(run, "s1", True), data)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(cli.main(["recheck-view", str(run), "--segment-id", "s1"]), 0)
                self.assertNotIn("tokens", json.loads(stdout.getvalue())["segments"][0])
            self.assertEqual(before, {p: p.read_bytes() for p in run.rglob("*") if p.is_file()})
            with self.assertRaises(ValueError):
                cli.saved_recheck_view(run, "../s1")

    def test_status_cli_defaults_compact_but_full_remains_available(self):
        state = {"run_id": "r", "status": "failed", "error": "failure", "identity": {"x": 1}}
        with tempfile.TemporaryDirectory() as folder, patch.object(cli, "ROOT", Path(folder)):
            run = Path(folder) / "run"
            cli._write(run / "state.json", state)
            for args, identity in (([], False), (["--full"], True)):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(cli.main(["status", str(run)] + args), 0)
                self.assertEqual("identity" in json.loads(stdout.getvalue()), identity)


if __name__ == "__main__":
    unittest.main()
