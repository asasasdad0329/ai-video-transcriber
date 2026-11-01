import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from video_workflow import media

FFMPEG = Path(__file__).resolve().parents[2] / ".runtime/tools/ffmpeg/ffmpeg.exe"


class TranscriptEvidenceTests(unittest.TestCase):
    def test_chinese_raw_text_and_probability_preserved(self):
        raw = {"result": {"language": "zh"}, "transcription": [
            {"offsets": {"from": 1200, "to": 3500}, "text": " 原始 錯字。\n",
             "tokens": [{"text": "錯", "p": 0.125,
                         "offsets": {"from": 1700, "to": 1850}},
                        {"text": "[未對齊]", "p": 0.5}]}]}
        result = media._normalize_transcript(raw, "auto")
        seg = result["segments"][0]
        self.assertEqual(seg["text"], " 原始 錯字。\n")
        self.assertEqual((seg["start"], seg["end"]), (1.2, 3.5))
        self.assertEqual(seg["tokens"][0], {"text": "錯", "p": 0.125, "start": 1.7, "end": 1.85})
        self.assertIsNone(seg["tokens"][1]["start"])
        self.assertEqual(result["language"], "zh")

    def test_invalid_time_or_confidence_does_not_become_evidence(self):
        for segment in [
            {"text": "x", "offsets": {"from": 2, "to": 1}},
            {"text": "x", "offsets": {"from": 0, "to": 1000}, "tokens": [{"text": "x", "p": float("nan")}]},
            {"text": "x"},
        ]:
            with self.subTest(segment=segment), self.assertRaises(media.MediaError):
                media._normalize_transcript({"transcription": [segment]}, "zh")

    def test_cuda_build_or_device_discovery_is_not_gpu_use(self):
        for log in ["CUDA = 1", "ggml_cuda_init: found 1 CUDA devices", "whisper_backend_init_gpu: using CUDA0 backend"]:
            self.assertFalse(media._gpu_evidence(log))
        self.assertTrue(media._gpu_evidence("whisper_backend_init_gpu: using CUDA0 backend\nwhisper_model_load: CUDA0 total size = 1450.50 MB"))
        self.assertFalse(media._gpu_evidence("using CUDA0 backend\nCUDA0 total size = 20 MB\nfailed to initialize CUDA0 backend"))


class MediaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "工具 空間" / "ffmpeg.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.source = self.root / "中文 影片.mp4"
        self.source.write_bytes(b"original")

    def tearDown(self):
        self.temp.cleanup()

    def test_subprocess_isolated_and_failure_logs_retained(self):
        prefix = self.root / "run"
        import os
        original_path = os.environ.get("PATH")
        with patch.object(media.subprocess, "run", return_value=subprocess.CompletedProcess([], 7, b"out", b"failure")) as run:
            with self.assertRaises(media.MediaError):
                media._run([str(self.exe), str(self.source)], self.root, 3, prefix)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.args[0][1], str(self.source))
        self.assertTrue(run.call_args.kwargs["env"]["PATH"].startswith(str(self.exe.parent)))
        self.assertEqual(os.environ.get("PATH"), original_path)
        self.assertEqual(prefix.with_suffix(".stderr.log").read_bytes(), b"failure")

    def test_audio_refuses_overwriting_input(self):
        with patch.object(media, "_run") as run, self.assertRaises(FileExistsError):
            media.extract_audio(self.exe, self.source, self.source)
        run.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_probe_stream_duration_fallback_and_original_fields(self):
        raw = {"streams": [{"codec_type": "video", "duration": "3.75"}], "format": {"tags": {"title": "中文"}}}
        with patch.object(media, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(raw).encode(), b"")):
            result = media.probe(self.exe, self.source)
        self.assertEqual(result["duration"], 3.75)
        self.assertEqual(result["format"], raw["format"])

    def test_silent_cpu_fallback_keeps_raw_but_fails_cuda_request(self):
        audio = self.root / "speech.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 160)
        model = self.root / "model.bin"
        model.touch()
        output = self.root / "asr"
        def fake_run(*args):
            (output / "whisper.json").write_text('{"transcription": []}', encoding="utf-8")
            return subprocess.CompletedProcess([], 0, b"", b"no GPU found")
        with patch.object(media, "_run", side_effect=fake_run), self.assertRaises(media.MediaError):
            media.transcribe(self.exe, model, audio, output, device="cuda")
        self.assertTrue((output / "whisper.json").is_file())
        self.assertFalse((output / "transcript.json").exists())

    def _speech(self):
        audio = self.root / "speech.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 160)
        model = self.root / "model.bin"
        model.touch()
        return model, audio

    def _whisper(self, output, results):
        calls = []
        def fake_run(args, cwd, timeout, prefix):
            calls.append(args)
            outcome = results[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            (output / (args[args.index("-of") + 1] + ".json")).write_text('{"transcription": []}', encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, b"", outcome)
        return calls, fake_run

    def test_auto_device_without_driver_runs_on_all_cpu_threads(self):
        model, audio = self._speech()
        output = self.root / "asr"
        calls, fake_run = self._whisper(output, [b"no GPU found"])
        with patch.object(media, "_run", side_effect=fake_run), patch.object(media.os, "cpu_count", return_value=12):
            result = media.transcribe(self.exe, model, audio, output, gpu_ready=lambda: False)
        self.assertIn("-ng", calls[0])
        self.assertEqual(calls[0][calls[0].index("-t") + 1], "12")
        self.assertEqual((result["requested_device"], result["actual_device"], result["threads"]), ("auto", "cpu", 12))

    def test_auto_device_records_the_device_that_actually_ran(self):
        gpu_log = b"using CUDA0 backend\nCUDA0 total size = 1450.50 MB"
        for log, actual in ((gpu_log, "cuda"), (b"failed to initialize CUDA: no CUDA-capable device is detected", "cpu")):
            with self.subTest(actual=actual):
                model, audio = self._speech()
                output = self.root / ("asr-" + actual)
                calls, fake_run = self._whisper(output, [log])
                with patch.object(media, "_run", side_effect=fake_run):
                    result = media.transcribe(self.exe, model, audio, output, gpu_ready=lambda: True)
                self.assertNotIn("-ng", calls[0])
                self.assertEqual(result["actual_device"], actual)

    def test_auto_device_retries_on_the_cpu_when_the_gpu_run_fails(self):
        model, audio = self._speech()
        output = self.root / "asr"
        calls, fake_run = self._whisper(output, [media.MediaError("whisper-cli.exe exited 1: out of memory"), b"no GPU found"])
        with patch.object(media, "_run", side_effect=fake_run):
            result = media.transcribe(self.exe, model, audio, output, gpu_ready=lambda: True)
        self.assertEqual(len(calls), 2)
        self.assertIn("-ng", calls[1])
        self.assertEqual(calls[1][calls[1].index("-of") + 1], "whisper-cpu")
        self.assertEqual(result["actual_device"], "cpu")
        self.assertIn("out of memory", result["gpu_failure"])
        self.assertTrue(result["raw_json"].endswith("whisper-cpu.json"))

    def test_a_character_whisper_left_unfinished_goes_to_review_not_the_whole_run(self):
        # Byte patterns from real runs: 瘤 cut after two of its three bytes, and a stray
        # continuation byte after 银. Strict decoding used to fail the whole run on either.
        model, audio = self._speech()
        output = self.root / "asr"
        raw = (b'{"transcription": ['
               b'{"offsets": {"from": 0, "to": 1000}, "text": "\xe5\xad\x90\xe5\xae\xab\xe8\x82\x8c\xe7\x98",'
               b' "tokens": [{"text": "\xe7\x98[_TT_50]", "p": 0.4}]},'
               b'{"offsets": {"from": 1000, "to": 2000}, "text": "\xe9\x93\xb6\xb6\xe8\xa1\x8c", "tokens": []}]}')
        def fake_run(args, cwd, timeout, prefix):
            (output / "whisper.json").write_bytes(raw)
            return subprocess.CompletedProcess(args, 0, b"", b"no GPU found")
        with patch.object(media, "_run", side_effect=fake_run):
            result = media.transcribe(self.exe, model, audio, output, gpu_ready=lambda: False)
        self.assertEqual([s["text"] for s in result["segments"]], ["子宫肌�", "银�行"])

    def test_explicit_cuda_does_not_fall_back(self):
        model, audio = self._speech()
        output = self.root / "asr"
        calls, fake_run = self._whisper(output, [media.MediaError("whisper-cli.exe exited 1: out of memory")])
        with patch.object(media, "_run", side_effect=fake_run), self.assertRaises(media.MediaError):
            media.transcribe(self.exe, model, audio, output, device="cuda")
        self.assertEqual(len(calls), 1)

    def test_exact_pixel_duplicates_keep_timeline_and_ocr_source(self):
        output = self.root / "visuals"
        def fake_run(*args):
            for n in range(1, 4):
                (output / "frames" / f"frame-{n:06d}.png").touch()
            log = "\n".join(f"[Parsed_showinfo_1 @ a] n: {n} pts: {n*100} pts_time:{t} fmt:rgb24" for n, t in enumerate([0.0, 10.04, 20.08]))
            return subprocess.CompletedProcess([], 0, b"", log.encode())
        with patch.object(media, "_run", side_effect=fake_run), \
             patch.object(media, "_ocr_engine", return_value=(object(), {})), \
             patch.object(media, "_dhash", side_effect=[0, 0, 255]), \
             patch.object(media, "_pixel_hash", side_effect=["same", "same", "different"]), \
             patch.object(media, "_ocr_frame", return_value=[{"text": "投影片", "confidence": 0.9, "box": [[0, 0]]}]) as ocr:
            result = media.extract_visuals(self.exe, self.source, output)
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(len(result["frames"]), 3)
        self.assertEqual(result["frames"][1]["duplicate_of"], "f000001")
        self.assertEqual(result["frames"][1]["time"], 10.04)
        self.assertEqual(result["frames"][1]["ocr_status"], "reused_exact_pixel_duplicate")
        self.assertEqual(result["frames"][1]["text_source_time"], 0.0)
        self.assertEqual(result["frames"][1]["ocr_evidence_time"], 0.0)
        self.assertEqual(result["frames"][1]["boxes"], result["frames"][0]["boxes"])
        self.assertFalse(result["coverage"]["exhaustive"])

        self.assertFalse(result["coverage"]["dedup_is_lossy"])
        self.assertTrue(result["coverage"]["scene_detection"])
        self.assertEqual(result["coverage"]["scene_threshold"], 0.25)

    def test_same_dhash_with_changed_pixels_never_skips_ocr(self):
        from PIL import Image
        output = self.root / "visuals"
        def fake_run(args, *unused):
            self.assertIn("gt(scene,0.25)", args[args.index("-vf") + 1])
            self.assertIn("gte(t-prev_selected_t,10)", args[args.index("-vf") + 1])
            Image.new("RGB", (64, 64), (255, 255, 255)).save(output / "frames" / "frame-000001.png")
            Image.new("RGB", (64, 64), (254, 254, 254)).save(output / "frames" / "frame-000002.png")
            log = "[Parsed_showinfo_1 @ a] n: 0 pts: 0 pts_time:0\n[Parsed_showinfo_1 @ a] n: 1 pts: 104 pts_time:10.4"
            return subprocess.CompletedProcess([], 0, b"", log.encode())
        with patch.object(media, "_run", side_effect=fake_run), \
             patch.object(media, "_ocr_engine", return_value=(object(), {})), \
             patch.object(media, "_ocr_frame", side_effect=[[{"text": "第一張", "confidence": 0.9, "box": []}], [{"text": "第二張", "confidence": 0.9, "box": []}]]) as ocr:
            result = media.extract_visuals(self.exe, self.source, output)
        self.assertEqual(ocr.call_count, 2)
        first, second = result["frames"]
        self.assertEqual(first["dhash"], second["dhash"])
        self.assertNotEqual(first["pixel_sha256"], second["pixel_sha256"])
        self.assertIsNone(second["duplicate_of"])
        self.assertEqual(second["perceptually_similar_to"], first["id"])
        self.assertEqual(second["text"], "第二張")
        self.assertEqual(second["ocr_evidence_time"], 10.4)

    def test_pixel_hash_uses_dimensions_and_pixels_not_encoded_png_bytes(self):
        from PIL import Image
        a, b, different_size = (self.root / name for name in ("a.png", "b.png", "size.png"))
        image = Image.new("RGB", (20, 10), "white")
        image.save(a, compress_level=0)
        image.save(b, compress_level=9)
        Image.new("RGB", (10, 20), "white").save(different_size)
        self.assertNotEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(media._pixel_hash(a), media._pixel_hash(b))
        self.assertNotEqual(media._pixel_hash(a), media._pixel_hash(different_size))

    @unittest.skipUnless(FFMPEG.is_file(), "project FFmpeg not installed; run setup")
    def test_real_scene_selection_retains_actual_pts_and_interval_backstop(self):
        ffmpeg = FFMPEG
        source = self.root / "synthetic-scene.mkv"
        media._run([str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-f", "lavfi", "-i", "color=black:size=32x32:rate=10:duration=12", "-f", "lavfi", "-i", "color=white:size=32x32:rate=10:duration=12", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "ffv1", str(source)], self.root, 30)
        with patch.object(media, "_ocr_engine", return_value=(object(), {})), \
             patch.object(media, "_ocr_frame", return_value=[]):
            result = media.extract_visuals(ffmpeg, source, self.root / "real-visuals")
        times = [frame["time"] for frame in result["frames"]]
        self.assertEqual(times, [0.0, 10.0, 12.0, 22.0])
        self.assertEqual(result["coverage"]["max_observed_sample_gap_seconds"], 10.0)
        self.assertFalse(result["coverage"]["exhaustive"])
        with patch.object(media, "_ocr_engine", return_value=(object(), {})), \
             patch.object(media, "_ocr_frame", return_value=[]):
            clipped = media.extract_visuals(ffmpeg, source, self.root / "real-clipped", interval=1,
                                             start=12.3, end=14.7)
        self.assertEqual([frame["time"] for frame in clipped["frames"]], [12.3, 13.3, 14.3])

    def test_missing_frame_timestamps_fail_instead_of_inventing_grid(self):
        output = self.root / "visuals"
        def fake_run(*args):
            (output / "frames" / "frame-000001.png").touch()
            return subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(media, "_run", side_effect=fake_run), self.assertRaises(media.MediaError):
            media.extract_visuals(self.exe, self.source, output)


if __name__ == "__main__":
    unittest.main()
