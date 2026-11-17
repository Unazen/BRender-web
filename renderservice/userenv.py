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

from . import runner, storage

BLENDER_ORG = "blender_org"
USER_REPO = "user_default"
EXT_PREFIX = "bl_ext."
EXT_MIN = (4, 2)
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BLEND_MAGICS = (b"BLENDER", b"\x1f\x8b", b"\x28\xb5\x2f\xfd")
SCAN_TIMEOUT = 180
INSTALL_TIMEOUT = 600
ORIGIN_ORDER = {"missing": 0, "user": 1, "extension": 2, "bundled": 3}


class SetupError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def series_of(version):
    return tuple(int(part) for part in str(version).split(".")[:2])


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


class UserEnv:
    def __init__(self, settings, installed_fn):
        self.settings = settings
        self.installed_fn = installed_fn
        self.root = settings.user_dir
        self.config = self.root / "config"
        self.scripts = self.root / "scripts"
        self.addons = self.scripts / "addons"
        self.extensions = self.root / "extensions"
        self.state_path = self.root / "state.json"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="addons")
        self.pending = set()
        self.errors = {}
        self.busy = 0

    def ensure_dirs(self):
        for folder in (self.config, self.addons, self.extensions):
            folder.mkdir(parents=True, exist_ok=True)

    def env(self):
        self.ensure_dirs()
        env = dict(os.environ)
        env["BLENDER_USER_CONFIG"] = str(self.config)
        env["BLENDER_USER_SCRIPTS"] = str(self.scripts)
        env["BLENDER_USER_EXTENSIONS"] = str(self.extensions)
        return env

    def load(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("configured", False)
        state.setdefault("skipped", False)
        state.setdefault("prefs", None)
        state.setdefault("addons", {})
        state.setdefault("ignored", [])
        return state

    def save(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name("state.json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def tool_blender(self, extensions=False):
        items = self.installed_fn()
        if extensions:
            items = [item for item in items if item["version"][:2] >= EXT_MIN]
            if not items:
                raise SetupError(409, "Для расширений нужен Blender 4.2 или новее. Скачайте его в разделе «Версии Blender»")
        if not items:
            raise SetupError(409, "Не найдено ни одной версии Blender. Скачайте её в разделе «Версии Blender»")
        return items[-1]["path"]

    def scan(self):
        blender = self.tool_blender()
        self.ensure_dirs()
        target = self.root / "scan.json"
        target.unlink(missing_ok=True)
        command = runner.build_tool_command(blender, self.settings.wrapper, ["--scan", str(target)])
        self.busy += 1
        try:
            result = subprocess.run(
                command,
                env=self.env(),
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
        self.merge(report)

    def is_user_path(self, path):
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(self.root.resolve())
        except OSError:
            return False

    def merge(self, report):
        with self.lock:
            state = self.load()
            known = state["addons"]
            ignored = set(state["ignored"])
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
            state["addons"] = addons
            state["blender"] = report.get("blender")
            state["scanned"] = time.time()
            self.save(state)

    def public(self):
        state = self.load()
        items = []
        for module, item in state["addons"].items():
            status, message = "ok", None
            if item["origin"] == "missing":
                if module in self.pending:
                    status = "installing"
                elif module in self.errors:
                    status, message = "failed", self.errors[module]
                else:
                    status = "missing"
            items.append({"module": module, "status": status, "message": message, **item})
        items.sort(key=lambda entry: (ORIGIN_ORDER[entry["origin"]], entry["name"].lower()))
        installed = self.installed_fn()
        return {
            "configured": state["configured"],
            "skipped": state["skipped"],
            "needs_setup": not state["configured"] and not state["skipped"],
            "prefs": state["prefs"],
            "blender": state.get("blender"),
            "addons": items,
            "busy": bool(self.pending) or self.busy > 0,
            "has_blender": bool(installed),
            "extensions_supported": any(item["version"][:2] >= EXT_MIN for item in installed),
        }

    def refresh(self):
        self.scan()
        self.queue_missing()
        return self.public()

    def queue_missing(self):
        state = self.load()
        for module, item in state["addons"].items():
            if item["origin"] != "missing" or module in self.pending:
                continue
            parts = module.split(".")
            if len(parts) == 3 and parts[0] + "." == EXT_PREFIX and parts[1] == BLENDER_ORG:
                self.errors.pop(module, None)
                self.pending.add(module)
                self.pool.submit(self.install_extension, module, parts[2])

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

    def install_extension(self, module, ext_id):
        try:
            blender = self.tool_blender(extensions=True)
            env = self.env()
            self.run_cli(blender, env, ["sync"])
            try:
                self.run_cli(blender, env, ["install", "--no-prefs", ext_id])
            except SetupError:
                self.run_cli(blender, env, ["install", "--no-prefs", "-r", BLENDER_ORG, ext_id])
            self.scan()
        except SetupError as exc:
            self.errors[module] = exc.message
        except Exception as exc:
            self.errors[module] = str(exc)
        finally:
            self.pending.discard(module)

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

    def store_prefs(self, stream, filename):
        name = storage.safe_name(filename, "userpref.blend")
        lower = name.lower()
        if not (lower.endswith(".zip") or lower.endswith(".blend")):
            raise SetupError(400, "Нужен файл userpref.blend или zip с папкой настроек")
        self.ensure_dirs()
        workdir = Path(tempfile.mkdtemp(dir=self.root, prefix="up-"))
        try:
            raw, tree = self.unpack(stream, name, workdir)
            if tree is None:
                if not looks_like_blend(raw):
                    raise SetupError(400, "Файл не похож на userpref.blend")
                clean_copy(raw, self.config / "userpref.blend")
            else:
                found = sorted(tree.rglob("userpref.blend"), key=lambda path: len(path.parts))
                if not found:
                    raise SetupError(400, "В архиве нет файла userpref.blend")
                clean_copy(found[0], self.config / "userpref.blend")
                self.copy_addons(tree)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        with self.lock:
            state = self.load()
            state["prefs"] = {"name": name, "uploaded": time.time()}
            state["ignored"] = []
            state["addons"] = {}
            self.save(state)
        return self.refresh()

    def copy_addons(self, tree):
        for folder in tree.rglob("addons"):
            if folder.is_dir() and folder.parent.name == "scripts":
                for item in folder.iterdir():
                    if not item.name.startswith(".") and item.name != "__pycache__":
                        clean_copy(item, self.addons / item.name)
        for folder in tree.rglob("extensions"):
            if not folder.is_dir() or "addons" in folder.relative_to(tree).parts:
                continue
            for repo in folder.iterdir():
                if not repo.is_dir() or repo.name.startswith("."):
                    continue
                for item in repo.iterdir():
                    if item.is_dir() and not item.name.startswith("."):
                        clean_copy(item, self.extensions / repo.name / item.name)

    def install_addon(self, stream, filename):
        name = storage.safe_name(filename, "addon.zip")
        lower = name.lower()
        self.ensure_dirs()
        workdir = Path(tempfile.mkdtemp(dir=self.root, prefix="ad-"))
        try:
            raw, tree = self.unpack(stream, name, workdir)
            if tree is None:
                if not lower.endswith(".py"):
                    raise SetupError(400, "Нужен аддон в виде .zip или одного файла .py")
                module = name[:-3]
                if not MODULE_RE.match(module):
                    raise SetupError(400, "Имя файла аддона должно состоять из латинских букв, цифр и подчёркиваний")
                clean_copy(raw, self.addons / f"{module}.py")
            else:
                self.install_package(tree, name[:-4])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self.refresh()

    def install_package(self, tree, stem):
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
                clean_copy(root, self.extensions / USER_REPO / ext_id)
                return
        for root in roots:
            if (root / "__init__.py").is_file():
                module = root.name if root is not tree else re.sub(r"\W", "_", stem)
                if not MODULE_RE.match(module):
                    module = "addon_" + re.sub(r"\W", "_", module)
                clean_copy(root, self.addons / module)
                return
        raise SetupError(400, "В архиве не найден аддон: нужен __init__.py или blender_manifest.toml")

    def set_enabled(self, module, enabled):
        with self.lock:
            state = self.load()
            if module not in state["addons"]:
                raise SetupError(404, "Такого аддона нет в списке")
            state["addons"][module]["enabled"] = bool(enabled)
            self.save(state)
        return self.public()

    def remove(self, module):
        with self.lock:
            state = self.load()
            item = state["addons"].get(module)
            if item is None:
                raise SetupError(404, "Такого аддона нет в списке")
            if item["origin"] == "bundled":
                raise SetupError(400, "Встроенный аддон удалить нельзя, его можно отключить")
            if item["origin"] == "user":
                for candidate in (self.addons / module, self.addons / f"{module}.py"):
                    if candidate.is_dir():
                        shutil.rmtree(candidate)
                    elif candidate.is_file():
                        candidate.unlink()
            elif item["origin"] == "extension":
                parts = module.split(".")
                if len(parts) == 3:
                    shutil.rmtree(self.extensions / parts[1] / parts[2], ignore_errors=True)
            elif module not in state["ignored"]:
                state["ignored"].append(module)
            state["addons"].pop(module, None)
            self.save(state)
        self.errors.pop(module, None)
        return self.public()

    def reset_prefs(self):
        with self.lock:
            (self.config / "userpref.blend").unlink(missing_ok=True)
            state = self.load()
            state["prefs"] = None
            state["ignored"] = []
            self.save(state)
        if self.installed_fn():
            return self.refresh()
        return self.public()

    def finish(self, skipped):
        with self.lock:
            state = self.load()
            state["configured"] = not skipped
            state["skipped"] = bool(skipped)
            self.save(state)
        return self.public()

    def modules_for(self, version, enabled):
        series = series_of(version)
        state = self.load()
        names = []
        for module, item in sorted(state["addons"].items()):
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
