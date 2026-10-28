"""pytest 全局配置：加载 .env，让依赖嵌入 API 的测试能读到密钥。

没有这个文件时，`os.getenv("SILICONFLOW_API_KEY")` 在收集阶段返回空，
所有相关用例会被静默跳过——看起来「全绿」，实际一条没跑。
"""

from dotenv import load_dotenv

load_dotenv()
