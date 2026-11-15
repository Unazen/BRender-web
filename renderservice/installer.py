import hashlib
import lzma
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import blenders

FALLBACK_SERIES = [
    "5.0", "4.5", "4.4", "4.3", "4.2", "4.1", "4.0",
    "3.6", "3.5", "3.4", "3.3", "3.2", "3.1", "3.0", "2.93",
]
MIN_SERIES = (2, 93)
MIN_FREE_BYTES = 3 * 1024 ** 3
CATALOG_TTL = 3600
ACTIVE_STATES = {"queued", "downloading", "extracting"}
HEADERS = {"User-Agent": "blender-render-service"}
DIR_RE = re.compile(r'href="Blender(\d+\.\d+)/?"')
FILE_RE = re.compile(r'href="blender-(\d+\.\d+\.\d+)-linux-x64\.tar\.xz"')
HASH_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$")
BSD_HASH_RE = re.compile(r"^SHA256 \((.+)\) = ([0-9a-fA-F]{64})$")
FULL_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class InstallError(Exception):
    pass


def _open(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout)


def _fetch_text(url):
    with _open(url, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def _exists(url):
    request = urllib.request.Request(url, headers=HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=15):
            return True
    except urllib.error.HTTPError:
        return False


class Installer:
    def __init__(self, settings, installed_fn):
        self.settings = settings
        self.installed_fn = installed_fn
        self.base = settings.download_base.rstrip("/") + "/"
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-install")
        self.lock = threading.Lock()
        self.states = {}
        self.futures = {}
        self._catalog = None
        self._catalog_time = 0.0

    def series_list(self):
        try:
            html = _fetch_text(self.base)
        except urllib.error.HTTPError:
            html = ""
        found = {tuple(int(part) for part in item.split(".")) for item in DIR_RE.findall(html)}
        if found:
            series = [item for item in found if item >= MIN_SERIES]
        else:
            series = [blenders.parse_series(item) for item in FALLBACK_SERIES]
        return sorted(series, reverse=True)

    def _probe(self, series):
        try:
            for patch in range(30, -1, -1):
                version = f"{series}.{patch}"
                if _exists(f"{self.base}Blender{series}/blender-{version}-linux-x64.tar.xz"):
                    return [tuple(int(part) for part in version.split("."))]
        except (urllib.error.URLError, OSError):
            return []
        return []

    def versions_for(self, series):
        versions = []
        try:
            html = _fetch_text(f"{self.base}Blender{series}/")
            versions = sorted({tuple(int(part) for part in item.split(".")) for item in FILE_RE.findall(html)})
        except (urllib.error.URLError, OSError):
            versions = []
        if not versions:
            versions = self._probe(series)
        return versions

    def catalog(self, force=False):
        with self.lock:
            if self._catalog and not force and time.time() - self._catalog_time < CATALOG_TTL:
                return self._catalog, None
        try:
            series = self.series_list()
        except (urllib.error.URLError, OSError) as exc:
            return list(self._catalog or []), f"Сервер загрузки недоступен: {exc}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda item: self.versions_for(blenders.label(item)), series))
        entries = []
        for item, versions in zip(series, results):
            if versions:
                entries.append({
                    "series": blenders.label(item),
                    "versions": [blenders.label(version) for version in reversed(versions)],
                    "latest": blenders.label(versions[-1]),
                })
        if not entries:
            return [], "Не удалось получить список версий с сервера загрузки"
        with self.lock:
            self._catalog = entries
            self._catalog_time = time.time()
        return entries, None

    def resolve_version(self, text):
        text = text.strip()
        match = FULL_VERSION_RE.fullmatch(text)
        if match:
            return text, f"{match.group(1)}.{match.group(2)}"
        series = blenders.label(blenders.parse_series(text))
        versions = self.versions_for(series)
        if not versions:
            raise InstallError(
                f"Для серии {series} не нашлось сборки Linux на сервере загрузки, либо сервер недоступен"
            )
        return blenders.label(versions[-1]), series

    def install(self, text):
        version, series = self.resolve_version(text)
        if any(blenders.label(item["version"]) == version for item in self.installed_fn()):
            return None, {"version": version, "series": series, "state": "done", "downloaded": 0, "total": None, "error": None}
        with self.lock:
            future = self.futures.get(version)
            if future is not None and not future.done():
                return future, self.states[version]
            state = {
                "version": version,
                "series": series,
                "state": "queued",
                "downloaded": 0,
                "total": None,
                "error": None,
                "started": time.time(),
            }
            self.states[version] = state
            future = self.pool.submit(self._run, version, series, state)
            self.futures[version] = future
        return future, state

    def ensure(self, series_text):
        future, state = self.install(series_text)
        if future is None:
            return
        future.result()
        if state["state"] != "done":
            raise InstallError(state["error"] or "установка не удалась")

    def statuses(self):
        with self.lock:
            return [dict(state) for state in self.states.values()]

    def active_for_series(self, series):
        with self.lock:
            for state in self.states.values():
                if state["series"] == series and state["state"] in ACTIVE_STATES:
                    return dict(state)
        return None

    def forget(self, version):
        with self.lock:
            self.states.pop(version, None)
            self.futures.pop(version, None)

    def _expected_hash(self, series, version, archive_name):
        try:
            text = _fetch_text(f"{self.base}Blender{series}/blender-{version}.sha256")
        except (urllib.error.URLError, OSError):
            return None
        for line in text.splitlines():
            line = line.strip()
            match = HASH_RE.match(line)
            if match and match.group(2) == archive_name:
                return match.group(1).lower()
            match = BSD_HASH_RE.match(line)
            if match and match.group(1) == archive_name:
                return match.group(2).lower()
        return None

    def _download(self, url, part, state):
        digest = hashlib.sha256()
        with _open(url) as response, open(part, "wb") as out:
            length = response.headers.get("Content-Length")
            state["total"] = int(length) if length else None
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                state["downloaded"] += len(chunk)
        if state["total"] is not None and state["downloaded"] != state["total"]:
            raise InstallError("Скачивание прервалось, файл получен не полностью")
        return digest.hexdigest()

    def _extract(self, archive, stage, trusted):
        if not hasattr(tarfile, "data_filter"):
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(stage)
            return
        try:
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(stage, filter="data")
        except tarfile.FilterError:
            if not trusted:
                raise
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir()
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(stage, filter="tar")

    def _run(self, version, series, state):
        root = Path(self.settings.blender_root)
        folder = f"blender-{version}-linux-x64"
        archive_name = f"{folder}.tar.xz"
        work = root / ".downloads"
        part = work / (archive_name + ".part")
        stage = root / f".extract-{version}"
        try:
            root.mkdir(parents=True, exist_ok=True)
            work.mkdir(exist_ok=True)
            free = shutil.disk_usage(root).free
            if free < MIN_FREE_BYTES:
                raise InstallError(f"Мало места на диске: свободно {free / 1024 ** 3:.1f} ГБ, нужно не меньше 3 ГБ")
            expected = self._expected_hash(series, version, archive_name)
            state["verified"] = bool(expected)
            state["state"] = "downloading"
            digest = self._download(f"{self.base}Blender{series}/{archive_name}", part, state)
            if expected and digest != expected:
                raise InstallError("Контрольная сумма архива не совпала, файл повреждён")
            state["state"] = "extracting"
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir()
            self._extract(part, stage, trusted=bool(expected))
            source = stage / folder
            if not (source / "blender").is_file():
                folders = [path for path in stage.iterdir() if path.is_dir() and (path / "blender").is_file()]
                if len(folders) != 1:
                    raise InstallError("В архиве не нашлось исполняемого файла Blender")
                source = folders[0]
            target = root / folder
            if not target.exists():
                os.replace(source, target)
            state["state"] = "done"
        except InstallError as exc:
            state.update(state="failed", error=str(exc))
        except urllib.error.HTTPError as exc:
            hint = "такой версии нет на сервере загрузки" if exc.code == 404 else f"ответ сервера {exc.code}"
            state.update(state="failed", error=f"Не удалось скачать Blender {version}: {hint}")
        except (OSError, urllib.error.URLError, tarfile.TarError, EOFError, lzma.LZMAError) as exc:
            state.update(state="failed", error=f"Не удалось установить Blender {version}: {exc}")
        except Exception as exc:
            state.update(state="failed", error=f"Неожиданная ошибка установки Blender {version}: {exc}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            try:
                part.unlink()
            except FileNotFoundError:
                pass
