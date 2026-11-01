"""ShopPilot 服务装配：FastAPI 实例、lifespan、路由挂载、静态托管。

接口实现按消费者拆在 app/api/ 下；这里只做装配。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import admin, customer
from app.api._graph import get_graph
from app.llm import ConfigError, get_llm, get_llm_quiet
from app import store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    try:
        get_llm()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise
    # checkpoint 连接建在事件循环内（aiomysql 绑定循环，不能事后建）；
    # 图在这里 compile 一次，整个进程复用
    await get_graph(get_llm_quiet)
    yield


app = FastAPI(title="ShopPilot", lifespan=lifespan)
app.include_router(customer.router)
app.include_router(admin.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
