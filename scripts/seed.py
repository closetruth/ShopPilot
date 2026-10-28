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

ORDERS = [
    ("ORDER20260001", DEMO_VISITOR, "p-mug-blue", 1, 5900, "2026-10-19 20:41:00"),
    ("ORDER20260002", DEMO_VISITOR, "p-bottle-pink", 1, 12900, "2026-10-23 11:02:00"),
    # 该顾客尚未提过工单：验证「首单顾客也能建单」
    ("ORDER20260009", DEMO_VISITOR, "p-mug-white", 2, 7800, "2026-10-27 09:15:00"),
    ("ORDER20260003", "another-visitor", "p-bowl-ceramic", 1, 2900, "2026-10-24 15:30:00"),
]

WORK_ORDERS = [
    ("wo-1", DEMO_VISITOR, "ORDER20260001", "p-mug-blue", "待处理",
     "顾客收到时杯口有裂纹", "运输破损", "普通", "manual",
     "2026-10-20 09:12:00", "2026-10-26 14:30:00"),
    ("wo-2", DEMO_VISITOR, "ORDER20260002", "p-bottle-pink", "处理中",
     "下单 5 天未发货", "发货延迟", "普通", "manual",
     "2026-10-24 18:05:00", "2026-10-27 10:11:00"),
    ("wo-3", "another-visitor", "ORDER20260003", "p-bowl-ceramic", "已完结",
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
                INSERT INTO orders
                    (order_no, visitor_id, product_id, quantity, amount_cents,
                     created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    visitor_id = VALUES(visitor_id),
                    product_id = VALUES(product_id),
                    quantity = VALUES(quantity),
                    amount_cents = VALUES(amount_cents)
                """,
                ORDERS,
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
        print(
            f"seeded {len(PRODUCTS)} products, {len(ORDERS)} orders, "
            f"{len(WORK_ORDERS)} work orders"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
