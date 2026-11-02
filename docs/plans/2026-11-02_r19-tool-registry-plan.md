# r19 实施计划：工具注册中心、权限、执行引擎、审计与 MCP 接入

设计：`docs/superpowers/specs/2026-11-02_r19-tool-registry-design.md`

裁定回顾（六项全按推荐）：MCP 走**客户端**方向；权限用
**scope + risk + allowed_callers** 三层；建工单泛化为**通用待确认动作**；
审计落**独立表** `tool_audit_logs`；注册中心用**装饰器 + 进程内注册表**；
验收自带**最小 stdio MCP server**。

执行纪律：每个 Task 先写失败测试，再写实现，再跑全量；每个 Task 一个提交，
提交信息说「为什么」。

---

- [x] **Task 1：包装工具的 runtime 注入 spike**
  - 用现有 `lookup_product` 构造一个包装工具：签名保留
    `runtime: ToolRuntime[AgentContext]`，`args_schema` 显式传入
  - 断言四件事：模型可见 schema 与 `args_schema` 一致且不含 `runtime`；
    `ToolNode` 能把 `runtime` 注进来；包装内能读到
    `runtime.context.visitor_id`；调用结果与直调一致
  - 结论：四条全部成立，**走主方案**（包装函数 + 显式 `args_schema`），
    不需要退回备选。spike 用最小回显工具，避免拉 MySQL
  - 测试：`tests/test_tool_wrapper.py`
  - 提交：`test: 钉住包装工具的 runtime 注入，确认不动 create_react_agent 也能拦截`

- [x] **Task 2：注册表与权限模型**
  - `app/tools.py` 拆成包：`__init__.py` / `registry.py` / `permission.py` /
    `builtin.py`，`AgentContext` 从 `__init__` 导出，现有 import 不破
  - `ToolSpec` 数据类（name/label/description/args_schema/impl/scope/risk/
    requires_confirmation/allowed_callers/preview/source/server/timeout_s）
  - `registry.register/get/all/tools_for/clear`；重名抛错，`override=True` 才覆盖
  - `@registered(...)` 装饰器：注册元数据，返回包装后的 `BaseTool`
  - `permission.Caller` + `authorize`：白名单 → visitor 身份 → destructive 配置校验
  - 四个内置工具迁入 `builtin.py`，语义不变（此时 `draft_work_order` 仍叫原名）
  - `TOOLS` 常量改为 `agent_tools()`；`agent.py` / `context_builder.py` 跟着改
  - 测试：注册/重名/白名单过滤/四种 scope×risk 组合/destructive 注册即报错
  - 实测补丁：LangChain 默认解析有两处会把注入参数弄丢（`extra="forbid"`
    schema 把 `runtime` 判成多余字段；空入参 schema 直接短路返回空 kwargs），
    用 `RegisteredTool` 子类修正，spike 结论在真实工具上依然成立
  - 提交：`feat: 工具注册中心与三层权限，工具元数据可被程序读取`

- [x] **Task 3：审计表与仓储**
  - `db.py` SCHEMA 加 `tool_audit_logs`（含三个索引）
  - `app/repo/audit.py`：`record(...)` 与 `list_logs(tool, visitor_id, status, limit, offset)`
  - 脱敏：键名命中 `phone|mobile|address|real_name|id_card|email` → `***`；
    值截断 200 字；`result_digest` 截断 500 字；`error` 只留类型 + 首行
  - 写库失败只 warning，不外抛
  - `store.py` 加转发
  - 测试：脱敏规则、截断、分页、写失败不影响调用方
  - 提交：`feat: 工具调用审计落库，参数脱敏与失败不阻断`

- [x] **Task 4：执行引擎**
  - `app/tools/executor.py`：`Executor.invoke(spec, runtime, raw_args)`
  - 顺序：参数校验（`args_schema.model_validate`）→ 鉴权 → 预览/执行 → 审计
  - 校验通过后把 `model_dump()` 传给 impl；工具体内的 `try/except ValidationError`
    与 `ProductQuery(...)` 等重复校验全部删掉
  - 同步 impl 走 `asyncio.to_thread`；`asyncio.wait_for(spec.timeout_s)`
  - 永不抛异常，失败一律 `{"error": ...}`；审计 status 按设计表落
  - `conversation_id` 从 `runtime.config["configurable"]["thread_id"]` 取
  - 测试：校验失败/鉴权拒绝/超时/impl 抛异常/正常，五种都断言审计行
  - 提交：`feat: 执行引擎统一校验与鉴权，工具调用全程留痕`

