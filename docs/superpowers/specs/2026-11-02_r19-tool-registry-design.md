# r19 设计：工具注册中心、权限、执行引擎、审计与 MCP 接入

## 1. 问题

r15 起业务 agent 有了四个工具，但它们只是 `app/tools.py` 里一个硬编码的
`TOOLS = [...]` 列表。工具数量再涨一个量级时，这套结构会同时暴露四个问题：

1. **没有元数据**。工具是什么来源、读还是写、要不要顾客确认、谁能调用，
   全靠人读代码判断，程序无法据此做任何决策。
2. **参数校验散落在每个工具体内**。四个工具各写一遍
   `try: ProductQuery(...) except ValidationError: return _validation_error(...)`，
   新增工具必须记得复制这段，漏了就变成裸异常。
3. **权限只有数据隔离**。`ToolRuntime[AgentContext]` 注入 `visitor_id` 保证了
   「只能查自己的订单」，但没有任何机制阻止一个写工具被模型直接执行。
4. **没有工具级审计**。`agent_traces` 只记到轮级（调了哪些工具名），
   没有每次调用的入参、耗时、结果、错误，出问题只能靠猜。
5. **无法接入外部工具**。MCP 生态里的工具（物流、发票、ERP）完全接不进来。

本轮的产物是一个**单一入口**：模型能看到的所有工具都是注册中心发出的包装
工具，每次调用必然经过「校验 → 鉴权 → 执行或挂起 → 审计」四步。

## 2. 目标与非目标

**目标**

- 装饰器 + 进程内注册表的工具注册中心，承载元数据（来源、scope、风险、
  是否需确认、允许的调用者、超时）
- 统一的参数校验，工具体内不再重复写 `ValidationError` 处理
- 三层权限：数据范围（scope）+ 风险等级（risk）+ 调用者白名单
- 执行引擎：唯一入口，负责校验、鉴权、超时、异常归一、审计
- 工具级审计表，可查、可分析
- 「待确认动作」通用机制，建工单迁移为它的第一个实例
- MCP 客户端接入，外部工具进同一个注册中心、走同一条执行链
- 管理页可看注册清单与审计流水

**非目标**

- 工具市场 / 插件热插拔 / 管理后台增删工具
- 把 ShopPilot 自身工具暴露成 MCP server（只做客户端）
- 工具级限流、熔断、重试策略
- 完整 RBAC 角色表与管理页
- 审计日志归档与保留策略（只建索引 + 分页查询）

## 3. 现状与缺口

