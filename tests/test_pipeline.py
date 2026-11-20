import functools
import hashlib
import http.server
import io
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from renderservice import blenders, cleanup
from renderservice.config import Settings
from renderservice.main import create_app
from renderservice.service import Service

FAKE = Path(__file__).with_name("fake_blender.py")


def make_settings(tmp, versions=("3.6.5", "4.5.12"), **extra):
    root = tmp / "blenders"
    for version in versions:
        folder = root / f"blender-{version}-linux-x64"
        folder.mkdir(parents=True)
        target = folder / "blender"
        shutil.copy(FAKE, target)
        target.chmod(0o755)
    return Settings(data_dir=tmp / "data", blender_root=root, xvfb="false", kill_grace=2, **extra)


def blend(header="BLENDER-v405", marker=b""):
    return header.encode() + b"REND" + b"\0" * 64 + marker


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def upload(client, name, content):
    return client.post("/api/jobs", files={"file": (name, content, "application/octet-stream")})


def wait_status(client, job_id, targets, timeout=40):
    end = time.time() + timeout
    job = {}
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in targets:
            return job
        time.sleep(0.1)
    raise AssertionError(f"таймаут: статус {job.get('status')}, ошибка {job.get('error')}")


def wait_until(check, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("условие не выполнилось за отведённое время")


def make_client(tmp, **extra):
    settings = make_settings(tmp, **extra)
    return TestClient(create_app(settings)), settings


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class FakeRelease:
    def __init__(self, tmp, versions, wrong_hash=False, with_hash=True):
        self.site = tmp / "site"
        release = self.site / "release"
        for version in versions:
            series = ".".join(version.split(".")[:2])
            folder = release / f"Blender{series}"
            folder.mkdir(parents=True, exist_ok=True)
            name = f"blender-{version}-linux-x64"
            stage = tmp / "stage" / name
            stage.mkdir(parents=True)
            shutil.copy(FAKE, stage / "blender")
            (stage / "blender").chmod(0o755)
            (stage / "notes.txt").write_text("fake")
            archive = folder / f"{name}.tar.xz"
            with tarfile.open(archive, "w:xz") as bundle:
                bundle.add(stage, arcname=name)
            if with_hash:
                digest = "0" * 64 if wrong_hash else hashlib.sha256(archive.read_bytes()).hexdigest()
                (folder / f"blender-{version}.sha256").write_text(
                    f"{digest}  {name}.tar.xz\n{'1' * 64}  blender-{version}-windows-x64.zip\n"
                )
        handler = functools.partial(QuietHandler, directory=str(self.site))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/release/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def installed_versions(client):
    return [item["version"] for item in client.get("/api/blenders").json()]


def test_blend_render_and_download(tmp):
    client, settings = make_client(tmp)
    with client:
        response = upload(client, "scene.blend", blend())
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        job = wait_status(client, job_id, {"ready", "failed"})
        assert job["status"] == "ready", job
        assert job["project_version"] == "4.5"
        assert job["blender"]["version"] == "4.5.12"
        assert job["info"]["engine"] == "CYCLES"
        assert job["suggested_mode"] == "animation"
        assert job["warnings"] == []

        started = client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frames": [1, 3]})
        assert started.status_code == 200, started.text
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done", job
        assert job["outputs"]["count"] == 3
        assert job["progress"]["percent"] == 1.0
        assert job["progress"]["frames_done"] == 3
        wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["previews_count"] == 3)
        preview = client.get(f"/api/jobs/{job_id}/previews/img_0001.png.jpg")
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"

        download = client.get(f"/api/jobs/{job_id}/download")
        assert download.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert sorted(archive.namelist()) == ["img_0001.png", "img_0002.png", "img_0003.png"]

        assert "Sample" in client.get(f"/api/jobs/{job_id}/log").text

        again = client.post(f"/api/jobs/{job_id}/start", json={"mode": "still", "frame": 2})
        assert again.status_code == 200, again.text
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done", job
        assert job["outputs"]["count"] == 1
        single = client.get(f"/api/jobs/{job_id}/download")
        assert single.headers["content-type"] == "image/png"
        assert single.content[:4] == b"\x89PNG"

        renders = settings.jobs_dir / job_id / "renders"
        assert [p.name for p in renders.iterdir()] == ["img_0002.png"]


