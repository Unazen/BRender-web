import uvicorn

from renderservice.config import load_settings

if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        "renderservice.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
