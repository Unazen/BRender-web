import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import blendver, runner, storage

BLENDER_ORG = "blender_org"
USER_REPO = "user_default"
EXT_PREFIX = "bl_ext."
EXT_MIN = (4, 2)
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SERIES_IN_TEXT = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)")
SERIES_DIR = re.compile(r"^(\d{1,2})\.(\d{1,2})$")
BLEND_MAGICS = (b"BLENDER", b"\x1f\x8b", b"\x28\xb5\x2f\xfd")
SCAN_TIMEOUT = 180
INSTALL_TIMEOUT = 600
ORIGIN_ORDER = {"missing": 0, "user": 1, "extension": 2, "bundled": 3}
NEED_SERIES = "Не удалось определить версию Blender для этих настроек. Выберите версию в списке рядом с кнопкой и загрузите ещё раз"


class SetupError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def key_of(version):
    parts = str(version).split(".")
    return f"{int(parts[0])}.{int(parts[1])}"


def tuple_of(key):
    major, minor = key.split(".")[:2]
    return int(major), int(minor)


def series_from_text(text):
    match = SERIES_IN_TEXT.search(text or "")
    return f"{int(match.group(1))}.{int(match.group(2))}" if match else None


def series_from_header(path):
    try:
        major, minor = blendver.read_version(path)
        return f"{major}.{minor}"
    except Exception:
        return None


def looks_like_blend(path):
    with open(path, "rb") as handle:
        head = handle.read(8)
    return head.startswith(BLEND_MAGICS)


def clean_copy(source, target):
    target = Path(target)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(source).is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        shutil.copy2(source, target)


def tail_text(text, lines=6):
    parts = [line for line in text.strip().splitlines() if line.strip()]
    return " | ".join(parts[-lines:]) or "без пояснений"


def blank_entry():
    return {"prefs": None, "addons": {}, "ignored": [], "blender": None, "scanned": None}