def test_zip_with_textures_and_zip_slip(tmp):
    client, settings = make_client(tmp)
    with client:
        good = zip_bytes({
            "proj/scene.blend": blend(),
            "proj\\tex\\a.png": b"png",
            "__MACOSX/junk": b"x",
        })
        response = upload(client, "proj.zip", good)
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        job = wait_status(client, job_id, {"ready", "failed"})
        assert job["status"] == "ready", job
        assert job["blend"] == "proj/scene.blend"
        project = settings.jobs_dir / job_id / "project"
        assert (project / "proj" / "tex" / "a.png").read_bytes() == b"png"
        assert not (project / "__MACOSX").exists()

        before = {p.name for p in settings.jobs_dir.iterdir()}
        evil = zip_bytes({"scene.blend": blend(), "../evil.txt": b"x"})
        bad = upload(client, "evil.zip", evil)
        assert bad.status_code == 400, bad.text
        assert {p.name for p in settings.jobs_dir.iterdir()} == before
        assert not (settings.jobs_dir / "evil.txt").exists()

        assert upload(client, "empty.zip", zip_bytes({"readme.txt": b"hi"})).status_code == 400
        assert upload(client, "broken.zip", b"not a zip").status_code == 400
        assert upload(client, "notes.txt", b"hi").status_code == 400
        assert upload(client, "junk.blend", b"this is not a blend file").status_code == 400
        assert {p.name for p in settings.jobs_dir.iterdir()} == before


def test_multiple_blends_need_selection(tmp):
    client, settings = make_client(tmp)
    with client:
        archive = zip_bytes({"a.blend": blend(), "sub/b.blend": blend("BLENDER-v306")})
        job = upload(client, "pack.zip", archive).json()
        assert job["status"] == "needs_selection"
        assert job["candidates"] == ["a.blend", "sub/b.blend"]
        assert client.post(f"/api/jobs/{job['id']}/select", json={"path": "nope.blend"}).status_code == 400
        selected = client.post(f"/api/jobs/{job['id']}/select", json={"path": "sub/b.blend"})
        assert selected.status_code == 200, selected.text
        job = wait_status(client, job["id"], {"ready", "failed"})
        assert job["status"] == "ready", job
        assert job["blender"]["version"] == "3.6.5"
        assert job["project_version"] == "3.6"


def test_missing_files_require_confirmation(tmp):
    client, settings = make_client(tmp)
    with client:
        job_id = upload(client, "m.blend", blend(marker=b"MISSING STILL")).json()["id"]
        job = wait_status(client, job_id, {"ready", "failed"})
        assert job["status"] == "ready"
        assert len(job["info"]["missing_files"]) == 1
        assert job["suggested_mode"] == "still"
        denied = client.post(f"/api/jobs/{job_id}/start", json={})
        assert denied.status_code == 409
        allowed = client.post(f"/api/jobs/{job_id}/start", json={"allow_missing": True})
        assert allowed.status_code == 200, allowed.text
        assert wait_status(client, job_id, {"done", "failed"})["status"] == "done"


def test_start_validation(tmp):
    client, settings = make_client(tmp)
    with client:
        job_id = upload(client, "v.blend", blend()).json()["id"]
        wait_status(client, job_id, {"ready"})
        assert client.post(f"/api/jobs/{job_id}/start", json={"device": "ROCM"}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"mode": "video"}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"frames": [5, 1]}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"mode": "still", "frames": [1, 2]}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frame": 3}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"blender": "abc"}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
        assert client.get("/api/jobs/unknown").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/previews/..%2Fx").status_code in (400, 404)


