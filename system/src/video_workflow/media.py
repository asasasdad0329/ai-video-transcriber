"""Local media adapters. Raw speech and OCR evidence are never rewritten."""
from __future__ import annotations

import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import wave


class MediaError(RuntimeError):
    """A media stage failed or its claimed output could not be verified."""


def _file(path: Path) -> Path:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _run(args: list[str], cwd: Path, timeout: float,
         log_prefix: Path | None = None) -> subprocess.CompletedProcess:
    """Only the child process sees the executable's private DLL directory."""
    env = os.environ.copy()
    env["PATH"] = str(Path(args[0]).parent) + os.pathsep + env.get("PATH", "")
    kwargs = {"cwd": str(cwd), "env": env, "shell": False,
              "stdin": subprocess.DEVNULL, "capture_output": True,
              "timeout": timeout}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        _save_logs(log_prefix, exc.stdout or b"", exc.stderr or b"")
        raise MediaError(f"{Path(args[0]).name} timed out after {timeout:.0f}s; logs: {log_prefix}") from exc
    _save_logs(log_prefix, result.stdout, result.stderr)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[-2500:]
        raise MediaError(f"{Path(args[0]).name} exited {result.returncode}: {detail}")
    return result


def _save_logs(prefix: Path | None, stdout: bytes, stderr: bytes) -> None:
    if prefix is not None:
        prefix.with_suffix(".stdout.log").write_bytes(stdout)
        prefix.with_suffix(".stderr.log").write_bytes(stderr)


def _number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise MediaError(f"Non-finite {label}: {value!r}")
    return number


def probe(ffprobe: Path, source: Path) -> dict:
    ffprobe, source = _file(ffprobe), _file(source)
    result = _run([str(ffprobe), "-v", "error", "-show_format", "-show_streams",
                   "-of", "json", str(source)], source.parent, 120)
    try:
        data = json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise MediaError("ffprobe did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise MediaError("ffprobe JSON is not an object")
    duration = data.get("format", {}).get("duration")
    if duration is None:
        values = [s.get("duration") for s in data.get("streams", [])
                  if s.get("duration") not in (None, "N/A")]
        if not values:
            raise MediaError("ffprobe did not report a duration")
        duration = max(_number(v, "stream duration") for v in values)
    data["duration"] = _number(duration, "duration")
    if data["duration"] <= 0:
        raise MediaError("Media duration must be positive")
    return data


def extract_audio(ffmpeg: Path, source: Path, out: Path) -> None:
    ffmpeg, source = _file(ffmpeg), _file(source)
    out = Path(out).resolve()
    if out.exists() or out == source:
        raise FileExistsError(f"Refusing to overwrite audio output: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-i", str(source),
          "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", "-f", "wav", str(out)], out.parent, 7200,
         out.parent / (out.stem + ".extract"))
    try:
        with wave.open(str(out), "rb") as audio:
            valid = (audio.getnchannels() == 1 and audio.getframerate() == 16000
                     and audio.getsampwidth() == 2 and audio.getnframes() > 0)
    except (wave.Error, OSError) as exc:
        raise MediaError(f"FFmpeg produced invalid WAV: {out}") from exc
    if not valid:
        raise MediaError("Audio output is not non-empty 16 kHz mono 16-bit PCM")


def _time_range(item: dict, required: bool = True) -> tuple:
    offsets = item.get("offsets")
    if isinstance(offsets, dict):
        start = _number(offsets.get("from"), "start offset") / 1000
        end = _number(offsets.get("to"), "end offset") / 1000
    else:
        stamps = item.get("timestamps")
        if not isinstance(stamps, dict):
            if not required:
                return None, None
            raise MediaError("Segment is missing source timestamps")
        start, end = (_parse_timestamp(stamps[key]) for key in ("from", "to"))
    if start < 0 or end < start:
        raise MediaError(f"Invalid source time range: {start}..{end}")
    return start, end


def _parse_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", str(value))
    if not match:
        raise MediaError(f"Invalid timestamp: {value!r}")
    h, m, s, ms = map(int, match.groups())
    if m >= 60 or s >= 60:
        raise MediaError(f"Invalid timestamp: {value!r}")
    return h * 3600 + m * 60 + s + ms / 1000


