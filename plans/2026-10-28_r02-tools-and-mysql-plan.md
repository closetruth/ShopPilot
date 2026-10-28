# ShopPilot 阶段二实施计划：工具调用 + MySQL

对应设计：`docs/superpowers/specs/2026-10-28_r02-tools-and-mysql-design.md`

## 步骤 1：依赖与环境

- [x] 加依赖 `langgraph`、`pymysql`、`cryptography`
- [x] 写 `docker-compose.yml`，MySQL 8.4，端口 3307，utf8mb4，UTC
- [x] `docker compose up -d` 起容器，等到 healthy
- [x] 验证：用 pymysql 连上并 `SELECT VERSION()`
- [x] 提交

## 步骤 2：存储层迁移到 MySQL

- [x] `store.py` 改 pymysql，`_conn_params()` 从环境变量读取
- [x] 建四张表：`conversations`、`messages`、`products`、`work_orders`
- [x] 所有查询改 `%s` 占位符（预编译，防注入）
- [x] 会话相关函数保持不变（create / owned_by / load_history / list / append）
- [x] 新增 `search_products()`、`find_work_order()`
- [x] 验证：`SHOW TABLES` 四张表都在
- [x] 提交

## 步骤 3：种子数据

- [x] `scripts/seed.py`：5 个虚构商品、3 张工单（2 张属 demo-visitor，1 张属另一个访客）
- [x] 用 `ON DUPLICATE KEY UPDATE` 保证幂等
- [x] 验证：重复执行不产生重复行
- [x] 提交

## 步骤 4：工具实现

- [x] `app/schemas.py`：`ProductQuery`、`WorkOrderQuery` 校验模型
- [x] `app/tools.py`：`lookup_product`、`lookup_work_order`
- [x] 注入方式：`runtime: ToolRuntime[AgentContext]`（裸类型注解，不加 `Annotated`）
- [x] 校验失败返回结构化错误，不抛异常
- [x] 工单查询强制带 `visitor_id`
- [x] 验证：直接调 store 确认隔离生效
- [x] 提交

## 步骤 5：Agent 编排

- [x] `app/agent.py`：`create_react_agent(model, tools, prompt, context_schema)`
- [x] `app/prompt.py` 重写：说明工具可用性与使用规则
- [x] `main.py` 的 `/api/chat` 改用 `agent.astream_events`
- [x] 提取 `on_chat_model_stream` 的文本 → `{"delta": ...}`
- [x] 提取 `on_tool_start` / `on_tool_end` → `{"tool", "label", "status"}`
- [x] 验证：脚本跑三问，确认工具被调用且回答基于真实数据
- [x] 提交

## 步骤 6：前端工具提示

- [x] 气泡上方加 `.tool-hint` 状态条
- [x] 收到 `tool start` 显示「正在查询商品…」，`done` 隐藏
- [x] `finally` 里兜底隐藏，避免异常时残留
- [x] 验证：浏览器观察提示出现与消失
- [x] 提交

## 步骤 7：数据迁移

- [x] `scripts/migrate_sqlite_to_mysql.py`，支持 `--dry-run`
- [x] 按 `(conversation_id, role, content, created_at)` 去重，可重复执行
- [x] 保留原 `id` 与时间戳；SQLite 文件不删
- [x] 验证：首次写入 5 会话 10 消息，再跑写入 0 条
- [x] 提交

## 步骤 8：测试与验收

- [x] `tests/test_store.py` 改为用随机 `visitor_id` 隔离（共享 MySQL 库）
- [x] 新增工具与搜索相关测试
- [x] `tests/test_schemas.py`：参数校验覆盖合法与非法输入
- [x] 全量 `uv run pytest` 通过（34 项）
- [x] 浏览器验收四项：商品查询、工单查询、越权拒绝、参数纠错
- [x] `.env.example` 补充 MySQL 变量
- [x] 提交

## 完成定义

设计文档「验收标准」八条全部满足，浏览器实跑通过，测试全绿。

## 执行中偏离计划的地方

- 工具注入最初用 `args_schema` + `InjectedToolArg`，被 Pydantic `extra_forbid` 拒绝；
  改用 `Annotated[Runtime, InjectedToolArg]` 又无法生成 JSON schema。
  最终按 LangGraph 文档要求用裸类型注解 `runtime: ToolRuntime[AgentContext]` 才通过。
- 商品搜索最初做逐字 OR 退化，导致「手机」误命中含「不锈钢」的商品。
  改为「整词 AND → 多词半命中 HAVING」，放弃逐字匹配，宁可返回空让模型换词。
