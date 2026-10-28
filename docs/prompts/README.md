# 提示词索引

每轮开发开始时粘贴给助手的上下文。作用是让新会话不用重新交代背景。

## 现行版本

| 文件 | 适用 | 状态 |
|---|---|---|
| [2026-10-28-tools-kickoff.md](2026-10-28-tools-kickoff.md) | 工具调用阶段的上下文 | 已归档 |
| [2026-10-28-milvus-kickoff.md](2026-10-28-milvus-kickoff.md) | 向量检索阶段的上下文 | 已归档 |
| [shoppilot-kickoff.md](shoppilot-kickoff.md) | 最初的项目模板 | **已过时**，技术栈仍在写 SQLite |

**注意：目前没有一份反映当前状态的提示词。**

`shoppilot-kickoff.md` 里写的是 SQLite、无向量库、无工具调用，与现状完全不符。
直接使用会拿到错误的技术栈。新会话请改用下面的「当前技术栈」一节。

## 当前技术栈

| 层 | 选型 |
|---|---|
| 模型 | DeepSeek，OpenAI 兼容接口（`ChatOpenAI`），换供应商只改 `.env` |
| 编排 | LangGraph `create_react_agent`，工具用 `@tool` + `ToolRuntime` 注入访客身份 |
| 后端 | FastAPI，对话走 SSE 流式 |
| 存储 | Docker MySQL 8.4（`docker compose up -d`） |
| 向量 | Milvus Lite（本地文件，**单进程独占**）+ BGE-M3（硅基流动 API） |
| 前端 | 原生 HTML/JS，不引框架；`/admin` 用 ES module |
| 环境 | uv + Python 3.13 |

## 结构约定

```
app/
  db.py         连接与建表
  repo/         按领域拆分的仓储（chats / commerce / knowledge）
  store.py      仓储的转发门面，调用方只认它
  tools.py      五个工具
  agent.py      LangGraph Agent
  vector_store.py Milvus 索引（只存 FAQ）
  embedding.py  BGE-M3 嵌入
  main.py       FastAPI 全部端点
  static/       chat 页 + admin 页
```

## 关键约束

- **向量库只存 FAQ**。商品走结构化查询（查不到返回全量），工单按访客隔离查询
- **工单只能由顾客点击创建**，AI 只能起草，写库在 `POST /api/work-orders`
- **管理接口无鉴权**，上线前必须加
- **Milvus Lite 单进程**，服务运行时 `scripts/reindex.py` 必然失败，走 `/api/admin/reindex`

## 写下一份提示词时

文件名用 `YYYY-MM-DD-<主题>-kickoff.md`，头部三行元信息：

```
日期：2026-10-28
类型：提示词
状态：已归档
```
