import re
from pathlib import Path

NAME_RE = re.compile(r"^blender-(\d+)\.(\d+)\.(\d+)")
SERIES_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


def label(version):
    return ".".join(str(part) for part in version)


def parse_series(text):
    match = SERIES_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"не похоже на версию Blender: {text}")
    return int(match.group(1)), int(match.group(2))


def discover(root):
    root = Path(root)
    found = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        match = NAME_RE.match(entry.name)
        executable = entry / "blender"
        if match and executable.is_file():
            version = tuple(int(part) for part in match.groups())
            found.append({"version": version, "path": str(executable)})
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
