from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from webapp.routes.main import router as main_router
from webapp.routes.mastercard import router as mastercard_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Vidra Web")

app.add_middleware(
    SessionMiddleware,
    secret_key="vidra-web-secret-key-change-later",
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(main_router)
app.include_router(mastercard_router)