import time

from . import storage

BUSY = {"queued", "rendering", "preflight"}
FINISHED = {"done", "failed", "cancelled"}


def run_cleanup(service, now=None):
    now = now or time.time()
    settings = service.settings
    project_age = settings.project_days * 86400
    result_age = settings.result_days * 86400
    removed = []
    trimmed = []
    for job in service.db.list():
        if job["status"] in BUSY:
            continue
        if job["status"] in FINISHED:
            reference = job.get("finished") or job["updated"]
        else:
            reference = job["updated"]
        age = now - reference
        paths = service.paths(job["id"])
        drop_all = age > result_age or (job["status"] not in FINISHED and age > project_age)
        if drop_all:
            with service.db.lock:
                current = service.db.get(job["id"])
                if current is None or current["status"] in BUSY:
                    continue
                service.db.delete(job["id"])
            storage.rmtree(paths.root)
            removed.append(job["id"])
        elif age > project_age and not job.get("project_deleted") and paths.project.exists():
            storage.rmtree(paths.project)
            service.db.update(job["id"], project_deleted=True)
            trimmed.append(job["id"])
    return removed, trimmed
