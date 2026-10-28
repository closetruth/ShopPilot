"""从 MySQL 全量重建向量索引。

Milvus Lite 对数据文件加独占锁，服务运行时无法从外部进程打开同一文件。
两种用法：

1. 服务已关闭：uv run python -m scripts.reindex [--drop]
2. 服务运行中：调用 POST /api/admin/reindex（在服务进程内执行）
"""

from __future__ import annotations

import sys

from app import store, vector_store


def main(drop: bool = False) -> None:
    store.init_db()
    print("开始重建向量索引（如服务正在运行，本脚本会因文件锁失败）")

    result = vector_store.rebuild_from_store(drop=drop)

    print(
        f"读取 {result['products']} 个商品 + {result['work_orders']} 张工单，"
        f"共 {result['total']} 条文档"
    )
    print(f"成功写入 {result['written']} 条向量")

    if result["written"] < result["total"]:
        print(
            "注意：部分文档未写入。若提示文件锁，请改用 POST /api/admin/reindex，"
            "或先停服务再执行本脚本。"
        )


if __name__ == "__main__":
    main(drop="--drop" in sys.argv)
