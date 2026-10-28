# ShopPilot 项目启动提示词（可复用）

## 用法

你是 ShopPilot 项目的开发助手，按 Superpowers 流程工作。前端不用。
动手前先通过 Context7 查最新库/API 文档。

## 交互约定

- 需求不明确时用 AskUserQuestion 弹窗给选项，不要用纯文字提问

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangChain |
| 后端 | FastAPI，对话走 SSE 流式，LangChain `@tool` |
| 存储 | Docker MySQL |
| 前端 | 单页 HTML + 原生 JS，不引框架 |
| 环境 | uv + Python 3.13 |

## 工作方式

1. 先给方案（结构、接口、数据模型、非目标），我说开工再动手
2. 每轮设计文档写到 `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
3. 每轮实施计划写到 `plans/` 下，带可勾选清单
4. 每轮提示词写到 `docs/prompts`
5. 每个可验证的步骤一个提交，分支用 `main`
6. 完成后**在浏览器实跑验收**，不能只跑单测就说完成

## 留痕规则

- 提交信息说明「为什么」，不是「做了什么」
- 计划文档逐项打勾，进度可回溯
- `.env` 永不入库，key 只走本地配置

## 已知边界

（待补）

## 本轮任务

加上工具，能调用工具查工单、查商品，工具需要校验参数
