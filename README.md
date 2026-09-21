# MewHelp · Ch01 电商智能客服（纯对话）

## 安装

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置

复制示例环境文件并填入上游 LLM 凭证：

```powershell
copy .env.example .env
```

编辑 `.env` 中的 `OPENAI_API_KEY`、`OPENAI_API_BASE`、`MODEL_NAME` 等。其余默认值见 `.env.example`（SQLite 路径、上下文 token 预算、历史轮数上限）。

> **本仓库未提交 `.env`**。无有效 API Key 时，自动化测试与离线 mock 仍可运行；依赖真实模型的 live curl 验收与抽取评估需先配置 `.env`。

## 测试

全量单元 / 集成测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

售后抽取评估（8 条 JSONL 标注；无 `.env` 时用 `SKIP_LIVE_EVAL=1` 跳过 live 调用）：

```powershell
$env:SKIP_LIVE_EVAL=1; .venv\Scripts\python.exe evals/run_extract_eval.py
```

配置 `.env` 后去掉 `SKIP_LIVE_EVAL` 即可对真实模型跑评估，通过率低于 60% 时脚本退出码为 1。

## 启动

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

浏览器打开 `http://127.0.0.1:8000/` 可使用聊天页（SSE 多轮 + 可选抽取）。

## API 演示（Windows CMD）

需先启动 uvicorn 且 `.env` 已配置有效上游。三则命令对应 spec 验收标准。

**1. SSE 流式对话** — 响应 `Content-Type: text/event-stream`，依次出现 `session`、`token`、`done` 事件：

```bat
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"message\":\"我的快递丢了，订单ORD-1001\"}"
```

**2. 多轮上下文** — 将首次响应 `session` 事件中的 `session_id` 填入 `<SID>`，第二轮应能承接上文；`GET messages` 核对 SQLite 落库：

```bat
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"session_id\":\"<SID>\",\"message\":\"希望退款\"}"

curl http://127.0.0.1:8000/api/v1/sessions/<SID>/messages
```

**3. 结构化售后抽取** — 返回 JSON（`intent_type`、`order_id` 等字段）：

```bat
curl -X POST http://127.0.0.1:8000/api/v1/extract ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"订单ORD-1001丢件，想退款\"}"
```

也可在 chat 接口传 `"mode":"extract"` 走同一会话内的抽取路径（见 spec）。
