import os
import signal
import subprocess
import threading
import time
import traceback
from collections import deque

from . import progress, runner, storage, thumbs


class Worker(threading.Thread):
    def __init__(self, service):
        super().__init__(name="render-worker", daemon=True)
        self.service = service
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.live = {}
        self.current_id = None
        self.proc = None
        self.cancel_requested = False

    def wake(self):
        self.wake_event.set()

    def run(self):
        db = self.service.db
        while not self.stop_event.is_set():
            with db.lock:
                job = db.claim_next()
                if job:
                    self.current_id = job["id"]
                    self.cancel_requested = False
                    self.proc = None
            if job is None:
                self.wake_event.wait(2)
                self.wake_event.clear()
                continue
            try:
                self.process(job)
            except Exception:
                detail = traceback.format_exc().splitlines()[-8:]
                self.service.fail(job["id"], "Внутренняя ошибка сервера", detail)
            finally:
                with db.lock:
                    self.current_id = None
                    self.proc = None
                self.live.pop(job["id"], None)

    def request_cancel(self, job_id):
        if self.current_id != job_id:
            return False
        self.cancel_requested = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
        return True

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
        if self.is_alive():
            self.join(timeout=self.service.settings.kill_grace + 5)

    def _terminate(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        timer = threading.Timer(self.service.settings.kill_grace, self._kill, args=(proc,))
        timer.daemon = True
        timer.start()

    def _kill(self, proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def process(self, job):
        service = self.service
        job_id = job["id"]
        paths = service.paths(job_id)
        options = job["options"]
        info = job.get("info") or {}
        blend = paths.project / job["blend"]
        if not blend.is_file():
            service.fail(job_id, "Файл проекта не найден на сервере")
            return

        paths.renders.mkdir(parents=True, exist_ok=True)
        paths.preview.mkdir(parents=True, exist_ok=True)
        try:
            paths.progress.unlink()
        except FileNotFoundError:
            pass

        wrapper_args = [
            "--mode", options["mode"],
            "--device", options["device"],
            "--out", str(paths.renders),
            "--progress", str(paths.progress),
        ]
        if options.get("frame") is not None:
            wrapper_args += ["--frame", str(options["frame"])]
        if options.get("frames"):
            wrapper_args += ["--frames", f"{options['frames'][0]}:{options['frames'][1]}"]
        command = runner.build_command(
            job["blender"]["path"],
            blend,
            service.settings.wrapper,
            wrapper_args,
            runner.use_xvfb(service.settings, info.get("engine", "")),
        )

        try:
            code, tail = self._run(command, paths, job_id, append=bool(options.get("resume")))
        except OSError as exc:
            service.fail(job_id, f"Не удалось запустить Blender: {exc}")
            return

        thumbs.sync_previews(paths.renders, paths.preview, settle=0)
        if self.stop_event.is_set() and not self.cancel_requested:
            return
        if self.cancel_requested:
            service.finish(job_id, "cancelled")
            return
        if code != 0:
            service.fail(job_id, runner.classify_failure(code, tail), list(tail)[-8:])
            return

        outputs = storage.list_outputs(paths.renders)
        if not outputs:
            service.fail(job_id, "Рендер завершился, но файлов нет", list(tail)[-8:])
            return
        if options["mode"] == "animation" and info.get("format") not in progress.VIDEO_FORMATS:
            total = progress.expected_total(info, options)
            found = storage.count_images(paths.renders)
            if found < total:
                service.fail(
                    job_id,
                    f"Рендер завершился не полностью: {found} из {total} кадров",
                    list(tail)[-8:],
                )
                return
        if len(outputs) > 1:
            storage.build_archive(paths.renders, paths.archive)
        service.finish(job_id, "done")

    def _run(self, command, paths, job_id, append):
        tail = deque(maxlen=40)
        with open(paths.log, "a" if append else "w", encoding="utf-8", errors="replace") as log:
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            self.proc = proc
            if self.cancel_requested or self.stop_event.is_set():
                self._terminate(proc)
            last_scan = 0.0
            for line in proc.stdout:
                log.write(line)
                log.flush()
                tail.append(line.rstrip())
                sample, remaining = runner.parse_line(line)
                if sample:
                    self.live[job_id] = {"sample": sample, "remaining": remaining, "time": time.time()}
                now = time.time()
                if line.startswith("Saved:"):
                    last_scan = now
                    thumbs.sync_previews(paths.renders, paths.preview, settle=0)
                elif now - last_scan > 3:
                    last_scan = now
                    thumbs.sync_previews(paths.renders, paths.preview)
            code = proc.wait()
        return code, tail
