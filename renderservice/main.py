import asyncio
import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_settings
from .service import Service, ServiceError
from .userenv import SetupError


class StartBody(BaseModel):
    mode: Optional[str] = None
    device: str = "OPTIX"
    frame: Optional[int] = None
    frames: Optional[List[int]] = None
    blender: Optional[str] = None
    allow_missing: bool = False
    resume: bool = False


class SelectBody(BaseModel):
    path: str


class DownloadBody(BaseModel):
    version: str


class EnabledBody(BaseModel):
    enabled: bool


class FinishBody(BaseModel):
    skipped: bool = False


@asynccontextmanager
async def lifespan(app):
    service = app.state.service
    service.startup()
    try:
        yield
    finally:
        service.shutdown()


def create_app(settings=None):
    settings = settings or load_settings()
    service = Service(settings)
    app = FastAPI(title="Blender Render", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def on_service_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(SetupError)
    async def on_setup_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.get("/")
    def index():
        return FileResponse(settings.static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

    @app.get("/api/status")
    def server_status():
        return service.status()

    @app.get("/api/setup")
    def setup_get():
        return service.userenv.public()

    @app.post("/api/setup/prefs")
    def setup_prefs(file: UploadFile = File(...)):
        return service.userenv.store_prefs(file.file, file.filename)

    @app.delete("/api/setup/prefs")
    def setup_prefs_reset():
        return service.userenv.reset_prefs()

    @app.post("/api/setup/addons")
    def setup_addon_upload(file: UploadFile = File(...)):
        return service.userenv.install_addon(file.file, file.filename)

    @app.post("/api/setup/rescan")
    def setup_rescan():
        return service.userenv.refresh()

    @app.post("/api/setup/addons/{module}/enabled")
    def setup_addon_enabled(module: str, body: EnabledBody):
        return service.userenv.set_enabled(module, body.enabled)

    @app.delete("/api/setup/addons/{module}")
    def setup_addon_remove(module: str):
        return service.userenv.remove(module)

    @app.post("/api/setup/done")
    def setup_done(body: FinishBody):
        return service.userenv.finish(body.skipped)

    @app.get("/guide")
    def guide():
        return FileResponse(settings.static_dir / "guide.html")

    @app.get("/api/blenders")
    def blenders_list():
        return service.blenders_info()

    @app.get("/api/blenders/catalog")
    def blenders_catalog(refresh: bool = False):
        return service.blender_catalog(refresh)

    @app.post("/api/blenders/download", status_code=202)
    def blenders_download(body: DownloadBody):
        return service.download_blender(body.version)

    @app.delete("/api/blenders/{version}")
    def blenders_delete(version: str):
        service.remove_blender(version)
        return {"ok": True}

    @app.get("/api/jobs")
    def jobs_list():
        return service.listing()

    @app.post("/api/jobs", status_code=201)
    def jobs_create(file: UploadFile = File(...)):
        job = service.create_job(file.file, file.filename)
        return service.public(job, detail=True)

    @app.get("/api/jobs/{job_id}")
    def jobs_get(job_id: str):
        data = service.snapshot(job_id)
        if data is None:
            raise ServiceError(404, "Задача не найдена")
        return data

    @app.post("/api/jobs/{job_id}/select")
    def jobs_select(job_id: str, body: SelectBody):
        return service.public(service.select(job_id, body.path), detail=True)

    @app.post("/api/jobs/{job_id}/start")
    def jobs_start(job_id: str, body: StartBody):
        return service.public(service.start(job_id, body.model_dump()), detail=True)

    @app.post("/api/jobs/{job_id}/cancel")
    def jobs_cancel(job_id: str):
        return service.public(service.cancel(job_id), detail=True)

    @app.delete("/api/jobs/{job_id}")
    def jobs_delete(job_id: str):
        service.delete(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    def jobs_download(job_id: str):
        path, name = service.download_target(job_id)
        return FileResponse(path, filename=name)

    @app.get("/api/jobs/{job_id}/previews/{name}")
    def jobs_preview(job_id: str, name: str):
        return FileResponse(service.preview_path(job_id, name), media_type="image/jpeg")

    @app.get("/api/jobs/{job_id}/log")
    def jobs_log(job_id: str, lines: int = 200):
        return PlainTextResponse(service.log_tail(job_id, max(1, min(lines, 5000))))

    @app.get("/api/jobs/{job_id}/events")
    async def jobs_events(job_id: str):
        async def stream():
            last = None
            while True:
                data = service.snapshot(job_id)
                if data is None:
                    yield "event: gone\ndata: {}\n\n"
                    return
                payload = json.dumps(data, ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
