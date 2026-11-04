import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system/bootstrap"))
import fetch_assets  # noqa: E402
import setup  # noqa: E402

POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SHELL_FILES = [ROOT / "run.ps1", ROOT / "run.cmd",
               *[p for pattern in ("*.ps1", "*.cmd", "*.bat") for p in (ROOT / "system").rglob(pattern)]]


def windows_powershell(script, **env):
    """Run an ASCII driver script under Windows PowerShell 5.1, the shell every Windows has."""
    with tempfile.TemporaryDirectory() as folder:
        driver = Path(folder) / "driver.ps1"
        driver.write_text(script, encoding="ascii")
        done = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(driver)], capture_output=True, timeout=120,
                              env={**os.environ, "VW_RUN": str(ROOT / "run.ps1"), **env})
    return done.stdout.decode("utf-8", "replace").strip().splitlines()


class ShellFileTests(unittest.TestCase):
    def test_shell_entry_files_are_ascii(self):
        # Windows PowerShell 5.1 reads a BOM-less script in the system code page; ASCII reads
        # the same in every code page, so no file depends on a BOM or on the user's locale.
        for path in SHELL_FILES:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertTrue(all(byte < 0x80 for byte in path.read_bytes()))

    def test_powershell_files_embed_no_inline_code(self):
        # Native arguments lose embedded quotes under Windows PowerShell 5.1; code belongs in files.
        for path in SHELL_FILES:
            if path.suffix == ".ps1":
                text = path.read_text(encoding="ascii")
                self.assertNotRegex(text, r"\s-c\s+['\"]", path.name)
                self.assertNotRegex(text, r"(?i)\s-Command\s+['\"]", path.name)

    @unittest.skipUnless(POWERSHELL.is_file(), "Windows PowerShell 5.1 not available")
    def test_windows_powershell_parses_every_script(self):
        for path in SHELL_FILES:
            if path.suffix == ".ps1":
                lines = windows_powershell(
                    "$t = $null; $e = $null\n"
                    "[void][System.Management.Automation.Language.Parser]::ParseFile($env:VW_FILE, [ref]$t, [ref]$e)\n"
                    "$e.Count\n", VW_FILE=str(path))
                self.assertEqual(lines[-1:], ["0"], path.name)


class ModuleNameTests(unittest.TestCase):
    def test_no_bootstrap_script_shadows_a_source_module(self):
        # Bootstrap scripts put their own folder on sys.path, so a script named like a module in
        # system/src wins or loses by path order alone: setup once imported the transcribe entry
        # script in place of the video_workflow package and failed after a finished install.
        src = ROOT / "system/src"
        modules = {p.stem for p in src.glob("*.py")} | {p.parent.name for p in src.glob("*/__init__.py")}
        scripts = {p.stem for p in (ROOT / "system/bootstrap").glob("*.py")}
        self.assertEqual(scripts & modules, set())


@unittest.skipUnless(POWERSHELL.is_file(), "Windows PowerShell 5.1 not available")
class EntryPointTests(unittest.TestCase):
    def test_arguments_reach_python_unchanged(self):
        lines = windows_powershell(
            "& $env:VW_RUN _args 'say \"hi\" now' 'C:\\a b\\' '' ([string][char]0x4E2D) 'Q&A.mp4' | ForEach-Object { $_ }\n"
            "'exit=' + $LASTEXITCODE\n")
        self.assertEqual(json.loads(lines[-2]), ['say "hi" now', "C:\\a b\\", "", "\u4e2d", "Q&A.mp4"])
        self.assertEqual(lines[-1], "exit=0")

    def test_stderr_is_not_a_powershell_error(self):
        # Exactly what an agent tends to type: stop on errors and merge every stream.
        lines = windows_powershell(
            "$ErrorActionPreference = 'Stop'\n"
            "try { $null = & $env:VW_RUN no-such-command *>&1; 'exit=' + $LASTEXITCODE } catch { 'THREW ' + $_ }\n")
        self.assertEqual(lines[-1], "exit=2")

    def test_output_stays_in_the_pipeline(self):
        lines = windows_powershell("$a = & $env:VW_RUN _args one two | ConvertFrom-Json\n$a -join '+'\n")
        self.assertEqual(lines[-1], "one+two")


