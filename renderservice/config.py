import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_BASE = "https://download.blender.org/release/"


def _number(name, default):
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else float(default)


def _flag(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def load_env_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    blender_root: Path
    host: str = "0.0.0.0"
    port: int = 8000
    result_days: float = 30
    project_days: float = 3
    max_upload_bytes: int = 0
    xvfb: str = "auto"
    preflight_timeout: int = 900
    kill_grace: float = 10
    auto_install: bool = False
    download_base: str = DEFAULT_DOWNLOAD_BASE
    token: str = ""

    @property
    def jobs_dir(self):
        return self.data_dir / "jobs"

    @property
    def user_dir(self):
        return self.data_dir / "blender-user"

    @property
    def db_path(self):
        return self.data_dir / "render.db"

    @property
    def wrapper(self):
        return PACKAGE_DIR / "blender_wrapper.py"

    @property
    def static_dir(self):
        return PACKAGE_DIR / "static"


def load_settings():
    base = PACKAGE_DIR.parent
    load_env_file(base / ".env")
    return Settings(
        data_dir=Path(os.environ.get("RENDER_DATA") or base / "data").expanduser().resolve(),
        blender_root=Path(os.environ.get("RENDER_BLENDER_ROOT") or "~/Applications/blender").expanduser(),
        host=os.environ.get("RENDER_HOST", "0.0.0.0"),
        port=int(_number("RENDER_PORT", 8000)),
        result_days=_number("RENDER_RESULT_DAYS", 30),
        project_days=_number("RENDER_PROJECT_DAYS", 3),
        max_upload_bytes=int(_number("RENDER_MAX_UPLOAD_GB", 0) * 1024 ** 3),
        xvfb=os.environ.get("RENDER_XVFB", "auto").lower(),
        auto_install=_flag("RENDER_AUTO_INSTALL", True),
        download_base=os.environ.get("RENDER_DOWNLOAD_BASE") or DEFAULT_DOWNLOAD_BASE,
        token=os.environ.get("RENDER_TOKEN", "").strip(),
    )
