import uvicorn

from renderservice.config import load_settings

if __name__ == "__main__":
    settings = load_settings()
    if not settings.token and settings.host not in ("127.0.0.1", "localhost", "::1"):
        print("Внимание: RENDER_TOKEN не задан, сайт открыт всем устройствам в сети. Задайте токен в файле .env.")
    uvicorn.run(
        "renderservice.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