class SetupTests(unittest.TestCase):
    def test_venv_must_be_built_on_the_project_python(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "python/cpython/python.exe"
            other = root / "elsewhere/python.exe"
            venv = root / "venv"
            for path in (base, other, venv / "Scripts/python.exe"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            config = venv / "pyvenv.cfg"
            config.write_text(f"home = {base.parent}\nversion_info = 3.12.12\n", encoding="utf-8")
            self.assertTrue(setup.venv_matches(venv, base))
            config.write_text(f"home = {other.parent}\n", encoding="utf-8")
            self.assertFalse(setup.venv_matches(venv, base))
            config.unlink()
            self.assertFalse(setup.venv_matches(venv, base))


class DownloadTests(unittest.TestCase):
    def test_next_route_is_tried_when_one_fails(self):
        def refuse(url, destination):
            raise subprocess.CalledProcessError(60, "curl")

        def deliver(url, destination):
            destination.write_bytes(b"pinned bytes")

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(fetch_assets, "_curl", refuse), patch.object(fetch_assets, "_powershell", refuse), \
                patch.object(fetch_assets, "_urllib", deliver), patch("builtins.print"):
            target = Path(folder) / "asset.partial"
            fetch_assets.download("https://example.invalid/asset", target)
            self.assertEqual(target.read_bytes(), b"pinned bytes")

    def test_every_route_failing_is_one_clear_error(self):
        def refuse(url, destination):
            raise OSError("unreachable")

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(fetch_assets, "_curl", refuse), patch.object(fetch_assets, "_powershell", refuse), \
                patch.object(fetch_assets, "_urllib", refuse), patch("builtins.print"):
            with self.assertRaisesRegex(OSError, "every method"):
                fetch_assets.download("https://example.invalid/asset", Path(folder) / "asset.partial")


class UnpackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target in (patch.object(fetch_assets, "ROOT", self.root), patch("builtins.print")):
            target.start()
            self.addCleanup(target.stop)
        self.program = self.root / ".runtime/tools/whisper/Release/whisper-cli.exe"

    def archive(self, content):
        path = self.root / "whisper.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            bundle.writestr("Release/whisper-cli.exe", content)
        return path

    def test_an_unpacked_tool_needs_no_archive(self):
        # Setup once fetched the archive before looking at the tool, so clearing the download
        # cache cost a 270 MB download, and offline setup failed outright.
        with patch.object(fetch_assets, "fetch", return_value=self.archive(b"pinned")):
            fetch_assets.install("whisper")
        with patch.object(fetch_assets, "fetch", side_effect=FileNotFoundError("cache cleared")):
            fetch_assets.install("whisper", offline=True)
        self.assertEqual(self.program.read_bytes(), b"pinned")

    def test_a_new_pin_or_a_missing_program_is_unpacked_again(self):
        self.program.parent.mkdir(parents=True)
        self.program.write_bytes(b"older version")
        (self.program.parents[1] / fetch_assets.UNPACKED_FROM).write_text("older pin\n", encoding="ascii")
        with patch.object(fetch_assets, "fetch", return_value=self.archive(b"pinned")):
            fetch_assets.install("whisper")
            self.assertEqual(self.program.read_bytes(), b"pinned")
            self.program.unlink()
            fetch_assets.install("whisper")
        self.assertEqual(self.program.read_bytes(), b"pinned")


class GpuOcrChoiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        choice = patch.object(setup, "GPU_OCR_CHOICE", Path(self.folder.name) / "gpu-ocr.json")
        choice.start()
        self.addCleanup(choice.stop)
        self.addCleanup(self.folder.cleanup)
        self.lines = []
        self.session = type("Session", (), {"say": lambda _, text: self.lines.append(text)})()

    def test_unattended_setup_only_mentions_the_option(self):
        setup.offer_gpu_ocr(self.session, ready=lambda: True, interactive=False)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("setup-gpu-ocr", self.lines[0])
        self.assertIsNone(setup.gpu_ocr_choice())
        self.assertEqual(setup.transcribe_lock(), "requirements.lock.txt")

    def test_an_empty_answer_is_no_and_is_not_asked_again(self):
        with patch.object(setup, "setup_gpu_ocr") as install:
            setup.offer_gpu_ocr(self.session, ready=lambda: True, interactive=True, ask=lambda _: "")
            install.assert_not_called()
            self.assertIs(setup.gpu_ocr_choice(), False)
            self.lines.clear()
            setup.offer_gpu_ocr(self.session, ready=lambda: True, interactive=True, ask=lambda _: "y")
            install.assert_not_called()
        self.assertEqual(self.lines, [])

    def test_yes_installs_and_later_setups_keep_the_gpu_lock(self):
        with patch.object(setup, "setup_gpu_ocr", side_effect=lambda session: setup.record_gpu_ocr_choice(True)) as install:
            setup.offer_gpu_ocr(self.session, ready=lambda: True, interactive=True, ask=lambda _: "Y")
        install.assert_called_once()
        self.assertEqual(setup.transcribe_lock(), setup.GPU_OCR_LOCK)

    def test_setup_reaches_the_real_detector_as_the_launcher_loads_it(self):
        # The other tests pass `ready`, and this process imported video_workflow long ago;
        # only a fresh project Python, set up like launcher.py, sees what `run.ps1 setup` sees.
        python = setup._python_for("bootstrap-lock.json")[1]
        driver = ("import pathlib, sys; sys.path.insert(0, sys.argv[1]); import setup; "
                  "setup.GPU_OCR_CHOICE = pathlib.Path(sys.argv[2]); "
                  "setup.offer_gpu_ocr(type('Session', (), {'say': lambda _, text: None})(), interactive=False); "
                  "print(sys.modules['video_workflow.system_components'].__file__)")
        done = subprocess.run([str(python if python.is_file() else sys.executable), "-X", "utf8", "-I", "-B",
                               "-c", driver, str(ROOT / "system/bootstrap"), self.folder.name + "/gpu-ocr.json"],
                              capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(done.stdout.strip()), ROOT / "system/src/video_workflow/system_components.py")

    def test_machines_without_nvidia_are_not_asked(self):
        setup.offer_gpu_ocr(self.session, ready=lambda: False, interactive=True, ask=lambda _: self.fail("asked"))
        self.assertEqual(self.lines, [])

    def test_the_gpu_lock_differs_from_the_cpu_lock_only_in_onnx_runtime(self):
        def pins(name):
            text = (ROOT / "system/config" / name).read_text(encoding="utf-8")
            return {line.split(" ")[0] for line in text.splitlines() if line[:1].isalpha()}
        cpu, gpu = pins("requirements.lock.txt"), pins(setup.GPU_OCR_LOCK)
        self.assertEqual({p for p in cpu - gpu}, {p for p in cpu if p.startswith("onnxruntime==")})
        self.assertTrue(all(p.startswith(("onnxruntime-gpu==", "nvidia-")) for p in gpu - cpu))


if __name__ == "__main__":
    unittest.main()
