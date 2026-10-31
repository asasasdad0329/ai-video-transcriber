"""Text comparison helpers shared by data cards and handouts."""
from __future__ import annotations

from functools import lru_cache
import re

from workspace_paths import ROOT

GLOSSARY = ROOT / "system/config/glossary.txt"
UNRESOLVED = "〔？〕"

# Organizer commentary the user has rejected in finished documents. A phrase that
# the speaker actually said is exempt, so the check runs against the source text.
META_PHRASES = (
    "整理者", "編者註", "編註", "本文件", "本資料", "本講義", "本手冊", "本卡",
    "未涵蓋", "未覆蓋", "不得推論", "不可推論", "免責", "僅供參考", "以上為", "以下為",
    "資料範圍", "處理原則", "整理原則", "轉錄原則", "無法確認", "不確定是否",
    "provenance", "interpreted", "verbatim",
)

_SPACE = re.compile(r"[\s　]+")
_NUMBER = re.compile(r"(?<![0-9])[0-9]+(?:[.:][0-9]+)?")
_ASCII_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+#.-]*[A-Za-z0-9+#]")
# Spoken fillers a written edition should drop.
_ASCII_FILLERS = {"ok", "okay", "yes", "yeah", "hi", "hello", "bye", "emm", "hmm", "uh", "um"}


def squash(text):
    """Compare text independent of Markdown line breaks and blockquote markers."""
    text = re.sub(r"(?m)^> ?", "", text or "")
    return _SPACE.sub("", text)


def contains(haystack, needle):
    needle = squash(needle)
    return bool(needle) and needle in squash(haystack)


def meta_hits(text, source=""):
    source = squash(source).lower()
    compact = squash(text).lower()
    return [p for p in META_PHRASES if p.lower() in compact and p.lower() not in source]


@lru_cache(maxsize=None)
def glossary_terms():
    if not GLOSSARY.is_file():
        return ()
    terms = []
    for line in GLOSSARY.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(line)
    return tuple(sorted(set(terms), key=len, reverse=True))


def protected_tokens(text, extra=()):
    """Facts a rewrite may not silently drop: numbers, Latin words, listed terms."""
    words = {w for w in _ASCII_WORD.findall(text) if w.lower() not in _ASCII_FILLERS}
    tokens = set(_NUMBER.findall(text)) | words
    tokens |= {t for t in glossary_terms() if t in text}
    tokens |= {t for t in extra if t in text}
    return tokens


def unresolved_clauses(text):
    """Return the clause around each 〔？〕 so the uncertain reading can be located."""
    # The marker itself contains a full-width question mark; hide it before splitting.
    hidden = ""
    clauses = []
    for part in re.split(r"(?<=[。！？；])", text.replace(UNRESOLVED, hidden)):
        if hidden in part:
            clauses.append(part.replace(hidden, UNRESOLVED).strip())
    return clauses
