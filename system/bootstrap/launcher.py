"""Command dispatcher behind run.ps1 and run.cmd.

Runs on the project's private base Python, so it behaves the same whatever shell, PowerShell
edition or system code page started it. Children get argument lists (escaped by subprocess)
and inherit stdout/stderr untouched; only exit codes decide success.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import project_env  # noqa: E402

TRANSCRIBE = ROOT / ".runtime/envs/transcribe/Scripts/python.exe"
PUBLISH = ROOT / ".runtime/envs/publish/Scripts/python.exe"
CATALOG = HERE / "catalog.py"
PUBLISH_COMMANDS = {"datacard", "handout", "publish-doctor", "derived-list"}
CHANGING_COMMANDS = {"process", "resume", "apply", "finalize-visuals", "finish-review", "datacard", "handout"}
NEEDS_SETUP = "轉錄環境還沒安裝：先執行 .\\run.ps1 setup（約 1.5 GB，全部裝在專案的 .runtime 內），再執行這個命令。"
NEEDS_PUBLISH = "資料卡與講義環境還沒安裝：先執行 .\\run.ps1 setup-publish（全部裝在專案的 .runtime 內），再執行這個命令。"


def run(python, *args, quiet=False):
    # -u: output reaches the caller as it is written, not in pipe-sized chunks.
    try:
        return subprocess.run([str(python), "-X", "utf8", "-I", "-B", "-u", *map(str, args)],
                              stdout=subprocess.DEVNULL if quiet else None).returncode
    except KeyboardInterrupt:
        return 130


def main(argv):
    project_env.apply(ROOT, os.environ)
    command = argv[0] if argv else None
    if command == "_args":
        # Self-test for the shell entry points: echoes exactly the arguments that arrived.
        print(json.dumps(argv[1:], ensure_ascii=False))
        return 0
    if command in ("setup", "setup-publish", "setup-gpu-ocr"):
        import setup
        return setup.main(argv)
    # The index only needs the standard library; the transcribe venv adds OpenCC name matching.
    index_python = next((p for p in (TRANSCRIBE, PUBLISH) if p.is_file()), Path(sys.executable))
    if command == "catalog":
        return run(index_python, CATALOG, *argv[1:])
    if command in PUBLISH_COMMANDS:
        if not PUBLISH.is_file():
            print(NEEDS_PUBLISH)
            return 3
        code = run(PUBLISH, HERE / "publish_workflow.py", *argv)
    elif command == "test":
        if not TRANSCRIBE.is_file():
            print(NEEDS_SETUP)
            return 3
        code = run(TRANSCRIBE, "-m", "unittest", "discover", "-s", ROOT / "system/tests", "-v")
        if PUBLISH.is_file():
            tests = ROOT / "system/tests/publish"
            code = run(PUBLISH, "-m", "unittest", "discover", "-s", tests, "-t", tests, "-v") or code
        else:
            print("略過資料卡／講義測試：publish 環境不存在（.\\run.ps1 setup-publish）。")
    else:
        if not TRANSCRIBE.is_file():
            print(NEEDS_SETUP)
            return 3
        code = run(TRANSCRIBE, HERE / "video_workflow.py", *argv)
    if command in CHANGING_COMMANDS:
        # Keep the work index current; an index failure never changes the command's result.
        if run(index_python, CATALOG, "--quiet", quiet=True):
            print("工作索引沒有更新成功；稍後執行 .\\run.ps1 catalog 可重建。")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
