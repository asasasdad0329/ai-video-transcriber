"""Validate and render Mermaid with the locked mermaid.min.js in a system browser.

The model only writes Mermaid text; version, theme and fonts are fixed here so
diagrams look the same whichever agent authored them.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from workspace_paths import ROOT

LOCK = ROOT / "system/config/publish-lock.json"
FONT_STACK = '"Microsoft JhengHei","Noto Sans TC","PingFang TC",sans-serif'

_RENDER = """async ([id, src]) => {
  const box = document.getElementById('box');
  box.innerHTML = '';
  try {
    const out = await mermaid.render(id, src);
    box.innerHTML = out.svg;
    const svg = box.querySelector('svg');
    const r = svg.getBoundingClientRect();
    return {ok: true, width: r.width, height: r.height};
  } catch (e) {
    document.querySelectorAll('[id^="d' + id + '"]').forEach(n => n.remove());
    return {ok: false, error: String((e && e.message) || e)};
  }
}"""

_PARSE = """async (src) => {
  try { await mermaid.parse(src); return null; }
  catch (e) { return String((e && e.message) || e); }
}"""

def asset_path():
    spec = json.loads(LOCK.read_text(encoding="utf-8"))["assets"]["mermaid"]
    path = ROOT / spec["path"]
    if not path.is_file():
        raise RuntimeError("缺少 mermaid.min.js，先執行 .\\run.ps1 setup-publish")
    return path


def labels(src):
    """Visible node and edge labels, used to check a diagram stays on the source text."""
    found = []
    pattern = re.compile(r'\[\s*"([^"]*)"\s*\]|\(\s*"([^"]*)"\s*\)|\{\s*"([^"]*)"\s*\}'
                         r'|\[([^\]\["]+)\]|\{([^}{"]+)\}|\|([^|]+)\|')
    for line in src.splitlines():
        if line.strip().startswith("%%"):
            continue
        for match in pattern.finditer(line):
            value = next(v for v in match.groups() if v is not None)
            found.append(re.sub(r"<br\s*/?>", "", value).strip())
    return [v for v in found if v]


class Mermaid:
    """One browser session for all diagrams of a document."""

    def __init__(self, theme=None):
        self.theme = theme or {}
        self._pw = self._browser = self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        channels = json.loads(LOCK.read_text(encoding="utf-8")).get("browser_channels", ["msedge", "chrome"])
        self._pw = sync_playwright().start()
        errors = []
        for channel in channels:
            try:
                self._browser = self._pw.chromium.launch(channel=channel, headless=True)
                break
            except Exception as exc:  # try the next installed browser
                errors.append(f"{channel}: {exc}".splitlines()[0])
        if self._browser is None:
            self._pw.stop()
            raise RuntimeError("找不到可用的 Edge／Chrome：" + "；".join(errors))
        self._page = self._browser.new_page(device_scale_factor=2, viewport={"width": 1200, "height": 900})
        self._page.set_content('<html><body style="margin:0;background:#ffffff"><div id="box" '
                               'style="display:inline-block;padding:8px"></div></body></html>')
        self._page.add_script_tag(content=asset_path().read_text(encoding="utf-8"))
        config = {"startOnLoad": False, "securityLevel": "strict", "fontFamily": FONT_STACK,
                  "theme": self.theme.get("theme", "base"),
                  "themeVariables": {"fontFamily": FONT_STACK, **self.theme.get("themeVariables", {})},
                  "flowchart": {"useMaxWidth": False, "htmlLabels": True, **self.theme.get("flowchart", {})}}
        self._page.evaluate("(cfg) => mermaid.initialize(cfg)", config)
        self._count = 0
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def parse(self, src):
        return self._page.evaluate(_PARSE, src)

    def render(self, src, png_path):
        self._count += 1
        result = self._page.evaluate(_RENDER, [f"d{self._count}", src])
        if result.get("ok"):
            Path(png_path).parent.mkdir(parents=True, exist_ok=True)
            self._page.locator("#box").screenshot(path=str(png_path), omit_background=False)
        return result
