"""Build the project-local environments: .\\run.ps1 setup and .\\run.ps1 setup-publish.

Runs on the private base Python that run.ps1 installs on first use. Everything lands in
.runtime and the work folders; nothing outside the project is changed. Each step checks what
is already in place, so an interrupted setup resumes by running the same command again.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "system/src"))
import fetch_assets  # noqa: E402
import fetch_publish_assets  # noqa: E402
from workspace_paths import DATACARD_TASKS, ENV, HANDOUT_TASKS, OUTPUTS, PUBLISH_ENV, RUNS  # noqa: E402

UV = ROOT / ".runtime/tools/uv/uv.exe"
FOLDERS = (Path("工作資料/原檔"), Path("工作資料/參考樣本"), RUNS, OUTPUTS / "逐字稿",
           OUTPUTS / "資料卡", OUTPUTS / "講義", Path("工作資料/歸檔"))
PUBLISH_FOLDERS = (DATACARD_TASKS, HANDOUT_TASKS, OUTPUTS / "資料卡", OUTPUTS / "講義")
ISOLATION_CHECK = (
    "import sys, pathlib, json; root=pathlib.Path(sys.prefix).parents[2]; "
    "assert pathlib.Path(sys.base_prefix).resolve().is_relative_to(root/'.runtime'), 'External base Python'; "
    "print(json.dumps({'python': sys.version.split()[0], 'isolated': sys.prefix != sys.base_prefix}))")


class SetupError(Exception):
    pass


class Session:
    """Prints progress and keeps the same lines in .runtime/logs, so a lost console is no loss."""

    def __init__(self, name, offline):
        self.offline = offline
        logs = ROOT / ".runtime/logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log_path = logs / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.log = self.log_path.open("ab")
        self.console = sys.stdout
        sys.stdout = self  # print() from the asset fetchers reaches the log as well

    def write(self, text):
        self.console.write(text)
        self.log.write(text.encode("utf-8"))
        return len(text)

    def flush(self):
        self.console.flush()
        self.log.flush()

    def say(self, text):
        print(text, flush=True)

    def call(self, *args, env=None):
        """Run a tool; stderr is merged into the output because tools report progress there."""
        self.flush()
        process = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   env={**os.environ, **(env or {})})
        for line in process.stdout:
            self.console.buffer.write(line)
            self.console.flush()
            self.log.write(line)
        self.log.flush()
        return process.wait()

    def uv(self, *args, network=False, failure):
        command = [UV, "--no-config", *args] + (["--offline"] if self.offline and network else [])
        code = self.call(*command)
        if code and network and not self.offline:
            # uv trusts its bundled Mozilla roots; a TLS-inspecting proxy's root lives only in the Windows store.
            self.say("改用 Windows 憑證庫重試一次。")
            code = self.call(*command, env={"UV_SYSTEM_CERTS": "1"})
        if code:
            raise SetupError(failure)

    def close(self):
        sys.stdout = self.console
        self.log.close()


def _python_for(lock_name):
    version = json.loads((ROOT / "system/config" / lock_name).read_text(encoding="utf-8"))["python"]
    return version, ROOT / f".runtime/python/cpython-{version}-windows-x86_64-none/python.exe"


def ensure_tools(session, lock_name):
    if not UV.is_file():
        fetch_assets.install("uv", session.offline)
    version, base = _python_for(lock_name)
    if not base.is_file():
        session.uv("python", "install", version, network=True, failure="無法安裝鎖定版本的 Python " + version)
    return base


def venv_matches(venv, base):
    """True when the venv exists and was built on `base` (uv may record a 3.12 alias; compare resolved paths)."""
    config = venv / "pyvenv.cfg"
    if not (venv / "Scripts/python.exe").is_file() or not config.is_file():
        return False
    fields = dict(line.split("=", 1) for line in config.read_text(encoding="utf-8-sig").splitlines() if "=" in line)
    home = next((value.strip() for key, value in fields.items() if key.strip() == "home"), "")
    return bool(home) and Path(home).resolve() == base.resolve().parent


def ensure_venv(session, relative, base):
    venv = ROOT / relative
    if venv_matches(venv, base):
        return venv / "Scripts/python.exe"
    if venv.exists():
        backup = ROOT / ".runtime/backups" / f"{venv.name}-{datetime.now():%Y%m%d%H%M%S%f}"
        if not backup.resolve().is_relative_to(ROOT) or not venv.resolve().is_relative_to(ROOT):
            raise SetupError("venv 備份路徑超出專案資料夾")
        backup.parent.mkdir(parents=True, exist_ok=True)
        session.say("既有環境不是用專案 Python 建的，先備份到 " + backup.relative_to(ROOT).as_posix())
        shutil.move(str(venv), str(backup))
    session.uv("venv", "--python", base, venv, failure="無法建立 " + relative.as_posix())
    return venv / "Scripts/python.exe"


def install_locked(session, python, lock_file):
    session.uv("pip", "sync", "--python", python, "--require-hashes", "--only-binary", ":all:",
               ROOT / "system/config" / lock_file, network=True, failure="鎖定套件安裝失敗：" + lock_file)


def check_isolation(session, python):
    if session.call(python, "-X", "utf8", "-I", "-B", "-c", ISOLATION_CHECK):
        raise SetupError("環境沒有和專案外的 Python 隔離")


def make_folders(folders):
    for folder in folders:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)


def setup_transcribe(session):
    base = ensure_tools(session, "bootstrap-lock.json")
    python = ensure_venv(session, ENV, base)
    install_locked(session, python, "requirements.lock.txt")
    # whisper.cpp's CUDA backend loads cuBLAS from its own folder; no system CUDA Toolkit is needed.
    for key in ("whisper", "cublas", "model", "ffmpeg"):
        fetch_assets.install(key, session.offline)
    check_isolation(session, python)
    make_folders(FOLDERS)
    session.say("轉錄環境已就緒。下一步：.\\run.ps1 doctor")


def setup_publish(session):
    # Shares the private Python and uv with the transcription environment; only adds its own venv.
    base = ensure_tools(session, "publish-lock.json")
    python = ensure_venv(session, PUBLISH_ENV, base)
    install_locked(session, python, "publish-requirements.lock.txt")
    fetch_publish_assets.mermaid(session.offline)
    check_isolation(session, python)
    make_folders(PUBLISH_FOLDERS)
    session.say("資料卡與講義環境已就緒。下一步：.\\run.ps1 publish-doctor")


def main(argv):
    command, options = argv[0], [a.lower() for a in argv[1:]]
    unknown = [a for a in options if a not in ("--offline", "-offline")]
    if unknown:
        print(f"用法：.\\run.ps1 {command} [--offline]（不認得：{' '.join(unknown)}）")
        return 2
    session = Session(command, offline=bool(options))
    session.say(f"{command}：全部安裝在專案的 .runtime 內。紀錄：{session.log_path.relative_to(ROOT).as_posix()}")
    try:
        (setup_transcribe if command == "setup" else setup_publish)(session)
        return 0
    except (SetupError, OSError, ValueError, SystemExit) as error:
        session.say(f"{command} 在這一步停下：{error}")
        session.say("已完成的部分都保留；排除原因後執行同一個命令即可接續。")
        return 1
    finally:
        session.close()