| 关注点 | 现状 | 本轮 |
|---|---|---|
| 注册 | [tools.py](file:///c:/Users/Adventure/VscodeSpace/ShopPilot/app/tools.py) 末尾硬编码 `TOOLS` 列表 | `@registered` 装饰器 + 注册表 |
| 参数校验 | 每个工具体内各写一遍 pydantic 校验 | 执行引擎统一校验，工具收归一化参数 |
| 权限 | 仅 `visitor_id` 数据隔离 | scope + risk + allowed_callers 三层 |
| 执行 | `create_react_agent` 直接调工具 | 执行引擎包装，唯一入口 |
| 审计 | 仅 `agent_traces` 轮级轨迹 | `tool_audit_logs` 每次调用一行 |
| 确认 | 工具内部建草稿，SSE 层反解析工具输出 | 注册表标记 + 通用待确认动作 |
| MCP | 无 | MCP 客户端，启动时注册远端工具 |

## 4. 总体结构

`app/tools.py` 拆成包 `app/tools/`（`__init__.py` 继续导出 `AgentContext` 与
`agent_tools()`，现有 import 只改一处函数名）：

```
app/tools/
  __init__.py     导出 AgentContext、agent_tools()、registry
  registry.py     ToolSpec 数据模型 + 注册表 + 包装工具构造
  permission.py   Caller / authorize / Denied
  executor.py     Executor：invoke / confirm / cancel
  builtin.py      四个内置工具的注册（从 tools.py 迁来）
  mcp.py          MCP 客户端：连服务器、拉工具、转 ToolSpec
app/repo/
  audit.py        tool_audit_logs 读写
  pending.py      pending_actions 读写
```

调用链（模型视角只看到最后一步的包装工具）：

```
create_react_agent
  └─ 包装工具（registry 生成，模型可见 schema = ToolSpec.args_schema）
       └─ Executor.invoke
            ├─ 1. 参数校验（args_schema）
            ├─ 2. 鉴权（permission.authorize）
            ├─ 3. 需确认？ → 建 pending_action，返回给模型
            │    不需要 → impl(runtime, **args)，带超时
            └─ 4. 审计（成功/失败/拒绝/挂起）
```

### 4.1 关键技术点：如何在不改 create_react_agent 的前提下拦截

`langgraph.prebuilt.create_react_agent` 内部 `ToolNode(tools)` 不暴露
`wrap_tool_call`，因此不能在图层面拦截。但 LangChain 的注入机制给了另一条路：

- `StructuredTool._injected_args_keys` 是从**被包装函数的签名**推导注入参数的
  （`_get_injected_args_keys_from_signature`），与 `args_schema` 无关；
- 只要包装函数签名里保留 `runtime: ToolRuntime[AgentContext]`，
  `ToolNode._inject_tool_args` 就会照常注入；
- `args_schema` 显式传给 `StructuredTool.from_function` 时跳过签名推断，
  模型看到的 schema 与原来完全一致。

所以包装工具写成：

```python
async def _entry(runtime: ToolRuntime[AgentContext], **kwargs):
    return await executor.invoke(spec, runtime, kwargs)
```

**这一步必须先做 spike 验证**（见计划 Task 1）：模型看到的 schema 不变、
`runtime` 能注入、`visitor_id` 能拿到。任何一条不成立，方案退回「在工具体内
调用 executor」的备选路径。

## 5. 数据模型

### 5.1 枚举

```python
Scope    = Literal["none", "visitor", "admin"]   # 需要什么数据主体
Risk     = Literal["read", "write", "destructive"]
CallerKind = Literal["agent", "admin", "confirm", "system"]
Source   = Literal["builtin", "mcp"]
AuditStatus = Literal["ok", "error", "denied", "pending", "cancelled"]
```

### 5.2 ToolSpec

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 模型可见名；MCP 工具为 `{server}__{tool}` |
| `label` | `str` | SSE 与前端显示的中文短名，如「商品」 |
| `description` | `str` | 取自实现函数 docstring |
| `args_schema` | `type[BaseModel]` | 模型可见参数模型，也是执行引擎的校验依据 |
| `impl` | `Callable` | 真实实现，签名 `(runtime, **args)` |
| `scope` | `Scope` | `visitor` 表示必须带 `visitor_id` 才能执行 |
| `risk` | `Risk` | `destructive` 强制需确认 |
| `requires_confirmation` | `bool` | 是否挂起为待确认动作 |
| `allowed_callers` | `frozenset[CallerKind]` | 调用者白名单 |
| `preview` | `Callable \| None` | 挂起前生成确认卡片内容，可拒绝 |
| `source` | `Source` | `builtin` / `mcp` |
| `server` | `str \| None` | MCP 服务器名 |
| `timeout_s` | `float` | 执行超时，默认 10s |

### 5.3 内置工具的元数据

| 工具 | label | scope | risk | 需确认 | allowed_callers |
|---|---|---|---|---|---|
| `lookup_product` | 商品 | none | read | 否 | agent, admin |
| `list_my_orders` | 订单 | visitor | read | 否 | agent |
| `lookup_work_order` | 工单 | visitor | read | 否 | agent |
| `create_work_order` | 建单 | visitor | write | **是** | agent, confirm |

`draft_work_order` 更名为 `create_work_order`：它不再是「写草稿」，而是
「提出一个待确认的建单请求」。语义变了，名字必须跟着变，否则 prompt 会
继续误导模型。

## 6. 注册中心 API

```python
# registry.py
registry.register(spec, *, override=False) -> ToolSpec
registry.get(name) -> ToolSpec | None
registry.all() -> list[ToolSpec]
registry.tools_for(caller: CallerKind) -> list[BaseTool]   # 已包装，供 agent 使用
registry.clear()                                           # 测试用

# 装饰器：注册元数据，返回模型可见的包装工具
@registered(name=..., label=..., args_schema=..., scope=..., risk=...,
            requires_confirmation=..., allowed_callers=..., preview=..., timeout_s=...)
async def impl(runtime: ToolRuntime[AgentContext], ...) -> dict: ...
```

- 重名默认抛错，`override=True` 才覆盖（MCP 重连时用）
- 注册顺序决定工具列表顺序，便于断言
- `tools_for(caller)` 只返回 `caller in allowed_callers` 的工具

## 7. 执行引擎

`Executor.invoke(spec, runtime, raw_args) -> dict`，**永不抛异常**，异常一律
归一成模型可读的 `{"error": ...}`。

| 阶段 | 失败时 | 审计 status | 返回给模型 |
|---|---|---|---|
| 1 参数校验 | `ValidationError` | `error` | `{"error": "参数不合法（字段: 原因）。请修正后重试。"}` |
| 2 鉴权 | `Denied` | `denied` | `{"error": "该操作不被允许：<原因>"}` |
| 3a 预览拒绝 | `preview.ok=False` | `ok` | preview 自带的 payload |
| 3b 挂起 | — | `pending` | `{"pending_action": {...}, "note": ...}` |
| 4 执行超时 | `TimeoutError` | `error` | `{"error": "工具执行超时，请稍后重试"}` |
| 5 执行异常 | 任意 `Exception` | `error` | `{"error": "工具暂时不可用，请稍后重试"}` |

细节：

- 校验通过后把 `args_schema.model_dump()` 传给 impl，工具体内不再自己校验
- `runtime.config["configurable"]["thread_id"]` 取 `conversation_id`，
  `runtime.context.visitor_id` 取访客，两者都不需要额外透传
- 同步 impl 用 `asyncio.to_thread` 执行，避免阻塞事件循环
- 审计写库失败只记日志，绝不影响工具返回

`Executor.confirm(action_id, *, visitor_id)`：

1. 查 `pending_actions`；不存在或 `visitor_id` 不匹配 → 404（不区分，防探测）
2. `status != pending` → 409；已过期 → 410 并置 `expired`
3. 取 spec，重放 `impl(runtime=None 等价的最小 runtime, **args)`；impl 不依赖
   注入对象时传 `runtime=None` 会让 `runtime.context` 崩，因此 confirm 路径
   统一构造 `AgentContext(visitor_id=action.visitor_id)` 的最小 runtime
4. 更新 action 为 `confirmed` + `result_json`；审计 `caller="confirm"`、`status="ok"`

`Executor.cancel(action_id, *, visitor_id)`：置 `cancelled`，审计 `cancelled`。

## 8. 权限模型

```python
@dataclass(frozen=True)
class Caller:
    kind: CallerKind
    visitor_id: str | None
```

判定顺序：

1. `caller.kind in spec.allowed_callers`，否则 `Denied("调用者不在白名单")`
2. `spec.scope == "visitor"` 且 `caller.visitor_id` 为空 → `Denied("缺少访客身份")`
3. `spec.risk == "destructive"` 且未标记 `requires_confirmation` → 注册期直接抛错
   （配置错误，不是运行时拒绝）

| scope | risk | 需确认 | agent 直接执行 |
|---|---|---|---|
| none | read | 否 | 是 |
| visitor | read | 否 | 是 |
| visitor | write | 是 | 否 → 挂起 |
| any | destructive | 强制是 | 否 → 挂起 |

数据隔离仍然由 impl 自己用 `visitor_id` 保证（`store.list_orders(visitor_id)`），
权限层只保证「有没有资格调」，不替 impl 做行级过滤。

## 9. 待确认动作

### 9.1 表

```sql
CREATE TABLE IF NOT EXISTS pending_actions (
    id              VARCHAR(32)  NOT NULL PRIMARY KEY,
    visitor_id      VARCHAR(64)  NOT NULL,
    conversation_id VARCHAR(32)  NOT NULL DEFAULT '',
    tool_name       VARCHAR(64)  NOT NULL,
    args_json       JSON         NOT NULL,
    summary_json    JSON         NOT NULL,
    status          VARCHAR(16)  NOT NULL,
    created_at      DATETIME(6)  NOT NULL,
    expires_at      DATETIME(6)  NOT NULL,
    resolved_at     DATETIME(6)  NULL,
    result_json     JSON         NULL,
    KEY idx_pending_visitor (visitor_id, created_at),
    KEY idx_pending_status (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`status` 取值：`pending` / `confirmed` / `cancelled` / `expired`。

### 9.2 卡片内容（summary_json）

```json
{
  "title": "待确认工单",
  "fields": [
    {"k": "订单号", "v": "ORDER20261101"},
    {"k": "问题类型", "v": "运输破损"},
    {"k": "问题描述", "v": "外箱破损，内件有凹陷"}
  ],
  "note": "确认后才会真正提交，可随时取消"
}
```

前端只认这三样，不认识具体业务字段——这是「通用确认卡片」的全部契约。

### 9.3 建工单的预览逻辑

`create_work_order.preview` 在挂起前做两个只读检查（原来在工具体内）：

- 订单号不属于当前顾客 → `ok=False`，payload 为「查不到该订单」的提示
- 已有同类未完结工单 → `ok=False`，payload 带既有工单号与状态

检查通过才生成 pending action。这样模型能像以前一样，在挂起之前就拿到
「重复了 / 订单不存在」的反馈，对话体验不变。

### 9.4 生命周期

```
agent 调 create_work_order
   → executor 校验、鉴权、preview 通过
   → 建 pending_actions（30 分钟有效）
   → 工具返回 {"pending_action": {"id", "summary"}, "note"}
   → 模型据此说「请核对下方卡片并确认」
   → SSE 推 {"pending_action": {...}}
   → 顾客点确认 → POST /api/tool/confirm → 执行 impl → 建工单
   → 顾客点取消 → POST /api/tool/cancel
```

原有「顾客主动提单」表单路径（`/api/work-orders/drafts` + `POST /api/work-orders`）
保持不变，继续服务界面上那个手动填单按钮。

## 10. 审计留痕

### 10.1 表

```sql
CREATE TABLE IF NOT EXISTS tool_audit_logs (
    id              BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(32)  NULL,
    visitor_id      VARCHAR(64)  NULL,
    tool_name       VARCHAR(64)  NOT NULL,
    source          VARCHAR(16)  NOT NULL,
    caller          VARCHAR(16)  NOT NULL,
    status          VARCHAR(16)  NOT NULL,
    args_json       JSON         NULL,
    result_digest   TEXT         NULL,
    error           TEXT         NULL,
    duration_ms     INT          NULL,
    action_id       VARCHAR(32)  NULL,
    created_at      DATETIME(6)  NOT NULL,
    KEY idx_audit_tool (tool_name, created_at),
    KEY idx_audit_visitor (visitor_id, created_at),
    KEY idx_audit_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 10.2 写入纪律

- 每次 `Executor.invoke` 恰好一行；confirm / cancel 各追加一行
- `args_json` 过脱敏：键名命中 `phone|mobile|address|real_name|id_card|email`
  的字段值替换为 `"***"`；超长值截断到 200 字
- `result_digest` 只存结果摘要（JSON 序列化后截断 500 字），不存全量结果
- `error` 存异常类型 + 首行消息，不存 traceback
- 写库失败只 `logger.warning`，绝不影响工具返回

## 11. MCP 接入

### 11.1 配置

仓库根新增 `mcp_servers.json`（不含密钥，可入库）：

```json
{
  "servers": {
    "logistics": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "mcp_servers.logistics.server"],
      "enabled": true,
      "scope": "none",
      "risk": "read",
      "requires_confirmation": false,
      "allowed_callers": ["agent"]
    }
  }
}
```

- `command: "python"` 会被替换成 `sys.executable`，避免依赖 PATH
- 路径可用 `MCP_CONFIG` 覆盖；`MCP_ENABLED` 控制总开关，默认 `0`
  （`.env.example` 里写 `1`，测试环境显式关掉）
- 单服务器可用 `enabled: false` 停用

### 11.2 生命周期

- `lifespan` 里用 `AsyncExitStack` 保持会话常开，而不是每次调用重开子进程
- 拉到的工具统一包装：`name = f"{server}__{tool}"`，`source="mcp"`，
  `label` 取远端工具描述前 12 字（无描述则用工具名）
- 注册失败（配置错、进程起不来、协议异常）只 `logger.warning`，
  **不能拖垮服务启动**
- 关停时 `stack.aclose()`；`registry.clear()` 由测试用，进程内不清

### 11.3 示例 MCP 服务器

`mcp_servers/logistics/server.py`：`FastMCP` + 一个工具
`query_shipping_eta(order_no, carrier=None)`，返回编造的时效数据，
用于本地验收，不依赖外网与凭据。

### 11.4 依赖

新增 `langchain-mcp-adapters`（自带 `mcp`）。已用 `uv pip install --dry-run`
验证与现有 `langchain-core 1.7.0 / langgraph 1.2.18` 无冲突（解析到 0.3.2 + mcp 1.30.0）。

若 `MultiServerMCPClient.session()` 在 0.3.2 的语义与预期不符，退回
`get_tools()` 每次调用开一次会话——验收仍能过，只是每次 MCP 调用多约 300ms。

## 12. 接口清单

**新增**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tool/confirm` | 确认待确认动作，`{action_id, visitor_id}` |
| POST | `/api/tool/cancel` | 取消待确认动作 |
| GET | `/api/admin/tools` | 注册清单：name/label/source/scope/risk/需确认/callers |
| GET | `/api/admin/audit` | 审计查询：`tool`/`visitor_id`/`status`/`limit`/`offset` |
| GET | `/admin/tools` | 工具与审计页（静态页） |

**变更**

| 位置 | 变更 |
|---|---|
| SSE 事件 | 新增 `{"pending_action": {...}}`，替代 `{"work_order_draft": {...}}` |
| `app/tools.py` → `app/tools/` | 拆包；`TOOLS` 常量改为 `agent_tools()` |
| `agent.py` | 工具名 `draft_work_order` → `create_work_order`；prompt 同步 |
| `context_builder.py` | `from app.tools import TOOLS` → `agent_tools()` |

## 13. 前端改动

**对话页**（`app.js` / `index.html`）

- `work_order_draft` 事件改为 `pending_action`，卡片渲染读 `summary.fields`
- 卡片确认 → `POST /api/tool/confirm`，成功后把卡片替换为「已提交」并刷新工单列表
- 取消 → `POST /api/tool/cancel`，卡片置灰
- 原有 `renderDraftCard` / `renderSubmittedCard` 保留结构，只换数据来源与接口

**管理页**（新增 `admin_tools.html` / `admin_tools.js`，复用 `admin.css`）

- 上半：注册清单表格（名称、来源、scope、risk、需确认、允许调用者）
- 下半：审计流水表格（时间、工具、来源、调用者、状态、耗时、访客），
  支持按工具与状态过滤
- 不做分页控件，只做「加载更多」

不做新 tab 塞进 `admin.html`：那页是 FAQ 专用布局，改它收益低于新开一页。

## 14. 测试策略

| 层次 | 用例 |
|---|---|
| 注册表 | 注册/取用/重名报错/`tools_for` 白名单过滤/clear |
| 包装工具 | schema 与 impl 一致；`runtime` 能注入；`visitor_id` 可取 |
| 执行引擎 | 校验失败/鉴权拒绝/超时/异常归一/正常返回，每种都断言审计行 |
| 权限 | 四种 scope×risk 组合的放行与拒绝；destructive 未确认时注册即报错 |
| 待确认 | 建 action → 确认 → 工单生成；取消；过期；跨访客 404 |
| 审计 | 脱敏规则；分页查询；写库失败不影响返回 |
| MCP | 起真实 stdio 子进程，断言拉到工具、调用成功、失败时服务仍启动 |
| 回归 | 现有 270 项全绿 |

MCP 用例单独放 `tests/test_mcp.py`，用 `MCP_ENABLED` 显式开关，
conftest 里默认关掉，避免每个用例都拉子进程。

## 15. 风险与坑

1. **包装工具丢注入**（最大风险）。若 Task 1 spike 不成立，退回备选方案：
   工具体内第一行调 `executor.invoke`，包装层只保留元数据。代价是每个工具
   多一行样板，但注入 100% 可靠。
2. **`args_schema` 的 `extra="forbid"` 与注入字段**。现有代码已验证可行，
   但包装层引入后要重新确认 `runtime` 不被 schema 校验拦掉。
3. **MCP 子进程在 Windows 上的编码**。stdio 服务器绝不能往 stdout 打日志，
   否则协议流被污染；日志一律走 stderr。
4. **`uv` 起子进程慢且路径脆弱**。配置里用 `python` 占位符替换为
   `sys.executable`，不依赖 PATH 与 `uv`。
5. **审计表写放大**。每次工具调用一行，长期会涨。本轮只建索引不做归档，
   在文档里标注为已知欠账。
6. **`pending_actions` 与 `work_order_drafts` 双轨**。手动提单仍走草稿表，
   两套并存是刻意的：草稿表服务「顾客自己填单」，pending_actions 服务
   「AI 提议 + 顾客确认」。在 README 里写清楚，避免后人以为是重复实现。

## 16. 验收标准

| # | 断言 | 怎么验 |
|---|---|---|
| 1 | 注册清单可见 | 浏览器开 `/admin/tools`，至少 5 个工具，四个内置 + 一个 MCP，元数据齐全 |
| 2 | 读工具审计 | 浏览器问「我的订单」，审计新增一行 `list_my_orders`，status=ok，耗时与访客正确 |
| 3 | 待确认链路 | 浏览器说「收到的杯子破了要投诉」→ 出现确认卡片 → 点确认 → 工单创建 + 审计出现 pending 与 ok 两行 |
| 4 | 取消链路 | 同一流程点取消 → 工单未创建，审计 status=cancelled |
| 5 | 跨访客越权 | 用 A 的 action_id 配 B 的 visitor_id 调 confirm → 404 |
| 6 | 权限拒绝 | 直接以无 visitor_id 的 caller 调 `list_my_orders` → 返回拒绝文案，审计 status=denied |
| 7 | 参数校验 | 让模型传不存在的 issue_type → 返回结构化参数错误，审计 status=error，对话不中断 |
| 8 | MCP 接入 | 启动日志显示加载 1 个 server、N 个工具；浏览器问「我的快递什么时候到」→ 调用 `logistics__query_shipping_eta`，审计 source=mcp |
| 9 | MCP 降级 | 把 `mcp_servers.json` 指向不存在的命令重启 → 服务正常启动，日志有 warning |
| 10 | 回归 | `uv run pytest` 全绿 |
| 11 | 浏览器实跑 | 上述 1–9 全在浏览器点过一遍，不是只跑单测 |
</content>