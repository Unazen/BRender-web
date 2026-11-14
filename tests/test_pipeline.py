import io
import shutil
import sys
import tempfile
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


def make_settings(tmp, **extra):
    root = tmp / "blenders"
    for version in ("3.6.5", "4.5.12"):
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
        assert page.status_code == 200 and "BLENDER RENDER" in page.text
        assert [item["version"] for item in client.get("/api/blenders").json()] == ["3.6.5", "4.5.12"]


def main():
    tests = [(name, func) for name, func in sorted(globals().items()) if name.startswith("test_")]
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


if __name__ == "__main__":
    sys.exit(main())
