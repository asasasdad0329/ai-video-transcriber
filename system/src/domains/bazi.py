"""BaZi charts: sexagenary pillars must be valid and actually present in the source."""
from __future__ import annotations

import re

from domains import Plain

STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
_PILLAR = re.compile(f"[{STEMS}][{BRANCHES}]")
# A stem and branch pair only when both are yang or both yin (60 combinations).
VALID = {s + b for i, s in enumerate(STEMS) for j, b in enumerate(BRANCHES) if i % 2 == j % 2}
CHART_FIELDS = {"四柱", "原局", "命盤", "大運", "流年"}


def _cells(source_text):
    """Table cells in screen transcriptions, e.g. | 上排 | 丙 | 辛 |."""
    rows = [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in source_text.splitlines() if line.strip().startswith("|")]
    return rows


def _column_pillars(source_text):
    """Pillars written as a stem row above a branch row in a screen table."""
    found = set()
    rows = _cells(source_text)
    for upper, lower in zip(rows, rows[1:]):
        for s, b in zip(upper, lower):
            if len(s) == 1 and len(b) == 1 and s in STEMS and b in BRANCHES:
                found.add(s + b)
    return found


class Domain(Plain):
    name = "bazi"

    def detects(self, text):
        return any(field in text for field in CHART_FIELDS) or len(_PILLAR.findall(text)) >= 2

    def protected_tokens(self, text):
        tokens = {p for p in _PILLAR.findall(text) if p in VALID}
        # Chart text often places stems and branches in separate table cells. In
        # BaZi scope, keep every visible branch symbol exact so 丑 never becomes
        # the ordinary adjective 醜 during the final Traditional conversion.
        if any(field in text for field in CHART_FIELDS):
            tokens.update(branch for branch in BRANCHES if branch in text)
        return tokens

    def handles(self, name):
        return name in CHART_FIELDS

    def validate_case_field(self, name, value, source_text):
        if name not in CHART_FIELDS:
            return None
        pillars = _PILLAR.findall(value)
        if not pillars:
            return f"{name}：找不到干支"
        invalid = [p for p in pillars if p not in VALID]
        if invalid:
            return f"{name}：不存在的干支 {'、'.join(invalid)}"
        available = set(_PILLAR.findall(source_text)) | _column_pillars(source_text)
        missing = [p for p in pillars if p not in available]
        if missing:
            return f"{name}：來源找不到 {'、'.join(missing)}"
        return None
