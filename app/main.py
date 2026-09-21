from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes_chat import router as chat_router
from app.api.routes_extract import router as extract_router
from app.api.routes_health import router as health_router
from app.api.routes_sessions import router as sessions_router
from app.db.base import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="MewHelp", lifespan=lifespan)
app.include_router(health_router)
app.include_router(chat_router)
app.include_router(extract_router)
app.include_router(sessions_router)
app.mount("/", StaticFiles(directory="static", html=True, check_dir=False), name="static")
