"""Read-only checks of system-level components that cannot live in .runtime.

Nothing here installs or changes anything. AGENTS.md routes any install through
the user's approval and the guide in README.md.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

GUIDE = "README.md#系統元件與安裝指引"
VC_RUNTIME = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcomp140.dll")
MIN_DRIVER_CUDA = (11, 8)


def _system32():
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _driver_cuda(run=subprocess.run):
    """Highest CUDA version the installed NVIDIA driver supports, from nvidia-smi."""
    smi = shutil.which("nvidia-smi") or _system32() / "nvidia-smi.exe"
    if not Path(smi).is_file():
        return None
    try:
        result = run([str(smi)], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Newer drivers print "CUDA UMD Version" where older ones print "CUDA Version".
    match = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", result.stdout or "")
    return (int(match[1]), int(match[2])) if match else None


def _browser():
    """Edge or Chrome where Playwright's msedge/chrome channels look for them."""
    candidates = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                           Path(base) / "Google/Chrome/Application/chrome.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)


def _word():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"):
            return True
    except (ImportError, OSError):
        return False


def cuda_ready(run=subprocess.run):
    cuda = _driver_cuda(run)
    return cuda is not None and cuda >= MIN_DRIVER_CUDA


def check(run=subprocess.run):
    missing = [name for name in VC_RUNTIME if not (_system32() / name).is_file()]
    cuda = _driver_cuda(run)
    browser = _browser()
    return {
        "vc_runtime": {"need": "必要", "ok": not missing, "missing": missing},
        "nvidia_driver": {"need": "建議；沒有時語音辨識自動改用 CPU，較慢", "ok": cuda is not None and cuda >= MIN_DRIVER_CUDA,
                          "driver_cuda": ".".join(map(str, cuda)) if cuda else None},
        "browser": {"need": "製作講義時必要", "ok": browser is not None, "path": browser},
        "word": {"need": "選用（講義預覽）", "ok": _word()},
        "guide": GUIDE,
    }
