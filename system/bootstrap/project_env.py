"""Process environment that keeps every child tool inside this project."""
from pathlib import Path


def project_environment(root):
    """Variables to set (str) or remove (None) for child processes."""
    root = Path(root)
    runtime = root / ".runtime"
    return {
        "UV_PYTHON_INSTALL_DIR": str(runtime / "python"),
        "UV_PYTHON_BIN_DIR": str(runtime / "bin"),
        "UV_PYTHON_CACHE_DIR": str(runtime / "cache/python"),
        "UV_CACHE_DIR": str(runtime / "cache/uv"),
        "UV_PYTHON_INSTALL_BIN": "false",
        "UV_PYTHON_INSTALL_REGISTRY": "false",
        "UV_PYTHON_NO_REGISTRY": "true",
        "UV_PYTHON_DOWNLOADS": "manual",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONHOME": None,
        "PYTHONPATH": None,
        "HF_HOME": str(runtime / "cache/huggingface"),
        "HF_HUB_CACHE": str(runtime / "cache/huggingface/hub"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PIP_CACHE_DIR": str(runtime / "cache/pip"),
        "PIP_CONFIG_FILE": "NUL",
        "TEMP": str(runtime / "tmp"),
        "TMP": str(runtime / "tmp"),
    }


def apply(root, environ):
    """Apply to `environ` (normally os.environ) so every child inherits it."""
    values = project_environment(root)
    Path(values["TEMP"]).mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        if value is None:
            environ.pop(key, None)
        else:
            environ[key] = value
