"""Parse the handout source Markdown into a fixed set of elements.

Only these elements exist, so layout stays in the renderer rather than in the
model's hands: chapter (##), subheading (###), paragraph, bullet list,
numbered list, table, callout (>), screen image, Mermaid diagram.
"""
from __future__ import annotations

import re

import yaml

CHAPTER = re.compile(r"^##\s+(.+?)\s*\{src=([^}]*)\}\s*$")
IMAGE = re.compile(r"^!\[([^\]]*)\]\(card:([A-Za-z0-9_-]+)(?:@([A-Za-z0-9_-]+))?\)\s*$")
NUMBERED = re.compile(r"^\d+[.)]\s+")


class HandoutError(ValueError):
    pass


def split_front_matter(text):
    if not text.startswith("---"):
        raise HandoutError("講義原稿需以 front matter（--- 區塊）開頭")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise HandoutError("front matter 沒有結束的 ---")
    meta = yaml.safe_load(parts[0][3:]) or {}
    if not isinstance(meta, dict) or not isinstance(meta.get("title"), str) or not meta["title"].strip():
        raise HandoutError("front matter 需要 title")
    return meta, parts[1].lstrip("-").lstrip("\n")


def expand_src(spec, speech_order):
    index = {bid: i for i, bid in enumerate(speech_order)}
    ids = []
    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue
        if "-" in token:
            first, last = (t.strip() for t in token.split("-", 1))
            if first not in index or last not in index or index[first] > index[last]:
                raise HandoutError(f"src 範圍無效：{token}")
            ids.extend(speech_order[index[first]:index[last] + 1])
        else:
            if token not in index:
                raise HandoutError(f"src 指向不存在的口述段落：{token}")
            ids.append(token)
    if not ids:
        raise HandoutError("src 不可為空")
    return ids


def parse(text, speech_order):
    meta, body = split_front_matter(text.replace("\r\n", "\n"))
    chapters = []
    lines = body.split("\n")
    i = 0

    def current():
        if not chapters:
            raise HandoutError(f"第一個 ## 章節之前不可有內容：{lines[i][:30]}")
        return chapters[-1]["elements"]

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("# ") or stripped == "#":
            raise HandoutError("不要寫 # 標題；標題放 front matter 的 title")
        chapter = CHAPTER.match(stripped)
        if stripped.startswith("## "):
            if not chapter:
                raise HandoutError(f"章節標題需帶 {{src=...}}：{stripped}")
            chapters.append({"title": chapter.group(1), "src": expand_src(chapter.group(2), speech_order),
                             "elements": []})
            i += 1
            continue
        if stripped.startswith("### "):
            current().append({"type": "subheading", "text": stripped[4:].strip()})
            i += 1
            continue
        if stripped.startswith("#"):
            raise HandoutError(f"只允許 ## 章節與 ### 小節：{stripped}")
        if stripped.startswith("```"):
            info = stripped[3:].strip()
            if not info.startswith("mermaid"):
                raise HandoutError("程式碼區塊只允許 mermaid")
            caption = info[len("mermaid"):].strip()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            if i >= len(lines):
                raise HandoutError("mermaid 區塊沒有結束的 ```")
            current().append({"type": "mermaid", "code": "\n".join(code).strip(), "caption": caption})
            i += 1
            continue
        image = IMAGE.match(stripped)
        if image:
            current().append({"type": "image", "caption": image.group(1).strip(), "card": image.group(2),
                              "frame": image.group(3)})
            i += 1
            continue
        if stripped.startswith("!["):
            raise HandoutError(f"圖片只能引用畫面卡：![圖說](card:screen-001)：{stripped}")
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            if len(rows) < 2 or not all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1]):
                raise HandoutError("表格第二列需為分隔線 | --- |")
            header, data = rows[0], rows[2:]
            if any(len(r) != len(header) for r in data):
                raise HandoutError("表格每列欄數需一致")
            current().append({"type": "table", "header": header, "rows": data})
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            current().append({"type": "callout", "lines": [q for q in quote if q.strip()]})
            continue
        if stripped.startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(lines[i].strip()[2:].strip())
                i += 1
            current().append({"type": "bullets", "items": items})
            continue
        if NUMBERED.match(stripped):
            items = []
            while i < len(lines) and NUMBERED.match(lines[i].strip()):
                items.append(NUMBERED.sub("", lines[i].strip()).strip())
                i += 1
            current().append({"type": "numbered", "items": items})
            continue
        paragraph = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i].strip()):
            paragraph.append(lines[i].strip())
            i += 1
        current().append({"type": "paragraph", "text": "".join(paragraph)})
    if not chapters:
        raise HandoutError("講義至少需要一個 ## 章節")
    return meta, chapters


def _starts_block(line):
    return (line.startswith(("#", "```", "|", ">", "- ", "* ", "![")) or bool(NUMBERED.match(line)))


def element_text(element):
    kind = element["type"]
    if kind in ("paragraph", "subheading"):
        return element["text"]
    if kind in ("bullets", "numbered"):
        return "\n".join(element["items"])
    if kind == "callout":
        return "\n".join(element["lines"])
    if kind == "table":
        return "\n".join(" ".join(r) for r in [element["header"], *element["rows"]])
    if kind == "mermaid":
        return element["code"] + "\n" + element.get("caption", "")
    if kind == "image":
        return element.get("caption", "")
    return ""


def prose_text(elements):
    """Rewritten speech only; tables, diagrams and captions are extra material."""
    return "\n".join(element_text(e) for e in elements
                     if e["type"] in ("paragraph", "subheading", "bullets", "numbered", "callout"))
