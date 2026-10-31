"""Portable reading edition: speech paragraphs and self-contained visual text."""
from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
import re
from opencc import OpenCC
from workspace_paths import ROOT

_CC = OpenCC("s2tw")
TAIWAN_TERMS = ROOT / "system/config/taiwan_terms.txt"
_PUNCTUATION = set("，。？！；：、…—,.!?;:")
# New runs title the reading edition after the video; older layouts kept this phrase.
DEFAULT_TITLE = "逐字稿與畫面"


@lru_cache(maxsize=None)
def _taiwan_terms():
    # s2twp's phrase table also rewrites ordinary speech (通過→透過, 局部→區域性),
    # so only listed context-free words change; identity lines shield longer words.
    pairs = {}
    for number, line in enumerate(TAIWAN_TERMS.read_text(encoding="utf-8-sig").splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) != 2:
            raise ValueError(f"{TAIWAN_TERMS.name}:{number}: expected '原詞 台灣用詞'")
        pairs[fields[0]] = fields[1]
    pattern = re.compile("|".join(map(re.escape, sorted(pairs, key=len, reverse=True)))) if pairs else None
    return pattern, pairs


_LITERAL = re.compile(r"```.*?```|`[^`\n]+`|[a-z][a-z0-9+.-]*://[^\s<>\"，。；、）」』]+"
                      r"|(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\)[^\s<>|\"*?，。；：、「」『』（）]+", re.S | re.I)


def _source_positions(source, converted):
    """The source character behind each converted one, or None where unknown."""
    # Every s2tw phrase and character entry keeps its length, so positions line up.
    if len(converted) == len(source):
        return source
    # Should a converter dictionary ever change a length, align around it with
    # 台/臺 as anchors; a stretch that cannot be paired keeps the converter's output.
    origin = [None] * len(converted)
    folded = SequenceMatcher(None, source.replace("臺", "台"), converted.replace("臺", "台"), autojunk=False)
    for tag, i1, i2, j1, j2 in folded.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            origin[j1:j2] = source[i1:i2]
    return origin


def _taiwan_script(text):
    """s2tw and the Taiwan word list, keeping 台 as the default spelling.

    s2tw writes 臺 for every plain 台 (台灣→臺灣); only that 臺 goes back to 台.
    A 臺 already in the source stays, as do 颱風 and 檯燈. Word-list entries
    still match the s2tw output, and a listed replacement is written as listed.
    """
    converted = _CC.convert(text)
    origin = _source_positions(text, converted)

    def keep_tai(start, end):
        return "".join("台" if char == "臺" and origin[index] == "台" else char
                       for index, char in enumerate(converted[start:end], start))

    pattern, pairs = _taiwan_terms()
    pieces, last = [], 0
    for match in pattern.finditer(converted) if pattern else ():
        word = pairs[match.group()]
        # Identity lines only shield longer words, so their characters follow the source.
        pieces += [keep_tai(last, match.start()),
                   keep_tai(*match.span()) if word == match.group() else word]
        last = match.end()
    pieces.append(keep_tai(last, len(converted)))
    return "".join(pieces)


_WIDE = {",": "，", "?": "？", "!": "！"}


def _cjk(char):
    code = ord(char)
    return (0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE4F or 0xFF00 <= code <= 0xFFEF or 0x20000 <= code <= 0x3FFFF)


def _widen_punctuation(text, held):
    """，？！ next to Chinese; Latin and numbers keep ,?! (First, introduce; 1,000)."""
    def chinese(index, step):
        # The nearest character past spaces and the ,?! run decides.
        while 0 <= index < len(text) and text[index] in " \t,?!":
            index += step
        if not 0 <= index < len(text):
            return False
        char = text[index]
        if char in held:  # a held literal or term counts by its own outer character
            char = held[char][-1 if step < 0 else 0]
        return _cjk(char)

    return "".join(_WIDE[char] if char in _WIDE and (chinese(index - 1, -1) or chinese(index + 1, 1))
                   else char for index, char in enumerate(text))


def _widen_join(left, right):
    """A segment's edge ,?! meets its neighbour only in the paragraph ("OK," + "我們")."""
    run = len(left) - len(left.rstrip(",?!"))
    literal = any(match.end() == len(left) for match in _LITERAL.finditer(left))  # e.g. a URL kept its comma
    if run and right[:1] and _cjk(right[0]) and not literal:
        left = left[:-run] + "".join(_WIDE[char] for char in left[-run:])
    run = len(right) - len(right.lstrip(",?!"))
    if run and left[-1:] and _cjk(left[-1]):
        right = "".join(_WIDE[char] for char in right[:run]) + right[run:]
    return left, right


