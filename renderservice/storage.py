import os
import shutil
import zipfile
from collections import deque
from pathlib import Path

THUMB_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".tga"}
FRAME_EXT = THUMB_EXT | {".exr", ".hdr", ".dpx", ".jp2", ".cin"}


class JobPaths:
    def __init__(self, jobs_dir, job_id):
        self.root = Path(jobs_dir) / job_id
        self.project = self.root / "project"
        self.renders = self.root / "renders"
        self.preview = self.root / "preview"
        self.log = self.root / "render.log"
        self.progress = self.root / "progress.json"
        self.preflight = self.root / "preflight.json"
        self.preflight_log = self.root / "preflight.log"
        self.archive = self.root / "result.zip"
        self.upload = self.root / "upload.tmp"


def safe_name(name, default="project.blend"):
    base = os.path.basename((name or "").replace("\\", "/")).strip().lstrip(".")
    return base or default


def safe_extract(zip_path, target):
    target = Path(target).resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("__MACOSX/") or not name:
                continue
            destination = (target / name).resolve()
            if not destination.is_relative_to(target):
                raise ValueError(f"недопустимый путь в архиве: {info.filename}")
            if info.is_dir() or name.endswith("/"):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)


def find_blends(project_dir):
    found = [
        path.relative_to(project_dir).as_posix()
        for path in Path(project_dir).rglob("*.blend")
        if path.is_file()
    ]
    found.sort(key=lambda item: (item.count("/"), item.lower()))
    return found


def list_outputs(renders):
    renders = Path(renders)
    if not renders.is_dir():
        return []
    return sorted(path for path in renders.iterdir() if path.is_file())


def count_images(renders):
    renders = Path(renders)
    if not renders.is_dir():
        return 0
    return sum(1 for path in renders.iterdir() if path.is_file() and path.suffix.lower() in FRAME_EXT)


def build_archive(renders, archive):
    archive = Path(archive)
    temporary = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for path in list_outputs(renders):
            bundle.write(path, path.name)
    os.replace(temporary, archive)


def archive_is_fresh(renders, archive):
    archive = Path(archive)
    if not archive.is_file():
        return False
    newest = max((path.stat().st_mtime for path in list_outputs(renders)), default=0)
    return archive.stat().st_mtime >= newest


def tail_lines(path, count=10):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip() for line in deque(handle, maxlen=count)]
    except OSError:
        return []


def rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


def reset_outputs(paths):
    rmtree(paths.renders)
    rmtree(paths.preview)
    for path in (paths.archive, paths.progress, paths.log):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
