"""Project locations; legacy aliases preserve original evidence hashes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = Path("工作資料/任務/轉錄")
OUTPUTS = Path("工作資料/輸出")
RECORDS = Path("工作資料/歸檔/開發紀錄")
DATACARD_TASKS = Path("工作資料/任務/資料卡")
HANDOUT_TASKS = Path("工作資料/任務/講義")
EXAMPLES = Path("工作資料/參考樣本/標準範例")
ENV = Path(".runtime/envs/transcribe")
PUBLISH_ENV = Path(".runtime/envs/publish")


def resolve(value, root=ROOT):
    """Resolve old project paths; external absolute paths are left alone."""
    root = Path(root).resolve()
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        return path
    parts = path.relative_to(root).parts
    if not parts:
        return root
    if parts[0] == "逐字稿輸出" and len(parts) >= 3:
        return root / RUNS / parts[1] / "exports/transcripts" / Path(*parts[2:])
    if parts[0] == "畫面轉錄輸出" and len(parts) >= 2:
        return root / RUNS / parts[1] / "exports/visuals" / Path(*parts[2:])
    aliases = {
        "runs": RUNS, "原檔": Path("工作資料/原檔"),
        "逐字稿輸出": OUTPUTS / "逐字稿", "講義輸出": OUTPUTS / "講義",
        "AI筆記與資料卡輸出": OUTPUTS / "資料卡", "reports": RECORDS,
        ".venv": ENV, ".tools": Path(".runtime/tools"),
        "models": Path(".runtime/models"), ".cache": Path(".runtime/cache"),
        ".tmp": Path(".runtime/tmp"), "config": Path("system/config"),
        "requirements.lock.txt": Path("system/config/requirements.lock.txt"),
        "requirements.in": Path("system/config/requirements.in"),
        "src": Path("system/src"), "skills": Path("system/skills"),
        "docs": Path("system/docs"), "scripts": Path("system/bootstrap"),
    }
    prefix = aliases.get(parts[0])
    return root / prefix / Path(*parts[1:]) if prefix is not None else path
