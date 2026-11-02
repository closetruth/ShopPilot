"""pytest 全局配置：加载 .env，让依赖嵌入 API 的测试能读到密钥。

没有这个文件时，`os.getenv("SILICONFLOW_API_KEY")` 在收集阶段返回空，
所有相关用例会被静默跳过——看起来「全绿」，实际一条没跑。
"""

import os

from dotenv import load_dotenv

load_dotenv()

# 默认关掉 MCP：否则每个起 TestClient 的用例都会拉一次 stdio 子进程。
# tests/test_mcp.py 用 monkeypatch.setenv 单独打开。
os.environ["MCP_ENABLED"] = "0"