def test_cancel_and_resume(tmp):
    client, settings = make_client(tmp)
    with client:
        job_id = upload(client, "s.blend", blend(marker=b"SLOW")).json()["id"]
        wait_status(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frames": [1, 3]})
        wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["outputs"]["count"] >= 1)
        live = client.get(f"/api/jobs/{job_id}").json()
        assert live["status"] == "rendering"
        assert live["progress"]["eta_seconds"] is not None
        assert client.delete(f"/api/jobs/{job_id}").status_code == 409

        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        job = wait_status(client, job_id, {"cancelled", "done", "failed"})
        assert job["status"] == "cancelled", job
        partial = job["outputs"]["count"]
        assert 1 <= partial < 3

        resumed = client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frames": [1, 3], "resume": True})
        assert resumed.status_code == 200, resumed.text
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done", job
        assert job["outputs"]["count"] == 3


def test_failures_are_explained(tmp):
    client, settings = make_client(tmp)
    with client:
        oom = upload(client, "oom.blend", blend(marker=b"OOM")).json()["id"]
        wait_status(client, oom, {"ready"})
        client.post(f"/api/jobs/{oom}/start", json={})
        job = wait_status(client, oom, {"failed", "done"})
        assert job["status"] == "failed"
        assert "памяти" in job["error"]
        assert job["error_log"]

        boom = upload(client, "boom.blend", blend(marker=b"FAIL")).json()["id"]
        wait_status(client, boom, {"ready"})
        client.post(f"/api/jobs/{boom}/start", json={})
        job = wait_status(client, boom, {"failed", "done"})
        assert job["status"] == "failed"
        assert "кодом 1" in job["error"]


def test_queue_runs_one_at_a_time(tmp):
    client, settings = make_client(tmp)
    with client:
        ids = []
        for name in ("a.blend", "b.blend"):
            job_id = upload(client, name, blend(marker=b"SLOW")).json()["id"]
            wait_status(client, job_id, {"ready"})
            ids.append(job_id)
        for job_id in ids:
            client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frames": [1, 2]})
        second = client.get(f"/api/jobs/{ids[1]}").json()
        assert second["status"] == "queued"
        assert second["queue_position"] == 1
        wait_until(lambda: client.get(f"/api/jobs/{ids[0]}").json()["status"] == "rendering")
        assert client.get(f"/api/jobs/{ids[1]}").json()["status"] == "queued"
        first = wait_status(client, ids[0], {"done", "failed"})
        second = wait_status(client, ids[1], {"done", "failed"})
        assert first["status"] == "done" and second["status"] == "done"
        assert first["finished"] <= second["started"] + 0.5


def test_cancel_queued_job(tmp):
    client, settings = make_client(tmp)
    with client:
        ids = []
        for name in ("a.blend", "b.blend"):
            job_id = upload(client, name, blend(marker=b"SLOW")).json()["id"]
            wait_status(client, job_id, {"ready"})
            ids.append(job_id)
        for job_id in ids:
            client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "frames": [1, 2]})
        assert client.post(f"/api/jobs/{ids[1]}/cancel").json()["status"] == "cancelled"
        client.post(f"/api/jobs/{ids[0]}/cancel")
        wait_status(client, ids[0], {"cancelled", "done"})
        assert client.delete(f"/api/jobs/{ids[1]}").status_code == 200
        assert client.get(f"/api/jobs/{ids[1]}").status_code == 404
        assert not (settings.jobs_dir / ids[1]).exists()


def test_version_resolution():
    installed = [
        {"version": (3, 6, 5), "path": "/b/3.6.5"},
        {"version": (4, 2, 1), "path": "/b/4.2.1"},
        {"version": (4, 5, 12), "path": "/b/4.5.12"},
    ]
    chosen, warnings = blenders.resolve(installed, (4, 5))
    assert chosen["version"] == "4.5.12" and warnings == []
    chosen, warnings = blenders.resolve(installed, (3, 6))
    assert chosen["version"] == "3.6.5" and warnings == []
    chosen, warnings = blenders.resolve(installed, (4, 0))
    assert chosen["version"] == "4.2.1" and len(warnings) == 1
    chosen, warnings = blenders.resolve(installed, (5, 0))
    assert chosen["version"] == "4.5.12" and len(warnings) == 2
    chosen, warnings = blenders.resolve(installed, (4, 5), "3.6")
    assert chosen["version"] == "3.6.5" and len(warnings) == 1
    try:
        blenders.resolve([], (4, 5))
        raise AssertionError("ожидалась ошибка")
    except LookupError:
        pass


