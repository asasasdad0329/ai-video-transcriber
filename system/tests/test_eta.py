import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import eta


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_record(self, name, minutes, asr_seconds, asr_device="cuda", ocr_seconds=None, day=1):
        run = self.runs / name
        run.mkdir()
        (run / "probe.json").write_text(json.dumps({"duration": minutes * 60}), encoding="utf-8")
        stages = {"asr": {"status": "done", "started_at": f"2026-10-{day:02d}T00:00:00+00:00",
                          "completed_at": f"2026-10-{day:02d}T00:00:{asr_seconds:02d}+00:00"}}
        if ocr_seconds is not None:
            stages["visuals"] = {"status": "done", "started_at": f"2026-10-{day:02d}T01:00:00+00:00",
                                 "completed_at": f"2026-10-{day:02d}T01:{ocr_seconds // 60:02d}:{ocr_seconds % 60:02d}+00:00"}
        (run / "state.json").write_text(json.dumps({"asr_device": asr_device, "stages": stages}), encoding="utf-8")

    def test_a_new_machine_uses_the_defaults(self):
        plan, based_on = eta.estimate(self.runs, 10, {"asr": "cuda", "visuals": "cpu"})
        self.assertEqual(based_on, 0)
        self.assertAlmostEqual(plan["asr"], eta.DEFAULT_RATES[("asr", "cuda")] * 10)
        self.assertAlmostEqual(plan["visuals"], eta.DEFAULT_RATES[("visuals", "cpu")] * 10)

    def test_the_machine_own_runs_replace_the_defaults_per_device(self):
        for day in (1, 2, 3):
            self.run_record(f"r{day}", minutes=10, asr_seconds=50, ocr_seconds=120, day=day)
        self.run_record("cpu", minutes=10, asr_seconds=59, asr_device="cpu", day=4)
        plan, based_on = eta.estimate(self.runs, 20, {"asr": "cuda", "visuals": "cpu"})
        self.assertEqual(based_on, 3)
        self.assertAlmostEqual(plan["asr"], 5.0 * 20)
        self.assertAlmostEqual(plan["visuals"], 12.0 * 20)
        cpu, cpu_based_on = eta.estimate(self.runs, 20, {"asr": "cpu", "visuals": "cpu"}, stages=("asr",))
        self.assertEqual(cpu_based_on, 0)
        self.assertAlmostEqual(cpu["asr"], eta.DEFAULT_RATES[("asr", "cpu")] * 20)

    def test_only_remaining_stages_are_estimated(self):
        plan, _ = eta.estimate(self.runs, 10, {"asr": "cuda", "visuals": "cpu"}, stages=("visuals",))
        self.assertEqual(list(plan), ["visuals"])


if __name__ == "__main__":
    unittest.main()
