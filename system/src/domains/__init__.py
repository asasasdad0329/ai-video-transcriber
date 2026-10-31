"""Optional subject modules. The core workflows stay domain-neutral; a module only
adds tokens a rewrite must keep and validators for structured case fields."""
from __future__ import annotations

from importlib import import_module

KNOWN = ("bazi",)


class Plain:
    name = None

    def detects(self, text):
        return False

    def protected_tokens(self, text):
        return set()

    def handles(self, name):
        """True when this module, not a plain text match, validates the field."""
        return False

    def validate_case_field(self, name, value, source_text):
        """Return an error message, or None when the value is acceptable."""
        return None


def load(name):
    if not name:
        return Plain()
    if name not in KNOWN:
        raise ValueError(f"未知領域模組：{name}（可用：{', '.join(KNOWN)}）")
    return import_module(f"domains.{name}").Domain()