- [x] **Task 5：待确认动作**
  - `db.py` SCHEMA 加 `pending_actions`；`app/repo/pending.py`
    （create/get/resolve/list_by_visitor，30 分钟过期）
  - `Executor.confirm` / `Executor.cancel`：归属校验（不区分 404 防探测）、
    状态机（409）、过期（410）、确认路径构造最小 runtime
  - `draft_work_order` 改名 `create_work_order`，`requires_confirmation=True`，
    加 `preview`：订单不属于本人 → 拒绝；已有同类未完结工单 → 拒绝并带既有单号
  - `agent.py` prompt 同步：说明工具返回待确认、不能宣称「已提交」
  - 测试：建 action → 确认 → 工单生成；取消；过期；跨访客 404；重复预览拒绝
  - 提交：`feat: 通用待确认动作，建工单迁为第一个实例`

- [x] **Task 6：SSE 与对话页接线**
  - `customer.py`：删掉 `_draft_from_tool_event` 的工具输出反解析，
    改为转发 executor 已生成的 `pending_action`；`TOOL_LABELS` 改由注册表取 label
  - 新增 `POST /api/tool/confirm`、`POST /api/tool/cancel`
  - `app.js`：`work_order_draft` → `pending_action`；卡片读 `summary.fields`；
    确认/取消改调新接口；成功后卡片替换为「工单已提交」
  - 测试：SSE 事件序列含 `pending_action`；confirm 接口越权 404；取消不落单
  - 提交：`feat: 待确认动作接进 SSE 与对话页，确认由顾客点击触发`

- [x] **Task 7：MCP 接入**
  - `pyproject.toml` 加 `langchain-mcp-adapters`
  - `mcp_servers/logistics/server.py`：`FastMCP` + `query_shipping_eta`，
    日志只走 stderr，stdout 留给协议
  - `mcp_servers.json`：`command: "python"` 占位，`enabled`/`scope`/`risk` 可覆盖
  - `app/tools/mcp.py`：读配置、`AsyncExitStack` 常开会话、工具转 ToolSpec，
    名 `{server}__{tool}`，`source="mcp"`
  - `main.py` lifespan：加载、注册、关停 `aclose()`；任何失败只 warning
  - `.env.example` 加 `MCP_ENABLED=1`、`MCP_CONFIG`；`tests/conftest.py` 默认关
  - 测试：`tests/test_mcp.py` 起真实子进程，断言拉到工具/调用成功/
    配错命令时服务仍启动
  - 提交：`feat: MCP 客户端接入，外部工具进同一注册中心与执行链`

- [x] **Task 8：注册清单与审计页**
  - `GET /api/admin/tools`、`GET /api/admin/audit`
  - `app/static/admin_tools.html` + `admin_tools.js`，复用 `admin.css`
  - 上半注册清单表，下半审计流水表，按工具与状态过滤，「加载更多」
  - `admin.py` 加 `/admin/tools` 静态路由
  - 测试：两个接口的字段与过滤
  - 提交：`feat: 注册清单与审计流水页，工具行为可回溯`

- [x] **Task 9：回归、浏览器实跑、文档**
  - `uv run pytest` 全绿（329 passed）
  - 浏览器逐条走设计文档第 16 节验收表 1–9
  - README 补工具注册中心、MCP 配置、`pending_actions` 与草稿表双轨说明
  - `docs/dev-notes/r19.md` 收尾段（前面各阶段已边做边补）
  - 提交：`docs: r19 验收记录与 README 同步`

---

## 验收对照

| # | 断言 | 落在哪个 Task |
|---|---|---|
| 1 | `/admin/tools` 清单可见、元数据齐全 | Task 8 |
| 2 | 读工具审计落行 | Task 3/4 |
| 3 | 待确认 → 确认 → 工单创建 | Task 5/6 |
| 4 | 取消不落单 | Task 5/6 |
| 5 | 跨访客 confirm 404 | Task 5 |
| 6 | 无访客身份被拒 | Task 2/4 |
| 7 | 非法参数返回结构化错误 | Task 4 |
| 8 | MCP 工具可调、审计 source=mcp | Task 7 |
| 9 | MCP 配错不拖垮启动 | Task 7 |
| 10 | 全量测试绿 | Task 9 |
| 11 | 浏览器实跑 | Task 9 |