import re
import subprocess
from pathlib import Path

NAME_RE = re.compile(r"^blender-(\d+)\.(\d+)\.(\d+)")
SERIES_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")
VERSION_RE = re.compile(r"Blender (\d+)\.(\d+)(?:\.(\d+))?")

_version_cache = {}


def label(version):
    return ".".join(str(part) for part in version)


def parse_series(text):
    match = SERIES_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"не похоже на версию Blender: {text}")
    return int(match.group(1)), int(match.group(2))


def _query_version(executable):
    key = (str(executable), executable.stat().st_mtime)
    if key in _version_cache:
        return _version_cache[key]
    version = None
    try:
        result = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True, errors="replace", timeout=30
        )
        match = VERSION_RE.search(result.stdout)
        if match:
            version = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    except (OSError, subprocess.SubprocessError):
        version = None
    _version_cache[key] = version
    return version


def _identify(folder):
    executable = folder / "blender"
    if not executable.is_file():
        return None
    match = NAME_RE.match(folder.name)
    version = tuple(int(part) for part in match.groups()) if match else _query_version(executable)
    if version is None:
        return None
    return {"version": version, "path": str(executable)}


def discover(root):
    root = Path(root)
    found = []
    if not root.is_dir():
        return found
    folders = [root] + sorted(
        path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    for folder in folders:
        item = _identify(folder)
        if item:
            found.append(item)
    found.sort(key=lambda item: item["version"])
    return found


def resolve(installed, project_series, requested=None):
    if not installed:
        raise LookupError("не найдено ни одной установленной версии Blender")
    warnings = []
    wanted = parse_series(requested) if requested else tuple(project_series)
    same = [item for item in installed if item["version"][:2] == wanted]
    if same:
        chosen = same[-1]
    else:
        newer = [item for item in installed if item["version"][:2] > wanted]
        if newer:
            nearest = min(item["version"][:2] for item in newer)
            chosen = [item for item in newer if item["version"][:2] == nearest][-1]
        else:
            chosen = installed[-1]
        warnings.append(
            f"Blender {wanted[0]}.{wanted[1]} не установлен, использован {label(chosen['version'])}"
        )
    if chosen["version"][:2] < tuple(project_series):
        warnings.append("проект сохранён в более новой версии Blender, чем используемая")
    return {"version": label(chosen["version"]), "path": chosen["path"]}, warnings