def _normalize_transcript(raw: dict, language: str) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("transcription"), list):
        raise MediaError("whisper.cpp JSON lacks a transcription array")
    segments = []
    for index, item in enumerate(raw["transcription"], 1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise MediaError("Invalid transcript segment")
        start, end = _time_range(item)
        tokens = []
        for token in item.get("tokens", []):
            if not isinstance(token.get("text"), str):
                raise MediaError("Invalid token text")
            p = token.get("p")
            if p is not None:
                p = _number(p, "token probability")
                if not 0 <= p <= 1:
                    raise MediaError("Token probability outside [0,1]")
            ts, te = _time_range(token, required=False)
            tokens.append({"text": token["text"], "p": p, "start": ts, "end": te})
        segments.append({"id": f"s{index:06d}", "start": start, "end": end,
                         "text": item["text"], "tokens": tokens})
    return {"engine": "whisper.cpp", "language": raw.get("result", {}).get("language", language),
            "segments": segments,
            "confidence_kind": "uncalibrated_decoder_token_probability"}


def _gpu_evidence(log: str) -> list[str]:
    """Compiled CUDA support or device enumeration alone are insufficient."""
    lines = log.splitlines()
    backend = [line for line in lines
               if re.search(r"using CUDA\d* backend", line, re.I)]
    allocation = [line for line in lines
                  if re.search(r"CUDA\d*\s+total size\s*=\s*[\d.]+", line, re.I)]
    failures = re.search(r"no GPU found|failed to initialize CUDA\d* backend", log, re.I)
    return backend + allocation if backend and allocation and not failures else []


def transcribe(whisper: Path, model: Path, audio: Path, out_dir: Path,
               language: str = "zh", device: str = "auto", threads: int = 4,
               prompt: str = "", beam_size: int = 5, *, gpu_ready=None) -> dict:
    """device "auto" uses the GPU when the driver supports it and the CPU otherwise.

    Only an explicit "cuda" fails without verified GPU use; "auto" records
    whichever device actually ran, so a machine without NVIDIA still finishes.
    """
    whisper, model, audio = _file(whisper), _file(model), _file(audio)
    if device not in ("auto", "cuda", "cpu"):
        raise ValueError("device must be 'auto', 'cuda' or 'cpu'")
    if threads < 1:
        raise ValueError("threads must be positive")
    if not 1 <= beam_size <= 16:
        raise ValueError("beam_size must be between 1 and 16")
    if os.name == "nt" and prompt and not prompt.isascii():
        raise ValueError("This Windows CLI cannot safely pass a Unicode prompt; use wider-beam recheck and keep the glossary as review evidence")
    if not language or not re.fullmatch(r"[A-Za-z-]+", language):
        raise ValueError("Invalid language code")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = out_dir / "transcript.json"
    if any((out_dir / name).exists() for name in ("whisper.json", "whisper-cpu.json", "transcript.json")):
        raise FileExistsError("Existing transcript output; use a fresh run directory")
    try:
        with wave.open(str(audio), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
    except (wave.Error, OSError) as exc:
        raise MediaError("transcribe expects a valid WAV from extract_audio") from exc
    if device == "auto":
        from .system_components import cuda_ready
        use_gpu = (gpu_ready or cuda_ready)()
    else:
        use_gpu = device == "cuda"
    # The CPU is bound by threads; with the GPU a few are enough.
    cpu_threads = max(threads, os.cpu_count() or threads)

    def attempt(name, gpu):
        # Windows' narrow argv in some official builds corrupts absolute CJK paths.
        # The child cwd is set through CreateProcessW; project-internal relative
        # arguments avoid that conversion without changing the system code page.
        args = [str(whisper), "-m", os.path.relpath(model, out_dir),
                "-f", os.path.relpath(audio, out_dir), "-l", language,
                "-t", str(threads if gpu else cpu_threads), "-bs", str(beam_size), "-ojf", "-of", name]
        if not gpu:
            args.append("-ng")
        if prompt:
            args += ["--prompt", prompt]
        return out_dir / name, _run(args, out_dir, max(600, min(43200, duration * 10 + 300)), out_dir / name)

    gpu_failure = None
    try:
        prefix, result = attempt("whisper", use_gpu)
    except MediaError as exc:
        # A driver that looks ready can still fail (an old card, too little memory).
        if device != "auto" or not use_gpu:
            raise
        gpu_failure, use_gpu = str(exc)[-500:], False
        prefix, result = attempt("whisper-cpu", False)
    raw_path = prefix.with_suffix(".json")
    log = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")
    evidence = _gpu_evidence(log) if use_gpu else []
    if device == "cuda" and not evidence:
        raise MediaError("CUDA was requested, but successful CUDA backend and model allocation "
                         f"were not verified. Raw output/logs retained in {out_dir}")
    if not raw_path.is_file():
        raise MediaError("whisper.cpp returned success without producing JSON")
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise MediaError(f"Invalid whisper.cpp JSON: {raw_path}") from exc
    data = _normalize_transcript(raw, language)
    actual = "cuda" if evidence else "cpu"
    data.update({"requested_device": device, "actual_device": actual,
                 "threads": threads if use_gpu else cpu_threads,
                 "beam_size": beam_size, "prompt_applied": bool(prompt),
                 "backend_evidence": evidence, "raw_json": str(raw_path),
                 "stdout_log": str(prefix.with_suffix(".stdout.log")),
                 "stderr_log": str(prefix.with_suffix(".stderr.log"))})
    if gpu_failure:
        data["gpu_failure"] = gpu_failure
    normalized_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _dhash(path: Path) -> int:
    """Similarity hint only: small text edits can have an identical dHash."""
    from PIL import Image
    with Image.open(path) as image:
        pixels = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return value


def _pixel_hash(path: Path) -> str:
    """Hash full decoded pixels and dimensions, without resize or quantization."""
    import hashlib
    from PIL import Image
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        digest = hashlib.sha256(f"{rgba.width}x{rgba.height}:RGBA:".encode("ascii"))
        digest.update(rgba.tobytes())
    return digest.hexdigest()


def _ocr_engine():
    """All three ONNX models must already be in the installed package."""
    module = importlib.import_module("rapidocr_onnxruntime")
    model_dir = Path(module.__file__).resolve().parent / "models"
    paths = {}
    for role in ("det", "cls", "rec"):
        candidates = sorted(model_dir.glob(f"*{role}*.onnx"))
        if len(candidates) != 1:
            raise MediaError(f"Expected one bundled {role} ONNX model in {model_dir}; "
                             "automatic model download is disabled")
        paths[f"{role}_model_path"] = str(candidates[0])
        paths[f"{role}_use_cuda"] = False
        paths[f"{role}_use_dml"] = False
    engine = module.RapidOCR(**paths)
    sessions = (engine.text_det.infer.session, engine.text_cls.infer.session,
                engine.text_rec.session.session)
    if any(session.get_providers() != ["CPUExecutionProvider"] for session in sessions):
        raise MediaError("RapidOCR CPU-only execution providers could not be verified")
    return engine, paths


def _ocr_frame(engine, path: Path) -> list[dict]:
    # PIL avoids OpenCV imread's Windows non-ASCII filename limitations.
    import numpy as np
    from PIL import Image
    with Image.open(path) as image:
        bgr = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
    result, _elapsed = engine(bgr)
    boxes = []
    for box, text, score in result or []:
        confidence = _number(score, "OCR confidence")
        if not 0 <= confidence <= 1:
            raise MediaError("OCR confidence outside [0,1]")
        boxes.append({"box": [[float(x), float(y)] for x, y in box],
                      "text": str(text), "confidence": confidence})
    return boxes


def extract_visuals(ffmpeg: Path, source: Path, out_dir: Path,
                    interval: float = 10.0, dedup_threshold: int = 5,
                    *, start: float = 0.0, end: float | None = None) -> dict:
    ffmpeg, source = _file(ffmpeg), _file(source)
    interval = _number(interval, "sample interval")
    if interval <= 0 or not isinstance(dedup_threshold, int) or not 0 <= dedup_threshold <= 64:
        raise ValueError("interval must be positive; dedup_threshold must be 0..64")
    start = _number(start, "visual range start")
    end = _number(end, "visual range end") if end is not None else None
    if start < 0 or (end is not None and end <= start):
        raise ValueError("invalid visual sampling range")
    out_dir = Path(out_dir).resolve()
    frame_dir = out_dir / "frames"
    if frame_dir.exists() and any(frame_dir.iterdir()):
        raise FileExistsError("Existing frames; use a fresh visual output directory")
    frame_dir.mkdir(parents=True, exist_ok=True)
    # Select scene changes as well as the first available frame at/after the
    # interval since the previous selection. PTS are recorded after selection;
    # they are never reconstructed from an assumed constant-rate frame grid.
    scene_threshold = 0.25
    selection = (f"isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.9g})"
                 f"+gt(scene,{scene_threshold})")
    # trim preserves source timestamps; do not use setpts or input seeking that rebases PTS.
    trim = f"trim=start={start}" + (f":end={end}" if end is not None else "")
    vf = (trim + "," if start or end is not None else "") + f"select='{selection}',showinfo"
    result = _run([str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-i", str(source),
                   "-map", "0:v:0", "-an", "-vf", vf, "-vsync", "0",
                   "-compression_level", "3", str(frame_dir / "frame-%06d.png")],
                  out_dir, 14400, out_dir / "extract_frames")
    log = result.stderr.decode("utf-8", errors="replace")
    records = re.findall(r"\[Parsed_showinfo[^\]]*\].*?\bn:\s*(\d+)\s+pts:.*?pts_time:([\d.eE+\-]+)", log)
    times = [_number(time, "selected frame PTS") for _, time in records]
    images = sorted(frame_dir.glob("frame-*.png"))
    if not images or len(images) != len(times):
        raise MediaError(f"Cannot verify extracted frame timestamps: {len(images)} images, {len(times)} PTS records")
    if ([int(index) for index, _ in records] != list(range(len(records)))
            or any(time < 0 for time in times)
            or any(current < previous for previous, current in zip(times, times[1:]))):
        raise MediaError("Selected frame PTS records are out of order or invalid")
    engine, ocr_config = _ocr_engine()
    frames, representatives, exact_pixels = [], [], {}
    for index, (path, timestamp) in enumerate(zip(images, times), 1):
        fingerprint = _dhash(path)
        pixel_hash = _pixel_hash(path)
        duplicate = exact_pixels.get(pixel_hash)
        similar = next((frame for previous_hash, frame in reversed(representatives)
                        if (previous_hash ^ fingerprint).bit_count() <= dedup_threshold), None)
        fid = f"f{index:06d}"
        boxes = [] if duplicate else _ocr_frame(engine, path)
        frame = {"id": fid, "time": timestamp, "path": str(path.resolve()),
                 "text": "\n".join(box["text"] for box in boxes),
                 "confidence": (sum(box["confidence"] for box in boxes) / len(boxes) if boxes else None),
                 "boxes": boxes, "duplicate_of": duplicate["id"] if duplicate else None,
                 "ocr_status": "reused_exact_pixel_duplicate" if duplicate else "completed",
                 "dhash": f"{fingerprint:016x}", "pixel_sha256": pixel_hash,
                 "perceptually_similar_to": similar["id"] if similar and not duplicate else None,
                 "ocr_evidence_time": duplicate["time"] if duplicate else timestamp}
        if duplicate:
            # Identical decoded pixels permit reuse, but do not constitute a
            # second independent OCR observation. Retain the original source.
            import copy
            frame["text"] = duplicate["text"]
            frame["confidence"] = duplicate["confidence"]
            frame["boxes"] = copy.deepcopy(duplicate["boxes"])
            frame["text_source_frame"] = duplicate["id"]
            frame["text_source_time"] = duplicate["time"]
        else:
            representatives.append((fingerprint, frame))
            exact_pixels[pixel_hash] = frame
        frames.append(frame)
    coverage = {"mode": "scene_changes_or_interval", "interval_seconds": interval,
                "requested_range": {"start": start, "end": end},
                "timestamp_source": "ffmpeg_source_frame_pts", "exhaustive": False,
                "scene_detection": True, "scene_detection_method": "ffmpeg_select_scene",
                "scene_threshold": scene_threshold, "selection_expression": selection,
                "sampled_frames": len(frames),
                "ocr_frames": len(representatives), "deduplicated_frames": len(frames) - len(representatives),
                "first_sample_seconds": times[0], "last_sample_seconds": times[-1],
                "max_observed_sample_gap_seconds": max((current - previous for previous, current in zip(times, times[1:])), default=0.0),
                "dedup_method": "sha256_full_decoded_rgba_pixels_and_dimensions",
                "dedup_threshold": dedup_threshold, "dedup_is_lossy": False,
                "perceptual_hash_role": "similarity_annotation_only_never_skips_ocr",
                "limitations": ["Only selected frames are OCR-inspected; brief text changes between samples may be missed.",
                                "Scene threshold is a pixel-change heuristic, not a guarantee that every text or slide change is selected.",
                                "Interval selection uses the next available frame; timestamp gaps can exceed the requested interval by a frame or due to source gaps.",
                                "Exact-pixel duplicates reuse OCR with their original source frame and time; they are not independent OCR observations.",
                                "OCR captures visible text, not the meaning of diagrams or motion."]}
    data = {"engine": "rapidocr_onnxruntime", "device": "cpu", "model_paths": {
        k: v for k, v in ocr_config.items() if k.endswith("_model_path")},
            "frames": frames, "coverage": coverage}
    (out_dir / "visuals.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
