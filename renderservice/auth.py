import asyncio
import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

COOKIE = "render_session"
COOKIE_DAYS = 30
FAIL_DELAY = 1.0
FAIL_LIMIT = 8
FAIL_WINDOW = 60
OPEN_PATHS = ("/login", "/api/login")
OPEN_PREFIXES = ("/static/",)

failures = {}


class LoginBody(BaseModel):
    token: str = ""


def digest(token):
    return hashlib.sha256(("render-service:" + token).encode("utf-8")).hexdigest()


def authorized(request, token):
    expected = digest(token)
    cookie = request.cookies.get(COOKIE, "")
    if cookie and hmac.compare_digest(cookie, expected):
        return True
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and hmac.compare_digest(digest(header[7:].strip()), expected):
        return True
    return False


def client_key(request):
    return request.client.host if request.client else "unknown"


def locked(request):
    now = time.time()
    recent = [stamp for stamp in failures.get(client_key(request), []) if now - stamp < FAIL_WINDOW]
    failures[client_key(request)] = recent
    return len(recent) >= FAIL_LIMIT


async def register_failure(request):
    failures.setdefault(client_key(request), []).append(time.time())
    await asyncio.sleep(FAIL_DELAY)


def attach(app, settings):
    @app.middleware("http")
    async def guard(request, call_next):
        path = request.url.path
        if settings.token and not (path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)):
            if not authorized(request, settings.token):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Нужен вход"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        if request.method == "GET" and not path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/login")
    def login_page():
        if not settings.token:
            return RedirectResponse("/", status_code=303)
        return FileResponse(settings.static_dir / "login.html")

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        if not settings.token:
            return {"ok": True}
        if locked(request):
            return JSONResponse({"detail": "Слишком много попыток, подождите минуту"}, status_code=429)
        if not hmac.compare_digest(digest(body.token.strip()), digest(settings.token)):
            await register_failure(request)
            return JSONResponse({"detail": "Токен неверный"}, status_code=401)
        failures.pop(client_key(request), None)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE,
            digest(settings.token),
            max_age=COOKIE_DAYS * 86400,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/logout")
    def logout():
        response = RedirectResponse("/login" if settings.token else "/", status_code=303)
        response.delete_cookie(COOKIE)
        return response
