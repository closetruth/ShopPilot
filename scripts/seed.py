"""灌入虚构的商品与工单数据，供开发与验收使用。

幂等：按主键 INSERT ... ON DUPLICATE KEY UPDATE，重复执行不会产生重复行。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pymysql
import pymysql.cursors

from app.store import _conn_params

DEMO_VISITOR = "demo-visitor"

PRODUCTS = [
    ("p-mug-blue", "青花瓷马克杯", "杯具", "蓝色", 5900, 42,
     "手工青花釉下彩，容量 350ml，可微波加热。"),
    ("p-mug-white", "简约白瓷马克杯", "杯具", "白色", 3900, 120,
     "哑光白瓷，容量 300ml，杯壁加厚保温。"),
    ("p-bottle-pink", "真空保温杯", "杯具", "粉色", 12900, 18,
     "304 不锈钢真空内胆，500ml，保温 12 小时。"),
    ("p-bottle-blue", "运动保温杯", "杯具", "蓝色", 9900, 0,
     "316 不锈钢，750ml，附吸管盖。"),
    ("p-bowl-ceramic", "日式陶瓷饭碗", "餐具", "白色", 2900, 76,
     "5.5 英寸，釉面光滑，可洗碗机清洗。"),
]

WORK_ORDERS = [
    ("wo-1", DEMO_VISITOR, "ORDER20260001", "p-mug-blue", "运输破损",
     "顾客收到时杯口有裂纹", "运输破损", "普通", "manual",
     "2026-10-20 09:12:00", "2026-10-26 14:30:00"),
    ("wo-2", DEMO_VISITOR, "ORDER20260002", "p-bottle-pink", "发货延迟",
     "下单 5 天未发货", "发货延迟", "普通", "manual",
     "2026-10-24 18:05:00", "2026-10-27 10:11:00"),
    ("wo-3", "another-visitor", "ORDER20260003", "p-bowl-ceramic", "退换货",
     "尺寸不合适申请退货", "退换货", "普通", "manual",
     "2026-10-25 11:20:00", "2026-10-27 16:45:00"),
]


def seed() -> None:
    conn = pymysql.connect(**_conn_params())
    try:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO products
                    (id, name, category, color, price_cents, stock, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name), category = VALUES(category),
                    color = VALUES(color), price_cents = VALUES(price_cents),
                    stock = VALUES(stock), description = VALUES(description)
                """,
                PRODUCTS,
            )
            cur.executemany(
                """
                INSERT INTO work_orders
                    (id, visitor_id, order_no, product_id, status, issue,
                     issue_type, priority, source, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status), issue = VALUES(issue),
                    issue_type = VALUES(issue_type),
                    priority = VALUES(priority), source = VALUES(source),
                    updated_at = VALUES(updated_at)
                """,
                WORK_ORDERS,
            )
        conn.commit()
        print(f"seeded {len(PRODUCTS)} products, {len(WORK_ORDERS)} work orders")
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
