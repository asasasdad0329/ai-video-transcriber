"""Exercise real GPU ASR and CPU OCR on a short local video excerpt."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system/src"))
from workspace_paths import resolve, RUNS, RECORDS
from video_workflow.media import extract_audio, extract_visuals, transcribe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    source = resolve(args.source)
    if not source.is_file() or not 0 < args.seconds <= 60:
        parser.error("Provide an existing source and a duration in (0, 60].")
    # Each invocation is a new measurement; preserve prior evidence unchanged.
    out = ROOT / RECORDS / "integration-smoke" / ("attempt-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=True)
    clip = out / "clip.mp4"
    ffmpeg = ROOT / ".runtime/tools/ffmpeg/ffmpeg.exe"
    if not clip.exists():
        subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", str(source), "-t", str(args.seconds), "-c", "copy", str(clip)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    wav = out / "audio.wav"
    if not wav.exists():
        extract_audio(ffmpeg, clip, wav)
    started = time.monotonic()
    transcript = transcribe(ROOT / ".runtime/tools/whisper/Release/whisper-cli.exe", ROOT / ".runtime/models/whisper/ggml-large-v3-turbo-q5_0.bin", wav, out / "asr", "zh", "cuda")
    asr_seconds = time.monotonic() - started
    visuals = extract_visuals(ffmpeg, clip, out / "visuals", interval=min(10, args.seconds / 2))
    result = {"source": source.name, "asr_wall_seconds": asr_seconds, "asr": {key: value for key, value in transcript.items() if key != "segments"}, "segments": len(transcript["segments"]), "frames": len(visuals["frames"]), "ocr_frames": sum(bool(f.get("text")) for f in visuals["frames"]), "passed": bool(transcript["segments"] and visuals["frames"])}
    (out / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