class UserEnv:
    def __init__(self, settings, installed_fn):
        self.settings = settings
        self.installed_fn = installed_fn
        self.root = settings.user_dir
        self.state_path = self.root / "state.json"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="addons")
        self.pending = set()
        self.errors = {}
        self.busy = 0
        try:
            self.migrate_legacy()
            for key in self.installed_keys():
                self.ensure_dirs(key)
        except OSError:
            pass

    def paths(self, key):
        base = self.root / key
        return {
            "config": base / "config",
            "scripts": base / "scripts",
            "addons": base / "scripts" / "addons",
            "extensions": base / "extensions",
        }

    def ensure_dirs(self, key):
        paths = self.paths(key)
        for name in ("config", "addons", "extensions"):
            paths[name].mkdir(parents=True, exist_ok=True)
        return paths

    def env(self, version):
        paths = self.ensure_dirs(key_of(version))
        env = dict(os.environ)
        env["BLENDER_USER_CONFIG"] = str(paths["config"])
        env["BLENDER_USER_SCRIPTS"] = str(paths["scripts"])
        env["BLENDER_USER_EXTENSIONS"] = str(paths["extensions"])
        return env

    def load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("configured", False)
        state.setdefault("skipped", False)
        state.setdefault("series", {})
        return state

    def save(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name("state.json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def entry(self, state, key):
        item = state["series"].setdefault(key, blank_entry())
        for name, value in blank_entry().items():
            item.setdefault(name, value)
        return item

    def migrate_legacy(self):
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        legacy_dirs = [name for name in ("config", "scripts", "extensions") if (self.root / name).is_dir()]
        if "series" in raw or not (legacy_dirs or "addons" in raw):
            return
        key = None
        try:
            key = key_of(raw.get("blender"))
        except (ValueError, IndexError):
            pass
        if key is None:
            installed = self.installed_fn()
            if installed:
                version = installed[-1]["version"]
                key = f"{version[0]}.{version[1]}"
        if key is None:
            return
        target = self.root / key
        target.mkdir(parents=True, exist_ok=True)
        for name in legacy_dirs:
            if not (target / name).exists():
                shutil.move(str(self.root / name), str(target / name))
        entry = blank_entry()
        entry.update(
            {
                "prefs": raw.get("prefs"),
                "addons": raw.get("addons", {}),
                "ignored": raw.get("ignored", []),
                "blender": raw.get("blender"),
                "scanned": raw.get("scanned"),
            }
        )
        self.save(
            {
                "configured": bool(raw.get("configured")),
                "skipped": bool(raw.get("skipped")),
                "series": {key: entry},
            }
        )

    def installed_keys(self):
        keys = {f"{item['version'][0]}.{item['version'][1]}" for item in self.installed_fn()}
        return sorted(keys, key=tuple_of)

    def tool_blender(self, key, extensions=False):
        if extensions and tuple_of(key) < EXT_MIN:
            raise SetupError(409, "Расширения поддерживаются с Blender 4.2, для версии " + key + " их нет")
        items = [item for item in self.installed_fn() if item["version"][:2] == tuple_of(key)]
        if not items:
            raise SetupError(409, f"Blender {key} не установлен. Скачайте его в разделе «Версии Blender»")
        return items[-1]["path"]

    def scan(self, key):
        blender = self.tool_blender(key)
        self.ensure_dirs(key)
        target = self.root / key / "scan.json"
        target.unlink(missing_ok=True)
        command = runner.build_tool_command(blender, self.settings.wrapper, ["--scan", str(target)])
        self.busy += 1
        try:
            result = subprocess.run(
                command,
                env=self.env(key),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=SCAN_TIMEOUT,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            raise SetupError(504, "Blender не ответил при чтении настроек")
        except OSError as exc:
            raise SetupError(500, f"Не удалось запустить Blender: {exc}")
        finally:
            self.busy -= 1
        try:
            report = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SetupError(500, "Не удалось прочитать список аддонов: " + tail_text(result.stdout + result.stderr))
        self.merge(key, report)

    def is_user_path(self, path):
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(self.root.resolve())
        except OSError:
            return False

    def merge(self, key, report):
        with self.lock:
            state = self.load()
            entry = self.entry(state, key)
            known = entry["addons"]
            ignored = set(entry["ignored"])
            from_prefs = set(report.get("enabled", []))
            available = {item["module"]: item for item in report.get("modules", [])}
            candidates = set(from_prefs)
            for module, item in available.items():
                if self.is_user_path(item.get("path")):
                    candidates.add(module)
            addons = {}
            for module in sorted(candidates - ignored):
                found = available.get(module)
                if found is None:
                    origin = "missing"
                elif self.is_user_path(found.get("path")):
                    origin = "extension" if module.startswith(EXT_PREFIX) else "user"
                else:
                    origin = "bundled"
                was = known.get(module)
                enabled = was["enabled"] if was else (module in from_prefs or origin in ("user", "extension"))
                addons[module] = {
                    "name": (found or {}).get("name") or (was or {}).get("name") or module,
                    "version": (found or {}).get("version") or "",
                    "origin": origin,
                    "enabled": enabled,
                }
            entry["addons"] = addons
            entry["blender"] = report.get("blender")
            entry["scanned"] = time.time()
            self.save(state)

    def series_public(self, key, entry):
        items = []
        for module, item in entry["addons"].items():
            status, message = "ok", None
            if item["origin"] == "missing":
                if (key, module) in self.pending:
                    status = "installing"
                elif (key, module) in self.errors:
                    status, message = "failed", self.errors[(key, module)]
                else:
                    status = "missing"
            items.append({"module": module, "status": status, "message": message, **item})
        items.sort(key=lambda row: (ORIGIN_ORDER[row["origin"]], row["name"].lower()))
        versions = [item["version"] for item in self.installed_fn() if item["version"][:2] == tuple_of(key)]
        file = self.paths(key)["config"] / "userpref.blend"
        prefs = None
        if entry["prefs"] or file.is_file():
            prefs = dict(entry["prefs"] or {"name": "userpref.blend", "uploaded": None})
            prefs["exists"] = file.is_file()
            prefs["size"] = file.stat().st_size if prefs["exists"] else 0
            prefs["path"] = str(file)
        return {
            "series": key,
            "folder": str(self.root / key),
            "installed": bool(versions),
            "installed_version": ".".join(str(part) for part in versions[-1]) if versions else None,
            "extensions_supported": tuple_of(key) >= EXT_MIN,
            "prefs": prefs,
            "blender": entry["blender"],
            "scanned": entry["scanned"],
            "addons": items,
        }

    def public(self):
        state = self.load()
        for key in self.installed_keys():
            try:
                self.ensure_dirs(key)
            except OSError:
                pass
        keys = set(self.installed_keys()) | set(state["series"])
        series = []
        for key in sorted(keys, key=tuple_of, reverse=True):
            series.append(self.series_public(key, self.entry(state, key)))
        return {
            "configured": state["configured"],
            "skipped": state["skipped"],
            "needs_setup": not state["configured"] and not state["skipped"],
            "busy": bool(self.pending) or self.busy > 0,
            "has_blender": bool(self.installed_fn()),
            "installed_series": self.installed_keys(),
            "root": str(self.root),
            "series": series,
        }

    def refresh(self, key):
        self.scan(key)
        self.queue_missing(key)
        return self.public()

    def queue_missing(self, key):
        if tuple_of(key) < EXT_MIN:
            return
        entry = self.entry(self.load(), key)
        for module, item in entry["addons"].items():
            token = (key, module)
            if item["origin"] != "missing" or token in self.pending:
                continue
            parts = module.split(".")
            if len(parts) == 3 and parts[0] + "." == EXT_PREFIX and parts[1] == BLENDER_ORG:
                self.errors.pop(token, None)
                self.pending.add(token)
                self.pool.submit(self.install_extension, key, module, parts[2])

    def run_cli(self, blender, env, args):
        try:
            result = subprocess.run(
                [blender, "--command", "extension", *args],
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=INSTALL_TIMEOUT,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            raise SetupError(504, "Скачивание расширения заняло слишком много времени")
        except OSError as exc:
            raise SetupError(500, f"Не удалось запустить Blender: {exc}")
        if result.returncode != 0:
            raise SetupError(502, tail_text(result.stdout + result.stderr))

    def install_extension(self, key, module, ext_id):
        token = (key, module)
        try:
            blender = self.tool_blender(key, extensions=True)
            env = self.env(key)
            self.run_cli(blender, env, ["sync"])
            try:
                self.run_cli(blender, env, ["install", "--no-prefs", ext_id])
            except SetupError:
                self.run_cli(blender, env, ["install", "--no-prefs", "-r", BLENDER_ORG, ext_id])
            self.scan(key)
        except SetupError as exc:
            self.errors[token] = exc.message
        except Exception as exc:
            self.errors[token] = str(exc)
        finally:
            self.pending.discard(token)

    def unpack(self, stream, filename, workdir):
        raw = workdir / "upload"
        with open(raw, "wb") as out:
            shutil.copyfileobj(stream, out, 1024 * 1024)
        if not filename.lower().endswith(".zip"):
            return raw, None
        tree = workdir / "tree"
        tree.mkdir()
        try:
            storage.safe_extract(raw, tree)
        except zipfile.BadZipFile:
            raise SetupError(400, "Архив повреждён или это не zip")
        except ValueError as exc:
            raise SetupError(400, str(exc))
        return raw, tree

    def plans_from_tree(self, tree, name, series):
        plans = []
        for folder in sorted(tree.rglob("*")):
            match = SERIES_DIR.match(folder.name)
            if not folder.is_dir() or not match:
                continue
            key = f"{int(match.group(1))}.{int(match.group(2))}"
            found = sorted(folder.rglob("userpref.blend"), key=lambda path: len(path.parts))
            addons = [d for d in folder.rglob("addons") if d.is_dir() and d.parent.name == "scripts"]
            extensions = [d for d in folder.rglob("extensions") if d.is_dir() and "addons" not in d.relative_to(folder).parts]
            if found or addons or extensions:
                plans.append({"key": key, "prefs": found[0] if found else None, "addons": addons, "extensions": extensions})
        if plans:
            if series:
                plans = [plan for plan in plans if plan["key"] == series]
                if not plans:
                    raise SetupError(400, f"В архиве нет папки с настройками версии {series}")
            return plans
        found = sorted(tree.rglob("userpref.blend"), key=lambda path: len(path.parts))
        if not found:
            raise SetupError(400, "В архиве нет файла userpref.blend")
        key = series or series_from_text(name) or series_from_header(found[0])
        if not key:
            raise SetupError(422, NEED_SERIES)
        addons = [d for d in tree.rglob("addons") if d.is_dir() and d.parent.name == "scripts"]
        extensions = [d for d in tree.rglob("extensions") if d.is_dir() and "addons" not in d.relative_to(tree).parts]
        return [{"key": key, "prefs": found[0], "addons": addons, "extensions": extensions}]

    def store_prefs(self, stream, filename, series=None):
        name = storage.safe_name(filename, "userpref.blend")
        lower = name.lower()
        if not (lower.endswith(".zip") or lower.endswith(".blend")):
            raise SetupError(400, "Нужен файл userpref.blend или zip с папкой настроек")
        series = key_of(series) if series else None
        self.root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(dir=self.root, prefix="up-"))
        try:
            raw, tree = self.unpack(stream, name, workdir)
            if tree is None:
                if not looks_like_blend(raw):
                    raise SetupError(400, "Файл не похож на userpref.blend")
                key = series or series_from_text(name) or series_from_header(raw)
                if not key:
                    raise SetupError(422, NEED_SERIES)
                plans = [{"key": key, "prefs": raw, "addons": [], "extensions": []}]
            else:
                plans = self.plans_from_tree(tree, name, series)
            for plan in plans:
                self.tool_blender(plan["key"])
            applied = []
            for plan in plans:
                paths = self.ensure_dirs(plan["key"])
                if plan["prefs"] is not None:
                    clean_copy(plan["prefs"], paths["config"] / "userpref.blend")
                for folder in plan["addons"]:
                    for item in folder.iterdir():
                        if not item.name.startswith(".") and item.name != "__pycache__":
                            clean_copy(item, paths["addons"] / item.name)
                for folder in plan["extensions"]:
                    for repo in folder.iterdir():
                        if not repo.is_dir() or repo.name.startswith("."):
                            continue
                        for item in repo.iterdir():
                            if item.is_dir() and not item.name.startswith("."):
                                clean_copy(item, paths["extensions"] / repo.name / item.name)
                with self.lock:
                    state = self.load()
                    entry = self.entry(state, plan["key"])
                    if plan["prefs"] is not None:
                        entry["prefs"] = {"name": name, "uploaded": time.time()}
                    entry["ignored"] = []
                    entry["addons"] = {}
                    self.save(state)
                applied.append(plan["key"])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        for key in applied:
            self.scan(key)
            self.queue_missing(key)
        result = self.public()
        result["applied"] = applied
        return result

    def resolve_series(self, series, name):
        if series:
            return key_of(series)
        found = series_from_text(name)
        if found:
            return found
        state = self.load()
        keys = sorted(state["series"]) or self.installed_keys()
        if len(keys) == 1:
            return keys[0]
        raise SetupError(422, "Выберите версию Blender, для которой добавляется аддон")

    def install_addon(self, stream, filename, series=None):
        name = storage.safe_name(filename, "addon.zip")
        lower = name.lower()
        key = self.resolve_series(series, name)
        self.tool_blender(key)
        paths = self.ensure_dirs(key)
        workdir = Path(tempfile.mkdtemp(dir=self.root, prefix="ad-"))
        try:
            raw, tree = self.unpack(stream, name, workdir)
            if tree is None:
                if not lower.endswith(".py"):
                    raise SetupError(400, "Нужен аддон в виде .zip или одного файла .py")
                module = name[:-3]
                if not MODULE_RE.match(module):
                    raise SetupError(400, "Имя файла аддона должно состоять из латинских букв, цифр и подчёркиваний")
                clean_copy(raw, paths["addons"] / f"{module}.py")
            else:
                self.install_package(tree, name[:-4], paths)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self.refresh(key)

    def install_package(self, tree, stem, paths):
        roots = [tree]
        children = [item for item in tree.iterdir() if not item.name.startswith(".")]
        if len(children) == 1 and children[0].is_dir():
            roots.append(children[0])
        for root in roots:
            manifest = root / "blender_manifest.toml"
            if manifest.is_file():
                try:
                    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise SetupError(400, "Не удалось прочитать blender_manifest.toml")
                ext_id = str(data.get("id", ""))
                if not MODULE_RE.match(ext_id):
                    raise SetupError(400, "В blender_manifest.toml нет корректного id")
                clean_copy(root, paths["extensions"] / USER_REPO / ext_id)
                return
        for root in roots:
            if (root / "__init__.py").is_file():
                module = root.name if root is not tree else re.sub(r"\W", "_", stem)
                if not MODULE_RE.match(module):
                    module = "addon_" + re.sub(r"\W", "_", module)
                clean_copy(root, paths["addons"] / module)
                return
        raise SetupError(400, "В архиве не найден аддон: нужен __init__.py или blender_manifest.toml")

    def known_key(self, series):
        if not series:
            raise SetupError(422, "Укажите версию Blender")
        return key_of(series)

    def set_enabled(self, module, enabled, series):
        key = self.known_key(series)
        with self.lock:
            state = self.load()
            entry = self.entry(state, key)
            if module not in entry["addons"]:
                raise SetupError(404, "Такого аддона нет в списке")
            entry["addons"][module]["enabled"] = bool(enabled)
            self.save(state)
        return self.public()

    def remove(self, module, series):
        key = self.known_key(series)
        paths = self.paths(key)
        with self.lock:
            state = self.load()
            entry = self.entry(state, key)
            item = entry["addons"].get(module)
            if item is None:
                raise SetupError(404, "Такого аддона нет в списке")
            if item["origin"] == "bundled":
                raise SetupError(400, "Встроенный аддон удалить нельзя, его можно отключить")
            if item["origin"] == "user":
                for candidate in (paths["addons"] / module, paths["addons"] / f"{module}.py"):
                    if candidate.is_dir():
                        shutil.rmtree(candidate)
                    elif candidate.is_file():
                        candidate.unlink()
            elif item["origin"] == "extension":
                parts = module.split(".")
                if len(parts) == 3:
                    shutil.rmtree(paths["extensions"] / parts[1] / parts[2], ignore_errors=True)
            elif module not in entry["ignored"]:
                entry["ignored"].append(module)
            entry["addons"].pop(module, None)
            self.save(state)
        self.errors.pop((key, module), None)
        return self.public()

    def reset_prefs(self, series):
        key = self.known_key(series)
        with self.lock:
            (self.paths(key)["config"] / "userpref.blend").unlink(missing_ok=True)
            state = self.load()
            entry = self.entry(state, key)
            entry["prefs"] = None
            entry["ignored"] = []
            self.save(state)
        if any(item["version"][:2] == tuple_of(key) for item in self.installed_fn()):
            return self.refresh(key)
        return self.public()

    def rescan(self, series):
        return self.refresh(self.known_key(series))

    def finish(self, skipped):
        with self.lock:
            state = self.load()
            state["configured"] = not skipped
            state["skipped"] = bool(skipped)
            self.save(state)
        return self.public()

    def modules_for(self, version, enabled):
        key = key_of(version)
        entry = self.load()["series"].get(key)
        if not entry:
            return []
        series = tuple_of(key)
        names = []
        for module, item in sorted(entry["addons"].items()):
            if item["origin"] == "missing" or bool(item["enabled"]) != enabled:
                continue
            if module.startswith(EXT_PREFIX) and series < EXT_MIN:
                continue
            names.append(module)
        return names

    def job_addons(self, version):
        return self.modules_for(version, True)

    def job_disabled(self, version):
        return self.modules_for(version, False)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)