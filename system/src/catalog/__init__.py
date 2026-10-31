"""Local work index: which source videos have a transcript, data card and handout.

The index keeps history. A removed file or an old version of an output stays in
the database with its status changed, so past work remains answerable.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from workspace_paths import ROOT
from catalog.db import INDEX_DIR, MARKDOWN_NAME, connect
from catalog.scan import display_name, scan
from catalog import view


def _open(root=ROOT, refresh=True):
    if refresh:
        scan(root)
    return connect(root)


def overview(root=ROOT, refresh=True):
    with _open(root, refresh) as connection:
        return view.overview(connection)


def search(query, root=ROOT, refresh=True, limit=20):
    with _open(root, refresh) as connection:
        return view.search(connection, query, limit)


def show(value, root=ROOT, refresh=True):
    with _open(root, refresh) as connection:
        return view.show(connection, value)


def find_run(value, root=ROOT, refresh=True):
    with _open(root, refresh) as connection:
        return view.find_run(connection, value, root)


def refresh_markdown(root=ROOT, refresh=True):
    index = overview(root, refresh)
    target = Path(root) / INDEX_DIR / MARKDOWN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(view.markdown(index), encoding="utf-8")
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.ps1 catalog",
                                     description="查每支影片的逐字稿、資料卡、講義狀態；保留已移除與舊版本紀錄")
    parser.add_argument("query", nargs="?", help="影片名稱或內容關鍵字（繁簡皆可）")
    parser.add_argument("--show", metavar="影片", help="列出單支影片的任務、成品版本與事件紀錄")
    parser.add_argument("--removed", action="store_true", help="只列已移除、不見或被取代的項目")
    parser.add_argument("--quiet", action="store_true", help="只更新工作索引.md，不輸出")
    args = parser.parse_args(argv)
    index = refresh_markdown()
    if args.quiet:
        return 0
    if args.show:
        result = show(args.show, refresh=False)
    elif args.removed:
        result = index["removed"]
    elif args.query:
        result = search(args.query, refresh=False)
    else:
        result = index
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
