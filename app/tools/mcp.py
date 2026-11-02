"""MCP 客户端：连服务器、拉工具、转成同一注册中心里的 ToolSpec。

会话用 AsyncExitStack 常开，避免每次调用重开子进程。任何服务器出错
只记 warning 并跳过，绝不让 MCP 拖垮服务启动。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel, ConfigDict, create_model

from app.tools.registry import ToolSpec, registry

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "mcp_servers.json"

ENABLED_ENV = "MCP_ENABLED"
CONFIG_ENV = "MCP_CONFIG"

LABEL_MAX = 12

_JSON_TO_PY: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "0").strip() not in ("", "0", "false", "False")


def config_path() -> Path:
    raw = os.getenv(CONFIG_ENV)
    return Path(raw) if raw else DEFAULT_CONFIG


def load_config(path: Path | None = None) -> dict:
    """读 mcp_servers.json。文件缺失或格式错时返回空配置，不抛异常。"""
    target = path or config_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("MCP 配置不存在：%s", target)
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("MCP 配置读取失败：%s", target, exc_info=True)
        return {}


def _connection(server: str, cfg: dict) -> dict:
    """把配置翻成 MultiServerMCPClient 的连接参数。"""
    command = cfg.get("command", "")
    if command in ("python", "python3"):
        command = sys.executable
    conn = {
        "transport": cfg.get("transport", "stdio"),
        "command": command,
        "args": list(cfg.get("args", [])),
        "env": cfg.get("env") or None,
        "cwd": str(ROOT),
    }
    if cfg.get("url"):
        conn["url"] = cfg["url"]
    logger.debug("MCP server %s 连接参数：%s", server, conn)
    return conn


def _field_type(schema: dict) -> Any:
    if "anyOf" in schema:
        variants = [s for s in schema["anyOf"] if s.get("type") != "null"]
        nullable = len(variants) != len(schema["anyOf"])
        base = _field_type(variants[0]) if len(variants) == 1 else Any
        return base | None if nullable else base
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"]
        base = _JSON_TO_PY.get(kinds[0], Any) if len(kinds) == 1 else Any
        return base | None if "null" in kind else base
    return _JSON_TO_PY.get(kind, Any)


def args_model(name: str, schema: dict) -> type[BaseModel]:
    """把 MCP 工具的 JSON schema 转成 pydantic 模型，交给执行引擎校验。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, prop in props.items():
        if key in required:
            fields[key] = (_field_type(prop), ...)
        else:
            fields[key] = (_field_type(prop) | None, prop.get("default"))
    model_name = "".join(part.capitalize() for part in name.replace("__", "_").split("_"))
    return create_model(
        f"{model_name}Args",
        __config__=ConfigDict(extra="ignore"),
        **fields,
    )


def _text_of(raw: Any) -> str:
    """MCP 返回的是内容块列表，取出纯文本。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(
            block.get("text", "") for block in raw if isinstance(block, dict)
        )
    return str(raw)


def normalize_result(raw: Any) -> dict:
    """尽量把 MCP 的文本结果解析成 dict，模型读起来与内置工具一致。"""
    text = _text_of(raw)
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"result": text}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _impl(tool: BaseTool):
    async def call(runtime, **kwargs):
        return normalize_result(await tool.ainvoke(kwargs))

    return call


def to_spec(server: str, tool: BaseTool, cfg: dict) -> ToolSpec:
    description = (tool.description or tool.name).strip()
    schema = tool.args_schema if isinstance(tool.args_schema, dict) else {}
    return ToolSpec(
        name=f"{server}__{tool.name}",
        label=description[:LABEL_MAX] or tool.name,
        description=description,
        args_schema=args_model(f"{server}__{tool.name}", schema),
        impl=_impl(tool),
        scope=cfg.get("scope", "none"),
        risk=cfg.get("risk", "read"),
        requires_confirmation=bool(cfg.get("requires_confirmation", False)),
        allowed_callers=tuple(cfg.get("allowed_callers", ["agent"])),
        source="mcp",
        server=server,
    )


async def _load_server(
    stack: AsyncExitStack, client: MultiServerMCPClient, server: str, cfg: dict
) -> list[ToolSpec]:
    session = await stack.enter_async_context(client.session(server))
    tools = await load_mcp_tools(session, server_name=server)
    specs = [to_spec(server, tool, cfg) for tool in tools]
    for spec in specs:
        registry.register(spec, override=True)
    return specs


async def load_servers(
    stack: AsyncExitStack, config: dict | None = None
) -> list[ToolSpec]:
    """按配置拉起全部启用的服务器并注册工具。单台失败只跳过该台。"""
    cfg = config if config is not None else load_config()
    servers = cfg.get("servers") or {}
    if not servers:
        return []

    connections = {
        name: _connection(name, server_cfg)
        for name, server_cfg in servers.items()
        if server_cfg.get("enabled", True)
    }
    if not connections:
        logger.info("MCP 没有启用的服务器")
        return []

    client = MultiServerMCPClient(connections)
    loaded: list[ToolSpec] = []
    for name in connections:
        try:
            specs = await _load_server(stack, client, name, servers[name])
        except Exception:  # noqa: BLE001 - MCP 故障不拖垮启动
            logger.warning("MCP server %s 加载失败，已跳过", name, exc_info=True)
            continue
        logger.info("MCP server %s 注册 %d 个工具", name, len(specs))
        loaded.extend(specs)
    return loaded