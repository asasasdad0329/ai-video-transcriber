from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import system_components as components


class SystemComponentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.system32 = self.root / "System32"
        self.system32.mkdir()
        environment = {"SystemRoot": str(self.root), "ProgramFiles": str(self.root / "pf"),
                       "ProgramFiles(x86)": str(self.root / "pf86"), "LOCALAPPDATA": str(self.root / "local")}
        self.env = patch.dict("os.environ", environment)
        self.env.start()
        self.which = patch.object(components.shutil, "which", return_value=None)
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.env.stop()
        self.folder.cleanup()

    def smi(self, stdout):
        (self.system32 / "nvidia-smi.exe").write_bytes(b"")
        return lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def test_reports_missing_runtime_dlls_by_name(self):
        (self.system32 / "vcruntime140.dll").write_bytes(b"")
        result = components.check()
        self.assertFalse(result["vc_runtime"]["ok"])
        self.assertEqual(result["vc_runtime"]["missing"], ["msvcp140.dll", "vcruntime140_1.dll", "vcomp140.dll"])
        for name in components.VC_RUNTIME:
            (self.system32 / name).write_bytes(b"")
        self.assertTrue(components.check()["vc_runtime"]["ok"])

    def test_driver_must_support_cuda_11_8(self):
        self.assertEqual(components.check()["nvidia_driver"], {"need": "建議；沒有時語音辨識自動改用 CPU，較慢", "ok": False,
                                                               "driver_cuda": None})
        for version, expected in (("12.4", True), ("11.8", True), ("11.4", False)):
            run = self.smi(f"| NVIDIA-SMI 551.86   Driver Version: 551.86   CUDA Version: {version}     |")
            with self.subTest(version=version):
                result = components.check(run)["nvidia_driver"]
                self.assertEqual((result["ok"], result["driver_cuda"]), (expected, version))
        run = self.smi("| NVIDIA-SMI 610.88   KMD Version: 610.88   CUDA UMD Version: 13.3     |")
        self.assertEqual(components.check(run)["nvidia_driver"]["driver_cuda"], "13.3")

    def test_browser_found_in_any_standard_location(self):
        self.assertFalse(components.check()["browser"]["ok"])
        edge = self.root / "pf86/Microsoft/Edge/Application/msedge.exe"
        edge.parent.mkdir(parents=True)
        edge.write_bytes(b"")
        self.assertEqual(components.check()["browser"]["path"], str(edge))


if __name__ == "__main__":
    unittest.main()
