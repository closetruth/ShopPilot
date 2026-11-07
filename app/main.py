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


class NoCacheStaticFiles(StaticFiles):
    """静态资源一律要求回源校验。

    默认的 StaticFiles 只给 ETag/Last-Modified，浏览器会按「距上次修改
    时间的 10%」自行估算新鲜期。脚本改坏了再改好，估算出来的新鲜期可能
    还有几小时，普通刷新根本不会回源——页面就一直卡在旧的那份坏脚本上，
    看起来像「功能没写」。加 no-cache 后每次都用 ETag 校验，内容没变则
    回 304，代价只有一个请求往返。
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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


app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")