def traditional(text, protected_terms=(), punctuation=True):
    # Names and single-cell symbols can be ambiguous to a script converter.
    # Explicit terms (e.g. the Earthly Branch 丑) retain their exact spelling, as do
    # literals whose characters identify something (code, URLs, paths).
    if any(0xF0000 <= ord(char) <= 0xFFFFD for char in text):
        raise ValueError("Text contains reserved normalization marker")
    placeholders = {}

    def hold(value):
        marker = chr(0xF0000 + len(placeholders))
        placeholders[marker] = value
        return marker

    text = _LITERAL.sub(lambda match: hold(match.group()), text)
    for term in sorted({t for t in protected_terms if t}, key=len, reverse=True):
        if term in text:
            text = text.replace(term, hold(term))
    text = _taiwan_script(text)
    if punctuation:
        text = _widen_punctuation(text, placeholders)
    for marker, term in placeholders.items():
        text = text.replace(marker, term)
    return text


def _drop_capture_notes(structure):
    """Only strip explicit notes or a leading HH:MM:SS label, never matching frame times."""
    capture = re.compile(r"^( {0,3})[0-9]{2}:[0-5][0-9]:[0-5][0-9](?:[ \t]+|$)")
    lines = []
    fence = None
    for line in structure.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            lines.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            lines.append(line)
            continue
        if not line.startswith(("    ", "\t")):
            if line.lstrip().startswith(("擷取註記：", "擷取註記:", "撷取注记：", "撷取注记:")):
                continue
            if "|" not in line:
                line = capture.sub(r"\1", line, count=1)
        lines.append(line)
    return "\n".join(lines).strip("\r\n")


def visual_body(card, include_structure=True, protected_terms=()):
    """OCR subtitle dumps and capture instructions belong to the evidence files."""
    original = traditional(card.get("verbatim_text", ""), protected_terms).strip()
    structure = card.get("structure", "").split("\n\n同幀字幕", 1)[0].strip("\r\n")
    if not include_structure:
        structure = ""
    structure = re.sub(r"^畫面由左至右[^\n]*\n*", "", structure)
    structure = _drop_capture_notes(structure)
    parts = []
    if original:
        # Keep slide line breaks in Markdown without depending on a screenshot.
        parts.append("  \n".join(original.splitlines()))
    if structure:
        parts.append(traditional(structure, protected_terms))
    if card.get("uncertainties"):
        parts.append("畫面註：" + "；".join(traditional(x, protected_terms) for x in card["uncertainties"]))
    return "\n\n".join(parts)


# A find fragment lacks the neighbours that set the width of its edge ,?! in the
# card, and corrections stored before this rule (and before 台 stayed 台) were
# written against text with every ,?! widened and 臺 for 台.
_WIDTH_AND_TAI = str.maketrans(",?!台", "，？！臺")


def replace_card_text(body, rule, card_id, protected_terms=(), recorded=False):
    """Apply one reviewed correction to a visual card's display text.

    find is located in the normalized display text; replace is written exactly as
    given, so normalization cannot undo the fix. Every rule must match once; a
    new rule must also change the text, while a recorded one may already be met.
    """
    if (not isinstance(rule, dict) or set(rule) != {"find", "replace"}
            or any(not isinstance(rule[key], str) or not rule[key].strip()
                   for key in ("find", "replace"))):
        raise ValueError("invalid visual text replacement")
    old, new = traditional(rule["find"], protected_terms), rule["replace"]
    haystack, needle = body, old
    if body.count(old) != 1:
        # The fold is one character for one, so positions carry over to body.
        haystack, needle = body.translate(_WIDTH_AND_TAI), old.translate(_WIDTH_AND_TAI)
    if haystack.count(needle) != 1:
        raise ValueError("visual text replacement must match exactly once: " + card_id)
    start = haystack.index(needle)
    changed = body[:start] + new + body[start + len(old):]
    if changed == body and not recorded:
        raise ValueError(f"visual text replacement leaves card {card_id} unchanged; "
                         f"the display text already reads {new!r}")
    return changed


def card_text(card, block, protected_terms=()):
    """A visual block's display text with its recorded literal corrections."""
    body = visual_body(card, block.get("include_structure", True), protected_terms)
    # Reviewed literal corrections remain in the layout/audit; the
    # original visual evidence and sealed read receipt stay intact.
    replacements = block.get("text_replacements", [])
    if not isinstance(replacements, list):
        raise ValueError("visual text replacements must be a list")
    for rule in replacements:
        body = replace_card_text(body, rule, card["id"], protected_terms, recorded=True)
    return body


