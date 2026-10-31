"""Download or verify locked assets for the data-card and handout workflows."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_assets import download

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "system/config/publish-lock.json"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def mermaid(offline=False):
    spec = json.loads(LOCK.read_text(encoding="utf-8"))["assets"]["mermaid"]
    target = ROOT / spec["path"]
    if target.is_file() and _sha256(target.read_bytes()) == spec["sha256"]:
        return target
    if offline:
        raise SystemExit(f"{spec['path']} is missing or altered; offline setup cannot download it.")
    cache = ROOT / ".runtime/cache/downloads" / Path(spec["url"]).name
    if cache.is_file():
        archive = cache.read_bytes()
    else:
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_suffix(cache.suffix + ".partial")
        download(spec["url"], partial)
        archive = partial.read_bytes()
        partial.unlink()
    algorithm, expected = spec["integrity"].split("-", 1)
    if base64.b64encode(hashlib.new(algorithm, archive).digest()).decode() != expected:
        raise SystemExit("mermaid tarball integrity mismatch")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(archive)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        data = bundle.extractfile(spec["member"]).read()
    if _sha256(data) != spec["sha256"]:
        raise SystemExit("mermaid.min.js checksum mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", choices=["mermaid"])
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    print(mermaid(args.offline).relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
