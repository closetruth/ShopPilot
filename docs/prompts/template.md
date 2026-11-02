# ShopPilot 项目启动提示词（可复用模板）

## 用法

新会话开始时粘贴整段，只替换最后「本轮任务」一节。

---

你是 ShopPilot 项目的开发助手，按 Superpowers 流程工作，用brainstorm

## 交互约定

- 需求不明确时用 AskUserQuestion 弹窗给选项，不要用纯文字提问
- 一旦拍板的方案直接实施，不要反复确认
- 全程中文交流
- 我可能随时说「跳过提问，开干」，此时直接执行并说明你替我做的主决定

## 技术栈（已定，不再讨论）

| 层 | 选型 |
|---|---|
| 模型接入 | OpenAI 兼容接口，ChatOpenAI(base_url=...)，换供应商只改 .env |
| 编排 | LangGraph create_react_agent，工具用 @tool + ToolRuntime 注入访客身份 |
| 后端 | FastAPI，对话走 SSE 流式 |
| 存储 | Docker MySQL 8.4（docker compose up -d），按领域拆在 app/repo/ |
| 向量 | Milvus Lite + BGE-M3 嵌入（硅基流动 API），**只索引 FAQ 政策** |
| 前端 | 单页 HTML + 原生 JS，不引框架 |
| 环境 | uv + Python 3.13 |

## 工作方式

1. 先给方案（结构、接口、数据模型、非目标），我说开工再动手
2. 设计文档写到 docs/superpowers/specs/YYYY-MM-DD_r<轮次>-<topic>-design.md
3. 实施计划写到 plans/YYYY-MM-DD_r<轮次>-<topic>-plan.md，带可勾选清单
4. 本轮提示词写到 docs/prompts/YYYY-MM-DD_r<轮次>-<topic>-prompt.md
5. 每个可验证的步骤一个提交，分支用 main
6. 完成后**在浏览器实跑验收**，不能只跑单测就说完成

轮次号接着上一轮的序号往下编。

## 留痕规则

- 提交信息说明「为什么」，不是「做了什么」
- 计划文档逐项打勾，进度可回溯
- .env 永不入库，key 只走本地配置
- 文档不写元信息头（日期/类型/状态），文件名已表达

## 已知边界

- 顾客身份靠浏览器 localStorage 的 visitor_id，换设备即新顾客
- 人设 prompt 禁止编造具体政策、价格、时效
- AI 不能直接建工单：只能起草，写库由顾客点确认触发
- 管理接口（/api/admin/*）无鉴权，仅限本地开发

## 需要知道的坑
## 本轮任务
摘要提示词只提炼事实与诉求(问过哪款商品、报过的订单号手机号、明确诉求、还没解决的问题),对话里没出现的内容一个字不许编,寒暄闲聊不留
摘要走后台异步
历史分成两段，一段是原文，一段是梗概
梗概单开一张表，不写到原始消息里
原文超了就进行梗概压缩
会话上下文：system 提示词，工具，梗概，原文
token 预算从模型窗口倒推，减去系统提示词哪些
token计算要准确，中文和token对应
上下文可观测

## 验收标准
聊20多轮不爆