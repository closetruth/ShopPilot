# MewHelp · Ch01 电商智能客服（纯对话）

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
cp .env.example .env     # 编辑 OPENAI_API_KEY 等
```

## 测试

```bash
pytest -v
```

## 启动

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

## API 演示（Windows CMD）

流式对话：

```bat
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"message\":\"我的快递丢了，订单ORD-1001\"}"
```

多轮对话（将首次响应中的 `session_id` 填入 `<SID>`）：

```bat
curl -N -X POST http://127.0.0.1:8000/api/v1/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"session_id\":\"<SID>\",\"message\":\"希望退款\"}"

curl http://127.0.0.1:8000/api/v1/sessions/<SID>/messages
```

结构化抽取：

```bat
curl -X POST http://127.0.0.1:8000/api/v1/extract ^
  -H "Content-Type: application/json" ^
  -d "{\"text\":\"订单ORD-1001丢件，想退款\"}"
```
