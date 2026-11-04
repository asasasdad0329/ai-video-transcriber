"""Download pinned release assets with SHA-256 checks; never execute downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system/src"))
from workspace_paths import resolve

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
LOCK = ROOT / "system/config/bootstrap-lock.json"


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "local-video-workflow"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("Output must remain inside this project")
    return path


def _curl(url, destination):
    subprocess.run([str(SYSTEM32 / "curl.exe"), "--fail", "--location", "--silent", "--show-error",
                    "--ssl-revoke-best-effort", "--retry", "3", "--connect-timeout", "30",
                    "--output", str(destination), url], check=True, stdout=subprocess.DEVNULL)


def _powershell(url, destination):
    # URL and path travel in the environment, so no quoting rules apply. This route also
    # honours the system proxy settings, which curl.exe does not read.
    script = ("$ProgressPreference='SilentlyContinue'; "
              "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; "
              "Invoke-WebRequest -UseBasicParsing -Uri $env:VW_URL -OutFile $env:VW_OUT")
    subprocess.run([str(SYSTEM32 / "WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-NonInteractive",
                    "-Command", script], check=True, stdout=subprocess.DEVNULL,
                   env={**os.environ, "VW_URL": url, "VW_OUT": str(destination)})


def _urllib(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "local-video-workflow"})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=4 * 1024 * 1024)


def download(url, destination):
    """Save `url` to `destination`, trying Windows' own TLS stack first.

    Python's OpenSSL only sees a snapshot of the Windows certificate store and cannot fetch a
    missing root or intermediate on demand the way Windows does. The caller's pinned SHA-256
    is what proves the file, so any route that delivers the bytes is acceptable.
    """
    routes = [(name, route) for name, route, tool in (
        ("curl.exe", _curl, SYSTEM32 / "curl.exe"),
        ("Windows PowerShell", _powershell, SYSTEM32 / "WindowsPowerShell/v1.0/powershell.exe"),
        ("Python", _urllib, None)) if tool is None or tool.is_file()]
    failures = []
    for name, route in routes:
        try:
            route(url, destination)
            return
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            failures.append(f"{name}: {error}")
            destination.unlink(missing_ok=True)
            if len(failures) < len(routes):
                print(f"{name} could not download; trying the next method.", flush=True)
    raise OSError("Download failed by every method: " + url + "\n  " + "\n  ".join(failures))


def fetch(asset, offline=False):
    target = inside(resolve(asset["path"]))
    if target.is_file() and sha256(target) == asset["sha256"]:
        print("cached: " + target.relative_to(ROOT).as_posix(), flush=True)
        return target
    if offline:
        raise FileNotFoundError("Pinned asset is missing or changed: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    print("download: " + asset["path"], flush=True)
    download(asset["url"], temporary)
    if sha256(temporary) != asset["sha256"]:
        raise ValueError("SHA256 mismatch: " + asset["path"])
    temporary.replace(target)
    return target


def extract(archive, destination):
    destination = inside(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            target = (destination / info.filename).resolve()
            if not target.is_relative_to(destination):
                raise ValueError("Unsafe archive member")
        source.extractall(destination)


TOOL_KEYS = ("ffmpeg", "cublas")
# Archives unpacked whole: folder and the program that must be there. The folder records the
# SHA-256 of the archive it came from, so a cleared download cache costs nothing and a newly
# pinned version is unpacked again.
UNPACKED = {"uv": (".runtime/tools/uv", "uv.exe"), "whisper": (".runtime/tools/whisper", "Release/whisper-cli.exe")}
UNPACKED_FROM = "unpacked-from.sha256"


def install_members(key, offline=False):
    """Extract only the locked files of one tools-lock entry; a different copy is replaced."""
    spec = json.loads((ROOT / "system/config/tools-lock.json").read_text(encoding="utf-8"))[key]
    destination = inside(ROOT / spec["destination"])
    members = spec["members"]
    if all((destination / name).is_file() and sha256(destination / name) == item["sha256"]
           for name, item in members.items()):
        print(f"cached: {key} in " + destination.relative_to(ROOT).as_posix(), flush=True)
        return
    archive = fetch(spec, offline)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for name, item in members.items():
            target = destination / name
            temporary = target.with_suffix(target.suffix + ".partial")
            with source.open(item["member"]) as stream, temporary.open("wb") as output:
                shutil.copyfileobj(stream, output, length=4 * 1024 * 1024)
            if sha256(temporary) != item["sha256"]:
                temporary.unlink()
                raise ValueError("SHA256 mismatch: " + item["member"])
            temporary.replace(target)


def install(key, offline=False):
    """Verify or fetch one pinned asset and unpack it where the workflow expects it."""
    if key in TOOL_KEYS:
        install_members(key, offline)
        return
    asset = json.loads(LOCK.read_text(encoding="utf-8"))["assets"][key]
    if key not in UNPACKED:
        fetch(asset, offline)
        return
    folder, program = UNPACKED[key]
    destination = ROOT / folder
    record = destination / UNPACKED_FROM
    if (destination / program).is_file() and record.is_file() \
            and record.read_text(encoding="ascii").strip() == asset["sha256"]:
        print(f"cached: {key} in {folder}", flush=True)
        return
    extract(fetch(asset, offline), destination)
    record.write_text(asset["sha256"] + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["resolve", "uv", "whisper", "model", *TOOL_KEYS, "all"])
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    lock_path = LOCK
    if args.action == "resolve":
        if lock_path.exists():
            raise SystemExit("Lock exists; no automatic refresh")
        assets = {}
        for key, repo, tag, filename, path in [
            ("uv", "astral-sh/uv", "0.12.13", "uv-x86_64-pc-windows-msvc.zip", ".cache/downloads/uv-0.12.13.zip"),
            ("whisper", "ggml-org/whisper.cpp", "b4938", "whisper-cublas-11.8.0-bin-x64.zip", ".cache/downloads/whisper-b4938-cu118.zip"),
        ]:
            release = get_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            item = next(item for item in release["assets"] if item["name"] == filename)
            digest = item.get("digest") or ""
            if not digest.startswith("sha256:"):
                raise ValueError("Release has no published SHA256")
            assets[key] = {"url": item["browser_download_url"], "sha256": digest[7:], "path": path, "size": item["size"], "version": tag}
        repository = "ggerganov/whisper.cpp"
        model_info = get_json(f"https://huggingface.co/api/models/{repository}")
        revision = model_info["sha"]
        model_name = "ggml-large-v3-turbo-q5_0.bin"
        tree = get_json(f"https://huggingface.co/api/models/{repository}/tree/{revision}?recursive=false&expand=false")
        item = next(item for item in tree if item["path"] == model_name)
        assets["model"] = {"url": f"https://huggingface.co/{repository}/resolve/{revision}/{model_name}", "sha256": item["lfs"]["oid"], "path": f"models/whisper/{model_name}", "size": item["size"], "revision": revision}
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"python": "3.12.12", "assets": assets}, indent=2) + "\n", encoding="utf-8")
        print(lock_path.as_posix())
        return
    for key in (["uv", "whisper", "model", *TOOL_KEYS] if args.action == "all" else [args.action]):
        install(key, args.offline)


if __name__ == "__main__":
    main()
