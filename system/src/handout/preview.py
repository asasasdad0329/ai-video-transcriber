"""Low-resolution page previews through an installed Word, for a quick visual check.

Word is only read and driven through COM; nothing is installed or configured.
The preview approximates print layout; Google Docs on a phone reflows text.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

_EXPORT = (
    "$ErrorActionPreference='Stop';"
    "$w=New-Object -ComObject Word.Application;$w.Visible=$false;$w.DisplayAlerts=0;"
    "try{$d=$w.Documents.Open($env:HANDOUT_DOCX,$false,$true,$false);"
    "$d.ExportAsFixedFormat($env:HANDOUT_PDF,17);$d.Close(0)}finally{$w.Quit()}"
)


def pages(docx_path, out_dir, scale=1.0):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page-*.png"):
        old.unlink()
    pdf = out_dir / "preview.pdf"
    env = {**os.environ, "HANDOUT_DOCX": str(Path(docx_path).resolve()), "HANDOUT_PDF": str(pdf.resolve())}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _EXPORT],
                            env=env, capture_output=True, text=True, timeout=240)
    if result.returncode != 0 or not pdf.is_file():
        raise RuntimeError("Word 轉 PDF 失敗：" + (result.stderr or result.stdout).strip()[:300])
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(pdf))
    written = []
    try:
        for number, page in enumerate(document, 1):
            image = page.render(scale=scale).to_pil()
            path = out_dir / f"page-{number:02d}.png"
            image.save(path)
            written.append(path)
    finally:
        document.close()
    return written
