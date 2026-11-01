"""api 包：按消费者拆分的路由模块。

- customer.py  顾客侧：对话 SSE、历史、订单、工单、FAQ 详情
- admin.py     管理侧：FAQ CRUD、批量导入、重建索引（无鉴权，本地开发）
- _graph.py    对话图单例（独立模块避免循环导入）
"""
