import os
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

from .storage import THUMB_EXT

GRAY_MODES = {"I", "I;16", "I;16L", "I;16B"}


def list_previews(preview_dir):
    preview_dir = Path(preview_dir)
    if not preview_dir.is_dir():
        return []
    return sorted(path.name for path in preview_dir.iterdir() if path.suffix == ".jpg")


def make_thumbnail(source, target, width):
    with Image.open(source) as image:
        if image.mode in GRAY_MODES:
            image = image.point(lambda value: value * (1 / 256)).convert("L")
        image = image.convert("RGB")
        image.thumbnail((width, width))
        temporary = target.with_name(target.name + ".part")
        image.save(temporary, format="JPEG", quality=80)
    os.replace(temporary, target)


def sync_previews(renders_dir, preview_dir, width=480, settle=0.5):
    renders_dir = Path(renders_dir)
    preview_dir = Path(preview_dir)
    if Image is None or not renders_dir.is_dir():
        return 0
    preview_dir.mkdir(parents=True, exist_ok=True)
    made = 0
    now = time.time()
    for source in sorted(renders_dir.iterdir()):
        if not source.is_file() or source.suffix.lower() not in THUMB_EXT:
            continue
        target = preview_dir / (source.name + ".jpg")
        if target.exists():
            continue
        try:
            if now - source.stat().st_mtime < settle:
                continue
            make_thumbnail(source, target, width)
            made += 1
        except Exception:
            continue
    return made
