"""ShopPilot 服务装配：FastAPI 实例、lifespan、路由挂载、静态托管。

接口实现按消费者拆在 app/api/ 下；这里只做装配。
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import admin, customer, eval_cases, feedback, finetune, flywheel
from app.api._graph import get_graph
from app.core import store
from app.core.llm import ConfigError, get_llm, get_llm_quiet
from app.observability import langfuse
from app.tools import mcp

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
    # MCP 会话常开：必须在 compile 图之前注册，否则工具绑不进 agent。
    # 加载失败只记 warning，不影响服务启动。
    stack = AsyncExitStack()
    if mcp.enabled():
        try:
            specs = await mcp.load_servers(stack)
            logger.info("MCP 加载完成，注册 %d 个工具", len(specs))
        except Exception:  # noqa: BLE001 - 外部工具故障不能拖垮主服务
            logger.warning("MCP 加载异常，已跳过", exc_info=True)

    # checkpoint 连接建在事件循环内（aiomysql 绑定循环，不能事后建）；
    # 图在这里 compile 一次，整个进程复用
    await get_graph(get_llm_quiet)
    try:
        yield
    finally:
        await stack.aclose()
        # 退出前把 Langfuse 缓冲推走；未启用或失败都不影响进程退出
        langfuse.flush()


app = FastAPI(title="ShopPilot", lifespan=lifespan)
app.include_router(customer.router)
app.include_router(feedback.router)
app.include_router(eval_cases.router)
app.include_router(flywheel.router)
app.include_router(admin.router)
app.include_router(finetune.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")