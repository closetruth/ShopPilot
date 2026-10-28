# ShopPilot 阶段一实施计划

对应设计：`docs/superpowers/specs/2026-10-28-chat-mvp-design.md`

## 步骤 1：依赖安装与可启动骨架

- [x] `uv sync` 拉取依赖，确认 Python 3.13 解析成功
- [x] `app/__init__.py` 建立包
- [x] `app/main.py` 最小 FastAPI 应用，`GET /health` 返回 `{"ok": true}`
- [x] 验证：`uv run uvicorn app.main:app` 起服务，curl `/health` 通
- [x] 提交

## 步骤 2：存储层

- [x] `app/store.py`：`init_db()` 建表、`create_conversation()`、`conversation_exists()`、`load_history(conv_id, limit)`、`append_message(conv_id, role, content)`
- [x] 连接每个操作独立打开关闭，启用 `PRAGMA foreign_keys=ON`
- [x] 验证：`uv run pytest tests/test_store.py`
- [x] 提交

## 步骤 3：模型接入

- [x] `app/prompt.py`：`SYSTEM_PROMPT` 常量，含角色/语气/政策边界
- [x] `app/llm.py`：`get_llm()` 返回 `ChatOpenAI`，读取 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `CHAT_MODEL`
- [x] 缺 key 时启动即抛错
- [x] 验证：写一个临时脚本跑一次真实调用，确认能流式吐字
- [x] 提交

## 步骤 4：SSE 对话端点

- [x] `app/main.py` 增加 `POST /api/chat`，请求体 `{conversation_id?, message}`
- [x] 无 `conversation_id` 时新建并作为首个 SSE 事件回传
- [x] 拼装 `[system] + 历史 + 用户消息`，调 `astream`
- [x] 逐块 yield `{"delta": ...}`
- [x] 结束后落库 user + assistant 两条消息，yield `[DONE]`
- [x] 上游异常时 yield `{"error": ...}`，本轮 assistant 不落库
- [x] 客户端中断时 `finally` 落库已生成部分
- [x] 验证：curl `-N` 观察逐行输出
- [x] 提交

## 步骤 5：前端聊天页

- [x] `app/static/index.html`：消息区、输入框、发送按钮
- [x] `app/static/style.css`：气泡布局，用户/AI 左右区分，流式光标
- [x] `app/static/app.js`：
  - [x] `localStorage` 读写 `conversation_id`
  - [x] `fetch` + `ReadableStream` 逐行解析 SSE（不用 EventSource，需 POST）
  - [x] 首屏拉历史（新增 `GET /api/history?conversation_id=`）
  - [x] 发送中禁用输入，显示流式光标
  - [x] `error` 事件渲染为错误气泡
- [x] `app/main.py` 挂载 `StaticFiles`
- [x] 验证：浏览器实跑三轮对话 + 刷新 + 故意断网
- [x] 提交

## 步骤 6：测试与文档收尾

- [x] `tests/test_store.py`：建表、追加、读历史、截断
- [x] `tests/test_chat.py`：历史拼装函数纯逻辑测试（不调网络）
- [x] `README.md`：三步启动说明 + 环境变量表
- [x] 全量 `uv run pytest` 通过
- [x] 提交

## 完成定义

设计文档「验收标准」六条全部满足，`git log` 每个步骤对应一个可读提交。