def default_layout(segments, cards, title=DEFAULT_TITLE):
    """Readable fallback; the agent can supply semantic paragraph boundaries."""
    ordered = sorted(cards, key=lambda c: (c["start"], c["id"]))
    sections, block, size = [{"title": "開場", "blocks": []}], [], 0
    index = 0

    def flush():
        nonlocal block, size
        if block:
            endings = {s["id"]: "，" for s in block}
            endings[block[-1]["id"]] = "。"
            sections[-1]["blocks"].append({"segment_ids": [s["id"] for s in block], "punctuation": endings})
            block, size = [], 0

    for segment in segments:
        while index < len(ordered) and ordered[index]["start"] <= segment["start"]:
            flush()
            card = ordered[index]
            sections.append({"title": card["title"], "blocks": [{"card_id": card["id"]}]})
            index += 1
        block.append(segment)
        size += len(segment["text"])
        if size >= 140 or segment["text"].endswith(("。", "？", "！", ".", "?", "!")):
            flush()
    flush()
    while index < len(ordered):
        card = ordered[index]
        sections.append({"title": card["title"], "blocks": [{"card_id": card["id"]}]})
        index += 1
    return {"title": title, "sections": [s for s in sections if s["blocks"]]}


def render(transcript, cards, decisions, layout=None):
    segments = transcript["segments"]
    by_id, by_card = {s["id"]: s for s in segments}, {c["id"]: c for c in cards}
    layout = layout if layout is not None else default_layout(segments, cards)
    protected_terms = layout.get("protected_terms", [])
    if not isinstance(protected_terms, list) or any(not isinstance(t, str) or not t for t in protected_terms):
        raise ValueError("reading_layout.protected_terms must be nonempty strings")
    convert = lambda text: traditional(text, protected_terms)
    speech_ids, visual_ids, records = [], [], []
    lines = ["# " + convert(layout["title"]), "",
             "正文保留口述內容；辨識錯字依上下文與畫面校正。〔？〕表示尚待核對的辨識；畫面補充另列。", ""]
    for section in layout["sections"]:
        lines += ["## " + convert(section["title"]), ""]
        for block in section["blocks"]:
            if "card_id" in block:
                cid = block["card_id"]
                if cid not in by_card:
                    raise ValueError("reading_layout: unknown card ID")
                visual_ids.append(cid)
                include_structure = block.get("include_structure", True)
                body = card_text(by_card[cid], block, protected_terms)
                lines += ["> **畫面補充：" + convert(by_card[cid]["title"]) + "**", ">"]
                lines += ["> " + line if line else ">" for line in body.splitlines()]
                lines.append("")
                records.append({"kind": "visual", "card_id": cid, "text": body, "include_structure": include_structure})
            else:
                ids = block.get("segment_ids")
                punctuation = block.get("punctuation", {})
                if not isinstance(ids, list) or not ids or any(i not in by_id for i in ids):
                    raise ValueError("reading_layout: unknown or empty speech range")
                if set(punctuation) - set(ids):
                    raise ValueError("reading_layout: punctuation outside paragraph")
                if any(not isinstance(p, str) or not p or not set(p) <= _PUNCTUATION for p in punctuation.values()):
                    raise ValueError("reading_layout: only punctuation may be added")
                speech_ids.extend(ids)
                pieces = []
                for sid in ids:
                    text = convert(by_id[sid]["text"]).strip()
                    if decisions.get(sid, {}).get("decision") == "needs_review":
                        text += "〔？〕"
                    ending = punctuation.get(sid, "")
                    if ending and not text.endswith(tuple(_PUNCTUATION)):
                        text += ending
                    if pieces:
                        pieces[-1], text = _widen_join(pieces[-1], text)
                    # Preserve word separation for mixed-language speech.
                    if pieces and pieces[-1][-1:].isascii() and pieces[-1][-1:].isalnum() and text[:1].isascii() and text[:1].isalnum():
                        pieces.append(" ")
                    pieces.append(text)
                paragraph = "".join(pieces)
                lines += [paragraph, ""]
                records.append({"kind": "speech", "segment_ids": ids, "text": paragraph})
    if speech_ids != [s["id"] for s in segments]:
        raise ValueError("reading_layout must cover all speech exactly once in original order")
    if len(visual_ids) != len(set(visual_ids)) or set(visual_ids) != set(by_card):
        raise ValueError("reading_layout must cover every visual card exactly once")
    return "\n".join(lines).rstrip() + "\n", records
