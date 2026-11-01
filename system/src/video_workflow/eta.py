"""How long local processing should take on this machine, learned from its own past runs.

Rates are seconds of work per minute of video. Until a machine has a few finished
runs of its own, the defaults below (measured on an RTX 3070 with a 12-thread CPU)
stand in. The estimate tells an agent how long to wait on the running command; it
is never a reason to sleep, so finishing early or late costs nothing extra.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import statistics

DEFAULT_RATES = {("asr", "cuda"): 3.2, ("asr", "cpu"): 64.0, ("visuals", "cpu"): 21.0, ("visuals", "cuda"): 10.0}
RECENT = 10
MIN_SAMPLES = 3


def elapsed(stage):
    try:
        return (datetime.fromisoformat(stage["completed_at"]) - datetime.fromisoformat(stage["started_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def history(runs_dir):
    """(stage, device) -> rates of this machine's finished runs, newest first."""
    samples = {}
    for state_path in Path(runs_dir).glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            minutes = float(json.loads((state_path.parent / "probe.json").read_text(encoding="utf-8"))["duration"]) / 60
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if minutes <= 0:
            continue
        devices = {"asr": state.get("asr_device") or state.get("config", {}).get("device"),
                   "visuals": state.get("ocr_device", "cpu")}
        for name, device in devices.items():
            stage = state.get("stages", {}).get(name, {})
            seconds = elapsed(stage) if stage.get("status") == "done" else None
            if seconds and device in ("cuda", "cpu"):
                samples.setdefault((name, device), []).append((stage["completed_at"], seconds / minutes))
    return {key: [rate for _, rate in sorted(values, reverse=True)] for key, values in samples.items()}


def rate(rates, stage, device):
    """Median of the recent runs on this machine, or the default when there are too few."""
    recent = rates.get((stage, device), [])[:RECENT]
    if len(recent) >= MIN_SAMPLES:
        return statistics.median(recent), len(recent)
    return DEFAULT_RATES[(stage, device)], 0


def estimate(runs_dir, minutes, devices, stages=("asr", "visuals")):
    """Seconds per remaining stage, plus how many of this machine's runs the estimate rests on."""
    rates = history(runs_dir)
    result, based_on = {}, []
    for stage in stages:
        value, count = rate(rates, stage, devices[stage])
        result[stage] = value * minutes
        based_on.append(count)
    return result, min(based_on) if based_on else 0


def describe(seconds):
    minutes = seconds / 60
    return "不到 1 分鐘" if minutes < 1 else f"約 {round(minutes)} 分鐘"