def test_cleanup_retention(tmp):
    client, settings = make_client(tmp, project_days=3, result_days=30)
    with client:
        job_id = upload(client, "c.blend", blend(marker=b"STILL")).json()["id"]
        wait_status(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/start", json={})
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done"
        service = client.app.state.service
        paths = service.paths(job_id)

        service.db.update(job_id, finished=time.time() - 1 * 86400)
        assert cleanup.run_cleanup(service) == ([], [])
        assert paths.project.exists()

        service.db.update(job_id, finished=time.time() - 4 * 86400)
        removed, trimmed = cleanup.run_cleanup(service)
        assert trimmed == [job_id] and removed == []
        assert not paths.project.exists() and paths.renders.exists()
        assert client.get(f"/api/jobs/{job_id}").json()["project_deleted"] is True
        assert client.post(f"/api/jobs/{job_id}/start", json={}).status_code == 410
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 200

        service.db.update(job_id, finished=time.time() - 31 * 86400)
        removed, trimmed = cleanup.run_cleanup(service)
        assert removed == [job_id]
        assert not paths.root.exists()
        assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_recover_after_restart(tmp):
    settings = make_settings(tmp)
    service = Service(settings)
    try:
        job = {
            "id": "abc123abc123", "name": "x", "filename": "x.blend", "status": "rendering",
            "created": time.time(), "updated": time.time(), "candidates": ["x.blend"], "blend": "x.blend",
            "info": {}, "options": {"mode": "animation", "device": "OPTIX", "resume": False},
            "queued_at": 5.0, "attempts": 1,
        }
        service.db.insert(job)
        service.recover()
        restored = service.db.get("abc123abc123")
        assert restored["status"] == "queued"
        assert restored["options"]["resume"] is True
        assert restored["queued_at"] == 5.0
    finally:
        service.db.close()


def test_index_and_blenders(tmp):
    client, settings = make_client(tmp)
    with client:
        page = client.get("/")
        assert page.status_code == 200 and "Рендер Blender" in page.text
        guide = client.get("/guide")
        assert guide.status_code == 200 and "Как подготовить проект" in guide.text
        assert [item["version"] for item in client.get("/api/blenders").json()] == ["3.6.5", "4.5.12"]


def test_version_menu_download_and_remove(tmp):
    release = FakeRelease(tmp, ["4.2.4", "4.2.5", "5.0.1"])
    try:
        client, settings = make_client(tmp, download_base=release.base, auto_install=False)
        with client:
            data = client.get("/api/blenders/catalog").json()
            assert data["error"] is None
            rows = {row["series"]: row for row in data["catalog"]}
            assert rows["4.2"]["latest"] == "4.2.5" and rows["4.2"]["versions"] == ["4.2.5", "4.2.4"]
            assert rows["5.0"]["latest"] == "5.0.1"
            assert [item["version"] for item in data["installed"]] == ["3.6.5", "4.5.12"]

            started = client.post("/api/blenders/download", json={"version": "4.2"})
            assert started.status_code == 202, started.text
            assert started.json()["version"] == "4.2.5"
            wait_until(lambda: "4.2.5" in installed_versions(client))
            target = settings.blender_root / "blender-4.2.5-linux-x64" / "blender"
            assert target.is_file() and os.access(target, os.X_OK)
            wait_until(lambda: not list(settings.blender_root.glob(".extract-*")))
            wait_until(lambda: not client.get("/api/blenders/catalog").json()["downloads"])

            again = client.post("/api/blenders/download", json={"version": "4.2"})
            assert again.status_code == 202 and again.json()["state"] == "done"
            assert client.post("/api/blenders/download", json={"version": "9.9"}).status_code == 502
            assert client.post("/api/blenders/download", json={"version": "abc"}).status_code == 400

            assert client.delete("/api/blenders/4.2.5").status_code == 200
            assert not (settings.blender_root / "blender-4.2.5-linux-x64").exists()
            assert client.delete("/api/blenders/4.2.5").status_code == 404
    finally:
        release.close()


def test_download_rejects_wrong_checksum(tmp):
    release = FakeRelease(tmp, ["4.2.5"], wrong_hash=True)
    try:
        client, settings = make_client(tmp, download_base=release.base, auto_install=False)
        with client:
            assert client.post("/api/blenders/download", json={"version": "4.2.5"}).status_code == 202
            failed = wait_until(
                lambda: next(
                    (d for d in client.get("/api/blenders/catalog").json()["downloads"] if d["state"] == "failed"),
                    None,
                )
            )
            assert "Контрольная сумма" in failed["error"]
            assert not (settings.blender_root / "blender-4.2.5-linux-x64").exists()
            wait_until(lambda: not list((settings.blender_root / ".downloads").glob("*")))
            wait_until(lambda: not list(settings.blender_root.glob(".extract-*")))
    finally:
        release.close()


def test_download_without_checksum_file(tmp):
    release = FakeRelease(tmp, ["4.2.5"], with_hash=False)
    try:
        client, settings = make_client(tmp, download_base=release.base, auto_install=False)
        with client:
            assert client.post("/api/blenders/download", json={"version": "4.2.5"}).status_code == 202
            wait_until(lambda: "4.2.5" in installed_versions(client))
    finally:
        release.close()


def test_auto_install_during_preflight(tmp):
    release = FakeRelease(tmp, ["4.5.12"])
    try:
        client, settings = make_client(tmp, versions=(), download_base=release.base, auto_install=True)
        with client:
            assert installed_versions(client) == []
            job_id = upload(client, "a.blend", blend(marker=b"STILL")).json()["id"]
            job = wait_status(client, job_id, {"ready", "failed"})
            assert job["status"] == "ready", job
            assert job["blender"]["version"] == "4.5.12"
            assert job["warnings"] == []
            assert job["install"] is None
            assert installed_versions(client) == ["4.5.12"]
            started = client.post(f"/api/jobs/{job_id}/start", json={})
            assert started.status_code == 200, started.text
            assert wait_status(client, job_id, {"done", "failed"})["status"] == "done"
    finally:
        release.close()


def test_auto_install_failure_falls_back_to_installed(tmp):
    client, settings = make_client(
        tmp, versions=("4.5.12",), download_base="http://127.0.0.1:9/release/", auto_install=True
    )
    with client:
        job_id = upload(client, "old.blend", blend("BLENDER-v306", b"STILL")).json()["id"]
        job = wait_status(client, job_id, {"ready", "failed"}, timeout=60)
        assert job["status"] == "ready", job
        assert job["blender"]["version"] == "4.5.12"
        assert any("Не удалось скачать" in text for text in job["warnings"])
        assert any("не установлен" in text for text in job["warnings"])


def test_no_blender_anywhere_names_the_folder(tmp):
    client, settings = make_client(
        tmp, versions=(), download_base="http://127.0.0.1:9/release/", auto_install=True
    )
    with client:
        job_id = upload(client, "a.blend", blend(marker=b"STILL")).json()["id"]
        job = wait_status(client, job_id, {"failed", "ready"}, timeout=60)
        assert job["status"] == "failed"
        assert str(settings.blender_root) in job["error"]
        assert "Версии Blender" in job["error"]


def test_auto_install_disabled_does_not_download(tmp):
    client, settings = make_client(tmp, versions=("4.5.12",), auto_install=False)
    with client:
        job_id = upload(client, "old.blend", blend("BLENDER-v306", b"STILL")).json()["id"]
        job = wait_status(client, job_id, {"ready", "failed"})
        assert job["status"] == "ready" and job["blender"]["version"] == "4.5.12"
        assert job["warnings"] and not any("скачать" in text for text in job["warnings"])
        assert client.app.state.service.installer.statuses() == []


def test_catalog_offline_reports_error(tmp):
    client, settings = make_client(tmp, download_base="http://127.0.0.1:9/release/", auto_install=False)
    with client:
        response = client.get("/api/blenders/catalog")
        assert response.status_code == 200
        data = response.json()
        assert data["error"] and data["catalog"] == []
        assert len(data["installed"]) == 2


def test_explicit_blender_override(tmp):
    client, settings = make_client(tmp, auto_install=False)
    with client:
        job_id = upload(client, "o.blend", blend(marker=b"STILL")).json()["id"]
        wait_status(client, job_id, {"ready"})
        assert client.post(f"/api/jobs/{job_id}/start", json={"blender": "4.0"}).status_code == 409
        accepted = client.post(f"/api/jobs/{job_id}/start", json={"blender": "3.6"})
        assert accepted.status_code == 200, accepted.text
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done" and job["blender"]["version"] == "3.6.5"


def test_discover_root_and_unnamed_folders(tmp):
    root = tmp / "root"

    def fake(folder, text):
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "blender"
        script.write_text(f"#!/bin/sh\necho 'Blender {text}'\n")
        script.chmod(0o755)

    fake(root, "4.5.3")
    fake(root / "custom", "3.6.9")
    fake(root / "blender-4.2.1-linux-x64", "0.0.0")
    fake(root / ".hidden", "2.0.0")
    assert [item["version"] for item in blenders.discover(root)] == [(3, 6, 9), (4, 2, 1), (4, 5, 3)]


def main():
    tests =[(name, func) for name, func in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, func in tests:
        folder = Path(tempfile.mkdtemp(prefix="render-test-"))
        began = time.time()
        try:
            if func.__code__.co_argcount:
                func(folder)
            else:
                func()
            print(f"OK    {name} ({time.time() - began:.1f}s)")
        except Exception:
            failed += 1
            import traceback
            print(f"FAIL  {name}")
            traceback.print_exc()
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


PREFS = (
    b"BLENDER-pref\nADDON:cycles\nADDON:node_wrangler\nADDON:my_addon\n"
    b"ADDON:bl_ext.blender_org.good_ext\nADDON:bl_ext.blender_org.nonexistent_x\n"
)


def prefs_upload(client, content=PREFS, name="userpref.blend"):
    return client.post("/api/setup/prefs", files={"file": (name, content, "application/octet-stream")})


def addon_upload(client, name, content):
    return client.post("/api/setup/addons", files={"file": (name, content, "application/octet-stream")})


def addons_of(client):
    return {item["module"]: item for item in client.get("/api/setup").json()["addons"]}


def test_setup_scan_and_auto_install(tmp):
    client, settings = make_client(tmp)
    with client:
        first = client.get("/api/setup").json()
        assert first["needs_setup"] is True and first["addons"] == [] and first["has_blender"] is True
        state = prefs_upload(client).json()
        found = {item["module"]: item for item in state["addons"]}
        assert found["cycles"]["origin"] == "bundled" and found["cycles"]["status"] == "ok"
        assert found["my_addon"]["status"] == "missing"
        assert state["prefs"]["name"] == "userpref.blend"
        wait_until(lambda: addons_of(client)["bl_ext.blender_org.good_ext"]["status"] == "ok")
        wait_until(lambda: addons_of(client)["bl_ext.blender_org.nonexistent_x"]["status"] == "failed")
        final = addons_of(client)
        assert final["bl_ext.blender_org.good_ext"]["origin"] == "extension"
        assert "not found" in final["bl_ext.blender_org.nonexistent_x"]["message"]
        resolved = addon_upload(client, "my_addon.zip", zip_bytes({"my_addon/__init__.py": "x = 1"})).json()
        mine = {item["module"]: item for item in resolved["addons"]}["my_addon"]
        assert mine["origin"] == "user" and mine["status"] == "ok" and mine["enabled"] is True


def test_addons_reach_blender(tmp):
    client, settings = make_client(tmp)
    with client:
        prefs_upload(client)
        wait_until(lambda: addons_of(client)["bl_ext.blender_org.good_ext"]["status"] == "ok")
        addon_upload(client, "my_addon.py", b"x = 1")
        job_id = upload(client, "a.blend", blend(marker=b"STILL")).json()["id"]
        wait_status(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/start", json={"mode": "still", "device": "CPU"})
        job = wait_status(client, job_id, {"done", "failed"})
        assert job["status"] == "done"
        assert "my_addon" in job["addons"] and "bl_ext.blender_org.good_ext" in job["addons"]
        assert "bl_ext.blender_org.nonexistent_x" not in job["addons"]
        log = client.get(f"/api/jobs/{job_id}/log").text
        assert "addons=" in log and "blender-user" in log and "good_ext" in log
        client.post("/api/setup/addons/my_addon/enabled", json={"enabled": False})
        client.post(f"/api/jobs/{job_id}/start", json={"mode": "still", "device": "CPU"})
        wait_status(client, job_id, {"done", "failed"})
        assert "disabled=cycles" not in client.get(f"/api/jobs/{job_id}/log").text
        assert "my_addon" in client.get(f"/api/jobs/{job_id}/log").text.split("disabled=")[1].split("\n")[0]
        old = upload(client, "old.blend", blend(header="BLENDER-v306", marker=b"STILL")).json()["id"]
        wait_status(client, old, {"ready"})
        client.post(f"/api/jobs/{old}/start", json={"mode": "still", "device": "CPU"})
        finished = wait_status(client, old, {"done", "failed"})
        assert finished["status"] == "done"
        assert all(not name.startswith("bl_ext.") for name in finished["addons"])


def test_failed_addon_becomes_warning(tmp):
    client, settings = make_client(tmp)
    with client:
        prefs_upload(client, b"BLENDER-pref\n")
        addon_upload(client, "broken_one.py", b"raise Exception")
        job_id = upload(client, "w.blend", blend(marker=b"STILL")).json()["id"]
        job = wait_status(client, job_id, {"ready"})
        assert any("broken_one" in text for text in job["warnings"])


def test_prefs_zip_with_addons_and_validation(tmp):
    client, settings = make_client(tmp)
    with client:
        archive = zip_bytes({
            "Blender/4.5/config/userpref.blend": b"BLENDER-pref\nADDON:zipped_addon\nADDON:bl_ext.user_default.ext_one\n",
            "Blender/4.5/scripts/addons/zipped_addon.py": "x = 1",
            "Blender/4.5/extensions/user_default/ext_one/__init__.py": "",
            "Blender/4.5/extensions/user_default/ext_one/blender_manifest.toml": 'id = "ext_one"\n',
        })
        state = prefs_upload(client, archive, "config.zip").json()
        found = {item["module"]: item for item in state["addons"]}
        assert found["zipped_addon"]["origin"] == "user" and found["zipped_addon"]["status"] == "ok"
        assert found["bl_ext.user_default.ext_one"]["origin"] == "extension"
        assert prefs_upload(client, zip_bytes({"a/b.txt": "x"}), "bad.zip").status_code == 400
        assert prefs_upload(client, zip_bytes({"../evil/userpref.blend": b"BLENDER"}), "slip.zip").status_code == 400
        assert prefs_upload(client, b"plain text", "userpref.blend").status_code == 400
        assert prefs_upload(client, PREFS, "notes.txt").status_code == 400
        assert addon_upload(client, "bad-name.py", b"x").status_code == 400
        assert addon_upload(client, "empty.zip", zip_bytes({"readme.txt": "x"})).status_code == 400
        assert addon_upload(client, "a.txt", b"x").status_code == 400
        manifest = zip_bytes({"pack/blender_manifest.toml": 'id = "pack_ext"\n', "pack/__init__.py": ""})
        added = {item["module"] for item in addon_upload(client, "pack.zip", manifest).json()["addons"]}
        assert "bl_ext.user_default.pack_ext" in added


def test_setup_flags_enable_and_remove(tmp):
    client, settings = make_client(tmp)
    with client:
        prefs_upload(client)
        wait_until(lambda: not client.get("/api/setup").json()["busy"])
        addon_upload(client, "mine.py", b"x = 1")
        assert client.post("/api/setup/addons/nothing/enabled", json={"enabled": True}).status_code == 404
        assert client.delete("/api/setup/addons/cycles").status_code == 400
        removed = client.delete("/api/setup/addons/mine").json()
        assert "mine" not in {item["module"] for item in removed["addons"]}
        assert not (settings.user_dir / "scripts" / "addons" / "mine.py").exists()
        client.delete("/api/setup/addons/my_addon")
        assert "my_addon" not in addons_of(client)
        assert "my_addon" not in {item["module"] for item in client.post("/api/setup/rescan").json()["addons"]}
        assert client.post("/api/setup/done", json={"skipped": True}).json()["needs_setup"] is False
        done = client.post("/api/setup/done", json={}).json()
        assert done["configured"] is True and done["skipped"] is False
        reset = client.delete("/api/setup/prefs").json()
        assert reset["prefs"] is None


def test_setup_needs_blender(tmp):
    client, settings = make_client(tmp, versions=())
    with client:
        assert client.get("/api/setup").json()["has_blender"] is False
        response = prefs_upload(client)
        assert response.status_code == 409 and "Blender" in response.json()["detail"]


def test_token_auth(tmp):
    from renderservice import auth
    auth.FAIL_DELAY = 0
    auth.failures.clear()
    try:
        client, settings = make_client(tmp, token="secret-token")
        with client:
            assert client.get("/api/jobs").status_code == 401
            assert client.get("/api/status").status_code == 401
            redirect = client.get("/", follow_redirects=False)
            assert redirect.status_code == 303 and redirect.headers["location"] == "/login"
            assert client.get("/login").status_code == 200
            assert client.get("/static/css/theme.css").status_code == 200
            assert client.post("/api/jobs", files={"file": ("a.blend", blend(), "application/octet-stream")}).status_code == 401
            assert client.post("/api/login", json={"token": "nope"}).status_code == 401
            assert client.get("/api/jobs").status_code == 401
            bearer = client.get("/api/jobs", headers={"Authorization": "Bearer secret-token"})
            assert bearer.status_code == 200
            ok = client.post("/api/login", json={"token": "secret-token"})
            assert ok.status_code == 200 and "render_session" in ok.headers["set-cookie"] and "HttpOnly" in ok.headers["set-cookie"]
            assert client.get("/api/jobs").status_code == 200
            assert client.get("/").status_code == 200
            assert client.get("/api/status").json()["auth"] is True
            client.get("/logout", follow_redirects=False)
            assert client.get("/api/jobs").status_code == 401
            for _ in range(auth.FAIL_LIMIT):
                client.post("/api/login", json={"token": "bad"})
            assert client.post("/api/login", json={"token": "secret-token"}).status_code == 429
    finally:
        auth.FAIL_DELAY = 1.0
        auth.failures.clear()


def test_no_token_means_open(tmp):
    client, settings = make_client(tmp)
    with client:
        assert client.get("/api/jobs").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 303
        assert client.get("/api/status").json()["auth"] is False


def test_rerender_after_done(tmp):
    client, settings = make_client(tmp)
    with client:
        job_id = upload(client, "r.blend", blend(marker=b"STILL")).json()["id"]
        wait_status(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/start", json={"mode": "still", "device": "CPU"})
        first = wait_status(client, job_id, {"done"})
        again = client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation", "device": "CPU", "frames": [1, 2]})
        assert again.status_code == 200 and again.json()["status"] == "queued"
        second = wait_status(client, job_id, {"done"})
        assert second["progress"]["total_frames"] == 2 and second["outputs"]["count"] == 2


def test_status_endpoint(tmp):
    client, settings = make_client(tmp, auto_install=False)
    with client:
        idle = client.get("/api/status").json()
        assert idle["rendering"] is None and idle["queued"] == 0 and idle["jobs"] == 0
        assert idle["disk_free"] > 0
        job_id = upload(client, "s.blend", blend(marker=b"SLOW")).json()["id"]
        wait_status(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/start", json={"mode": "animation"})
        wait_status(client, job_id, {"rendering"})
        busy = client.get("/api/status").json()
        assert busy["rendering"] == "s" and busy["jobs"] == 1
        client.post(f"/api/jobs/{job_id}/cancel")
        wait_status(client, job_id, {"cancelled", "failed", "done"})


def test_static_assets_served(tmp):
    client, settings = make_client(tmp, auto_install=False)
    with client:
        for path in ["/", "/guide", "/static/js/app.js", "/static/js/glyphs.js", "/static/css/theme.css", "/static/css/fonts.css",
                     "/static/fonts/tektur-cyrillic-900-normal.woff2", "/static/fonts/martian-mono-latin-400-normal.woff2"]:
            assert client.get(path).status_code == 200, path


if __name__ == "__main__":
    sys.exit(main())
