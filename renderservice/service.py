import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import blenders, blendver, cleanup, progress, runner, storage, thumbs
from .db import Database
from .installer import ACTIVE_STATES, MIN_SERIES, InstallError, Installer
from .worker import Worker

DEVICES = {"OPTIX", "CUDA", "CPU"}
MODES = {"still", "animation"}
STARTABLE = {"ready", "done", "failed", "cancelled"}
FINISHED = {"done", "failed", "cancelled"}


class ServiceError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Service:
    def __init__(self, settings):
        self.settings = settings
        settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.db_path)
        self.worker = Worker(self)
        self.preflight_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preflight")
        self.installer = Installer(settings, self.installed)
        self.stop_event = threading.Event()
        self.cleanup_thread = threading.Thread(target=self._cleanup_loop, name="cleanup", daemon=True)

    def startup(self):
        self.recover()
        cleanup.run_cleanup(self)
        self.worker.start()
        self.cleanup_thread.start()

    def shutdown(self):
        self.stop_event.set()
        self.worker.stop()
        self.preflight_pool.shutdown(wait=False, cancel_futures=True)
        self.installer.pool.shutdown(wait=False, cancel_futures=True)
        self.db.close()

    def _cleanup_loop(self):
        while not self.stop_event.wait(3600):
            try:
                cleanup.run_cleanup(self)
            except Exception:
                pass

    def recover(self):
        for job in self.db.list():
            if job["status"] == "rendering":
                options = dict(job.get("options") or {})
                options["resume"] = True
                self.db.update(
                    job["id"],
                    status="queued",
                    options=options,
                    queued_at=job.get("queued_at") or time.time(),
                    cancelling=False,
                )
            elif job["status"] == "preflight":
                self.preflight_pool.submit(self._preflight, job["id"])

    def paths(self, job_id):
        return storage.JobPaths(self.settings.jobs_dir, job_id)

    def installed(self):
        return blenders.discover(self.settings.blender_root)

    def _require(self, job_id):
        job = self.db.get(job_id)
        if job is None:
            raise ServiceError(404, "Задача не найдена")
        return job

    def finish(self, job_id, status, error=None):
        self.db.update(job_id, status=status, finished=time.time(), error=error, error_log=None, cancelling=False)

    def fail(self, job_id, message, tail=None):
        self.db.update(
            job_id,
            status="failed",
            finished=time.time(),
            error=message,
            error_log=tail or [],
            install_version=None,
            cancelling=False,
        )

    def _save_upload(self, source, target):
        limit = self.settings.max_upload_bytes
        written = 0
        with open(target, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if limit and written > limit:
                    raise ServiceError(413, "Файл больше допустимого размера")
                out.write(chunk)
        return written

    def create_job(self, source, filename):
        name = storage.safe_name(filename)
        lower = name.lower()
        if not (lower.endswith(".blend") or lower.endswith(".zip")):
            raise ServiceError(400, "Нужен файл .blend или .zip")
        job_id = secrets.token_hex(6)
        paths = self.paths(job_id)
        paths.project.mkdir(parents=True)
        try:
            self._save_upload(source, paths.upload)
            if lower.endswith(".zip"):
                try:
                    storage.safe_extract(paths.upload, paths.project)
                except (zipfile.BadZipFile, ValueError) as exc:
                    raise ServiceError(400, f"Не удалось распаковать архив: {exc}")
                os.remove(paths.upload)
            else:
                os.replace(paths.upload, paths.project / name)
            candidates = storage.find_blends(paths.project)
            if not candidates:
                raise ServiceError(400, "В загруженных файлах нет .blend")
            if len(candidates) == 1:
                try:
                    blendver.read_version(paths.project / candidates[0])
                except (ValueError, RuntimeError) as exc:
                    raise ServiceError(400, str(exc))
        except Exception:
            storage.rmtree(paths.root)
            raise

        now = time.time()
        single = len(candidates) == 1
        job = {
            "id": job_id,
            "name": Path(name).stem,
            "filename": name,
            "status": "preflight" if single else "needs_selection",
            "created": now,
            "updated": now,
            "candidates": candidates,
            "blend": candidates[0] if single else None,
            "info": None,
            "project_version": None,
            "blender": None,
            "warnings": [],
            "options": {},
            "error": None,
            "error_log": None,
            "attempts": 0,
            "queued_at": None,
            "started": None,
            "finished": None,
            "cancelling": False,
            "project_deleted": False,
        }
        self.db.insert(job)
        if single:
            self.preflight_pool.submit(self._preflight, job_id)
        return job

    def select(self, job_id, path):
        with self.db.lock:
            job = self._require(job_id)
            if job["status"] != "needs_selection":
                raise ServiceError(409, "Выбор файла сейчас не требуется")
            if path not in job["candidates"]:
                raise ServiceError(400, "Такого файла нет в проекте")
            try:
                blendver.read_version(self.paths(job_id).project / path)
            except (ValueError, RuntimeError) as exc:
                raise ServiceError(400, str(exc))
            job = self.db.update(job_id, blend=path, status="preflight")
        self.preflight_pool.submit(self._preflight, job_id)
        return job

    def _preflight(self, job_id):
        try:
            job = self.db.get(job_id)
            if not job or job["status"] != "preflight":
                return
            paths = self.paths(job_id)
            blend = paths.project / job["blend"]
            try:
                series = blendver.read_version(blend)
            except (ValueError, RuntimeError) as exc:
                self.fail(job_id, str(exc))
                return
            note = self._ensure_series(job_id, series)
            try:
                resolved, warnings = blenders.resolve(self.installed(), series)
            except LookupError:
                self.fail(job_id, self.no_blender_message(note))
                return
            if note:
                warnings.append(f"Не удалось скачать Blender {blenders.label(series)}: {note}")
            try:
                paths.preflight.unlink()
            except FileNotFoundError:
                pass
            command = runner.build_command(
                resolved["path"], blend, self.settings.wrapper, ["--preflight", str(paths.preflight)], False
            )
            try:
                with open(paths.preflight_log, "w", encoding="utf-8", errors="replace") as log:
                    subprocess.run(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=self.settings.preflight_timeout,
                        start_new_session=True,
                    )
            except subprocess.TimeoutExpired:
                self.fail(job_id, "Проверка проекта заняла слишком много времени")
                return
            except OSError as exc:
                self.fail(job_id, f"Не удалось запустить Blender: {exc}")
                return
            info = progress.read_json(paths.preflight)
            if info is None:
                self.fail(job_id, "Не удалось прочитать проект в Blender", storage.tail_lines(paths.preflight_log, 8))
                return
            self.db.update(
                job_id,
                status="ready",
                install_version=None,
                info=info,
                project_version=blenders.label(series),
                blender={"version": resolved["version"], "path": resolved["path"]},
                warnings=warnings,
                error=None,
            )
        except Exception as exc:
            self.fail(job_id, f"Ошибка проверки проекта: {exc}")

    def start(self, job_id, body):
        device = body.get("device") or "OPTIX"
        mode = body.get("mode")
        frames = body.get("frames")
        frame = body.get("frame")
        if device not in DEVICES:
            raise ServiceError(400, "Устройство должно быть OPTIX, CUDA или CPU")
        if mode is not None and mode not in MODES:
            raise ServiceError(400, "Режим должен быть still или animation")
        if frames is not None:
            if len(frames) != 2 or frames[0] > frames[1] or min(frames) < 0:
                raise ServiceError(400, "Диапазон кадров задан неверно")
            frames = [int(frames[0]), int(frames[1])]
        if frame is not None and frame < 0:
            raise ServiceError(400, "Номер кадра должен быть неотрицательным")

        with self.db.lock:
            job = self._require(job_id)
            if job["status"] not in STARTABLE:
                raise ServiceError(409, "Задачу сейчас нельзя запустить")
            info = job.get("info")
            if not info:
                raise ServiceError(409, "Проект ещё не проверен")
            paths = self.paths(job_id)
            blend = paths.project / job["blend"]
            if job.get("project_deleted") or not blend.is_file():
                raise ServiceError(410, "Файлы проекта удалены, загрузите проект заново")
            if info.get("missing_files") and not body.get("allow_missing"):
                raise ServiceError(409, "В проекте не найдены файлы, подтвердите запуск без них")

            if mode is None:
                mode = "animation" if info.get("frame_end", 1) > info.get("frame_start", 1) else "still"
            if mode == "still" and frames:
                raise ServiceError(400, "Диапазон кадров задаётся только для анимации")
            if mode == "animation" and frame is not None:
                raise ServiceError(400, "Номер кадра задаётся только для одного кадра")

            requested = body.get("blender")
            try:
                series = blendver.read_version(blend)
                resolved, warnings = blenders.resolve(self.installed(), series, requested)
            except ValueError as exc:
                raise ServiceError(400, str(exc))
            except LookupError:
                raise ServiceError(409, self.no_blender_message())
            if requested and tuple(int(part) for part in resolved["version"].split(".")[:2]) != blenders.parse_series(requested):
                raise ServiceError(409, f"Blender {requested} не установлен, скачайте его в разделе «Версии Blender»")

            resume = bool(body.get("resume"))
            if not resume:
                storage.reset_outputs(paths)
            options = {
                "mode": mode,
                "device": device,
                "frame": frame,
                "frames": frames,
                "blender": body.get("blender"),
                "allow_missing": bool(body.get("allow_missing")),
                "resume": resume,
            }
            job = self.db.update(
                job_id,
                status="queued",
                options=options,
                blender={"version": resolved["version"], "path": resolved["path"]},
                warnings=warnings,
                error=None,
                error_log=None,
                finished=None,
                started=None,
                queued_at=time.time(),
                cancelling=False,
            )
        self.worker.wake()
        return job

    def cancel(self, job_id):
        with self.db.lock:
            job = self._require(job_id)
            if job["status"] == "queued":
                self.db.update(job_id, status="cancelled", finished=time.time(), cancelling=False)
            elif job["status"] == "rendering":
                if not self.worker.request_cancel(job_id):
                    raise ServiceError(409, "Задача сейчас не выполняется")
                self.db.update(job_id, cancelling=True)
            else:
                raise ServiceError(409, "Эту задачу нельзя отменить")
        return self.db.get(job_id)

    def delete(self, job_id):
        with self.db.lock:
            job = self._require(job_id)
            if job["status"] in ("preflight", "rendering"):
                raise ServiceError(409, "Задачу нельзя удалить, пока она выполняется")
            self.db.delete(job_id)
        storage.rmtree(self.paths(job_id).root)

    def download_target(self, job_id):
        job = self._require(job_id)
        if job["status"] in ("queued", "rendering"):
            raise ServiceError(409, "Рендер ещё не закончен")
        paths = self.paths(job_id)
        outputs = storage.list_outputs(paths.renders)
        if not outputs:
            raise ServiceError(404, "Файлов рендера нет")
        if len(outputs) == 1:
            return outputs[0], outputs[0].name
        if not storage.archive_is_fresh(paths.renders, paths.archive):
            storage.build_archive(paths.renders, paths.archive)
        return paths.archive, f"{job['name']}_renders.zip"

    def log_tail(self, job_id, lines):
        self._require(job_id)
        return "\n".join(storage.tail_lines(self.paths(job_id).log, lines))

    def preview_path(self, job_id, name):
        self._require(job_id)
        if Path(name).name != name:
            raise ServiceError(400, "Недопустимое имя файла")
        path = self.paths(job_id).preview / name
        if not path.is_file():
            raise ServiceError(404, "Превью не найдено")
        return path

    def queue_position(self, job):
        if job["status"] != "queued":
            return None
        queued = sorted(
            (item for item in self.db.list() if item["status"] == "queued"),
            key=lambda item: item.get("queued_at") or 0,
        )
        for index, item in enumerate(queued, start=1):
            if item["id"] == job["id"]:
                return index
        return None

    def public(self, job, detail=False):
        paths = self.paths(job["id"])
        data = dict(job)
        data["blender"] = {"version": job["blender"]["version"]} if job.get("blender") else None
        data["progress"] = progress.build(job, paths, self.worker.live.get(job["id"]))
        outputs = storage.list_outputs(paths.renders)
        data["outputs"] = {"count": len(outputs), "bytes": sum(path.stat().st_size for path in outputs)}
        previews = thumbs.list_previews(paths.preview)
        data["previews_count"] = len(previews)
        data["last_preview"] = previews[-1] if previews else None
        if detail:
            data["previews"] = previews
        info = job.get("info") or {}
        data["suggested_mode"] = "animation" if info.get("frame_end", 1) > info.get("frame_start", 1) else "still"
        data["install"] = None
        if job.get("install_version") and job["status"] == "preflight":
            data["install"] = next(
                (state for state in self.installer.statuses() if state["version"] == job["install_version"]),
                None,
            )
        data["queue_position"] = self.queue_position(job)
        return data

    def snapshot(self, job_id):
        job = self.db.get(job_id)
        return self.public(job, detail=True) if job else None

    def listing(self):
        return [self.public(job) for job in self.db.list()]

    def blenders_info(self):
        return [{"version": blenders.label(item["version"]), "path": item["path"]} for item in self.installed()]

    def status(self):
        jobs = self.db.list()
        rendering = next((job for job in jobs if job["status"] == "rendering"), None)
        usage = shutil.disk_usage(self.settings.data_dir)
        return {
            "rendering": rendering["name"] if rendering else None,
            "queued": sum(1 for job in jobs if job["status"] == "queued"),
            "jobs": len(jobs),
            "disk_free": usage.free,
        }

    def no_blender_message(self, note=None):
        text = f"Не найдено ни одной версии Blender в папке {self.settings.blender_root}"
        if note:
            text += f" ({note})"
        return text + ". Скачайте нужную версию в разделе «Версии Blender» или укажите папку через RENDER_BLENDER_ROOT"

    def _ensure_series(self, job_id, series):
        if not self.settings.auto_install or series < MIN_SERIES:
            return None
        if any(item["version"][:2] == series for item in self.installed()):
            return None
        try:
            future, state = self.installer.install(blenders.label(series))
            if future is None:
                return None
            self.db.update(job_id, install_version=state["version"])
            future.result()
        except InstallError as exc:
            return str(exc)
        finally:
            self.db.update(job_id, install_version=None)
        if state["state"] != "done":
            return state["error"] or "установка не удалась"
        return None

    def blender_catalog(self, refresh=False):
        entries, error = self.installer.catalog(force=refresh)
        now = time.time()
        downloads = [
            state
            for state in self.installer.statuses()
            if state["state"] in ACTIVE_STATES or (state["state"] == "failed" and now - state["started"] < 3600)
        ]
        return {
            "root": str(self.settings.blender_root),
            "auto_install": self.settings.auto_install,
            "installed": self.blenders_info(),
            "catalog": entries,
            "downloads": downloads,
            "error": error,
        }

    def download_blender(self, text):
        try:
            future, state = self.installer.install(text)
        except ValueError as exc:
            raise ServiceError(400, str(exc))
        except InstallError as exc:
            raise ServiceError(502, str(exc))
        return dict(state)

    def remove_blender(self, version):
        entry = next((item for item in self.installed() if blenders.label(item["version"]) == version), None)
        if entry is None:
            raise ServiceError(404, "Такая версия не установлена")
        folder = Path(entry["path"]).parent.resolve()
        root = Path(self.settings.blender_root).resolve()
        if folder == root or root not in folder.parents:
            raise ServiceError(409, "Эта установка лежит вне папки версий или прямо в ней, удалите её вручную")
        for job in self.db.list():
            if job["status"] in ("preflight", "queued", "rendering") and (job.get("blender") or {}).get("version") == version:
                raise ServiceError(409, "Эта версия сейчас используется задачей")
        storage.rmtree(folder)
        self.installer.forget(version)
