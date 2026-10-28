"""商品、订单、工单与工单草稿读写。"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.db import connect, now

WORK_ORDER_OPEN_STATUSES = ("待处理", "处理中")
DRAFT_TTL_MINUTES = 30


# ---------- 商品 ----------


def search_products(
    keyword: str | None = None, product_id: str | None = None, limit: int = 5
) -> list[dict]:
    """按关键词或 ID 查商品。只返回对顾客可见的字段。

    关键词按空格切分，每个词都要命中（名称/类别/颜色/描述任一）。
    """
    if product_id:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, color, price_cents, stock, description
                  FROM products WHERE id = %s LIMIT 1
                """,
                (product_id,),
            )
            return [_product_row(r) for r in cur.fetchall()]

    if not keyword:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, category, color, price_cents, stock, description
                  FROM products ORDER BY stock DESC, id ASC LIMIT %s
                """,
                (limit,),
            )
            return [_product_row(r) for r in cur.fetchall()]

    terms = [t for t in keyword.split() if t][:4]
    rows = _search_terms(terms, joiner="AND", limit=limit)
    if not rows and len(terms) > 1:
        # 全部词都命中太严时放宽为「至少一半命中」，让「蓝色 杯子」也能有结果
        rows = _search_terms_any(terms, max(1, len(terms) // 2), limit)
    return [_product_row(r) for r in rows]


def _search_terms(terms: list[str], joiner: str, limit: int) -> list[dict]:
    where = f" {joiner} ".join(
        "(name LIKE %s OR category LIKE %s OR color LIKE %s OR description LIKE %s)"
        for _ in terms
    )
    params: list = []
    for term in terms:
        like = f"%{term}%"
        params.extend([like, like, like, like])
    params.append(limit)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, category, color, price_cents, stock, description
              FROM products WHERE {where}
             ORDER BY stock DESC, id ASC LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def _search_terms_any(terms: list[str], need: int, limit: int) -> list[dict]:
    """至少 need 个词命中即返回。用 SUM(CASE WHEN ...) 统计命中数。"""
    per_term = (
        "SUM(CASE WHEN name LIKE %s OR category LIKE %s"
        " OR color LIKE %s OR description LIKE %s THEN 1 ELSE 0 END)"
    )
    score_expr = " + ".join(per_term for _ in terms)

    params: list = []
    for term in terms:
        like = f"%{term}%"
        params.extend([like, like, like, like])
    params.extend([need, limit])

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, name, category, color, price_cents, stock, description
              FROM products
             GROUP BY id, name, category, color, price_cents, stock, description
            HAVING {score_expr} >= %s
             ORDER BY stock DESC, id ASC LIMIT %s
            """,
            params,
        )
        return cur.fetchall()


def _product_row(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "category": r["category"],
        "color": r["color"],
        "price_yuan": r["price_cents"] / 100,
        "stock": r["stock"],
        "description": r["description"],
    }


# ---------- 订单 ----------


def visitor_has_order(visitor_id: str, order_no: str) -> bool:
    """该顾客名下是否有此订单。建单前据此校验归属。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM orders WHERE visitor_id = %s AND order_no = %s LIMIT 1",
            (visitor_id, order_no),
        )
        return cur.fetchone() is not None


def list_orders(visitor_id: str) -> list[dict]:
    """该顾客的订单，最新的在前，附带已关联的工单。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.order_no, o.quantity, o.amount_cents, o.created_at,
                   p.name AS product_name, p.color AS product_color
              FROM orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s
             ORDER BY o.created_at DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT order_no, id, status, issue_type
              FROM work_orders
             WHERE visitor_id = %s
             ORDER BY created_at ASC
            """,
            (visitor_id,),
        )
        work_order_rows = cur.fetchall()

    by_order: dict[str, list[dict]] = {}
    for wo in work_order_rows:
        by_order.setdefault(wo["order_no"], []).append(
            {
                "work_order_no": wo["id"],
                "status": wo["status"],
                "issue_type": wo["issue_type"],
            }
        )

    return [
        {
            "order_no": r["order_no"],
            "product": (
                f"{r['product_name']}（{r['product_color']}）"
                if r["product_name"]
                else None
            ),
            "quantity": r["quantity"],
            "amount_yuan": r["amount_cents"] / 100,
            "created_at": r["created_at"].isoformat(),
            "work_orders": by_order.get(r["order_no"], []),
        }
        for r in rows
    ]


def get_order(visitor_id: str, order_no: str) -> dict | None:
    for order in list_orders(visitor_id):
        if order["order_no"] == order_no:
            return order
    return None


def product_name_for_order(visitor_id: str, order_no: str | None) -> str | None:
    """该顾客此订单关联的商品名，用于确认卡上核对。无订单时返回 None。"""
    if order_no is None:
        return None
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.name, p.color
              FROM orders o
              JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s AND o.order_no = %s
             LIMIT 1
            """,
            (visitor_id, order_no),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT p.name, p.color
                  FROM work_orders o
                  JOIN products p ON p.id = o.product_id
                 WHERE o.visitor_id = %s AND o.order_no = %s
                 ORDER BY o.created_at DESC LIMIT 1
                """,
                (visitor_id, order_no),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return f"{row['name']}（{row['color']}）"


# ---------- 工单 ----------


def find_open_work_order(
    visitor_id: str, order_no: str | None, issue_type: str
) -> dict | None:
    """查找同一顾客、同一订单、同一类型且未完结的工单，用于防重复建单。

    无订单的工单（咨询/投诉建议/其他）按「顾客 + 类型」判重，否则 NULL
    与 NULL 不相等，会导致无限重复建单。
    """
    with connect() as conn, conn.cursor() as cur:
        if order_no is None:
            cur.execute(
                """
                SELECT id, status, created_at
                  FROM work_orders
                 WHERE visitor_id = %s AND order_no IS NULL AND issue_type = %s
                   AND status IN %s
                 ORDER BY id ASC LIMIT 1
                """,
                (visitor_id, issue_type, WORK_ORDER_OPEN_STATUSES),
            )
        else:
            cur.execute(
                """
                SELECT id, status, created_at
                  FROM work_orders
                 WHERE visitor_id = %s AND order_no = %s AND issue_type = %s
                   AND status IN %s
                 ORDER BY id ASC LIMIT 1
                """,
                (visitor_id, order_no, issue_type, WORK_ORDER_OPEN_STATUSES),
            )
        return cur.fetchone()


def create_work_order(
    visitor_id: str,
    order_no: str | None,
    issue_type: str,
    description: str,
) -> str:
    """新建工单，返回对外工单号。归属与状态由服务端强制，不由模型传入。

    order_no 为空时表示不与具体订单关联（咨询、投诉建议类）。
    """
    work_order_no = "WO" + uuid.uuid4().hex[:10].upper()

    with connect() as conn, conn.cursor() as cur:
        product_id = _resolve_product_id(cur, visitor_id, order_no)
        ts = now()
        cur.execute(
            """
            INSERT INTO work_orders
                (id, visitor_id, order_no, product_id, status, issue, issue_type,
                 priority, source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                work_order_no,
                visitor_id,
                order_no,
                product_id,
                "待处理",
                description,
                issue_type,
                "普通",
                "ai",
                ts,
                ts,
            ),
        )
    return work_order_no


def _resolve_product_id(cur, visitor_id: str, order_no: str | None) -> str | None:
    """商品先从既有工单继承，其次从订单表取；都没有则留空。"""
    if order_no is None:
        return None

    cur.execute(
        """
        SELECT product_id FROM work_orders
         WHERE visitor_id = %s AND order_no = %s AND product_id IS NOT NULL
         ORDER BY created_at DESC LIMIT 1
        """,
        (visitor_id, order_no),
    )
    row = cur.fetchone()
    if row:
        return row["product_id"]

    cur.execute(
        "SELECT product_id FROM orders WHERE visitor_id = %s AND order_no = %s LIMIT 1",
        (visitor_id, order_no),
    )
    row = cur.fetchone()
    return row["product_id"] if row else None


def list_work_orders(visitor_id: str) -> list[dict]:
    """该顾客全部工单，最近更新在前，附带商品名用于核对。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.order_no, o.issue_type, o.status, o.issue,
                   o.created_at, o.updated_at,
                   p.name AS product_name, p.color AS product_color
              FROM work_orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s
             ORDER BY o.updated_at DESC
            """,
            (visitor_id,),
        )
        rows = cur.fetchall()

    return [
        {
            "work_order_no": r["id"],
            "order_no": r["order_no"],
            "issue_type": r["issue_type"],
            "status": r["status"],
            "description": r["issue"],
            "product": (
                f"{r['product_name']}（{r['product_color']}）"
                if r["product_name"]
                else None
            ),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


def get_work_order(visitor_id: str, work_order_no: str) -> dict | None:
    for order in list_work_orders(visitor_id):
        if order["work_order_no"] == work_order_no:
            return order
    return None


def find_work_order(visitor_id: str, order_no: str) -> dict | None:
    """按订单号查该访客名下的工单。访客不匹配时返回 None，
    不区分「不存在」与「不属于你」。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.id, o.order_no, o.status, o.issue,
                   o.created_at, o.updated_at,
                   p.name AS product_name, p.color AS product_color
              FROM work_orders o
              LEFT JOIN products p ON p.id = o.product_id
             WHERE o.visitor_id = %s AND o.order_no = %s
             LIMIT 1
            """,
            (visitor_id, order_no),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "work_order_id": row["id"],
        "order_no": row["order_no"],
        "status": row["status"],
        "issue": row["issue"],
        "product": (
            f"{row['product_name']}（{row['product_color']}）"
            if row["product_name"]
            else None
        ),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


# ---------- 工单草稿 ----------


def create_draft(
    visitor_id: str,
    conversation_id: str,
    order_no: str | None,
    issue_type: str,
    description: str,
) -> dict:
    """暂存待确认的建单草稿。草稿不产生工单，只是给前端渲染确认卡。"""
    draft_id = "drf-" + uuid.uuid4().hex
    created = now()
    expires = created + timedelta(minutes=DRAFT_TTL_MINUTES)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_order_drafts
                (id, visitor_id, conversation_id, order_no, issue_type,
                 description, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                draft_id,
                visitor_id,
                conversation_id,
                order_no,
                issue_type,
                description,
                created,
                expires,
            ),
        )
    return {
        "draft_id": draft_id,
        "order_no": order_no,
        "issue_type": issue_type,
        "description": description,
        "expires_at": expires.isoformat(),
        "product": product_name_for_order(visitor_id, order_no),
    }


def get_draft(draft_id: str, visitor_id: str) -> dict | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, conversation_id, order_no, issue_type, description, expires_at
              FROM work_order_drafts
             WHERE id = %s AND visitor_id = %s
            """,
            (draft_id, visitor_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "draft_id": row["id"],
        "conversation_id": row["conversation_id"],
        "order_no": row["order_no"],
        "issue_type": row["issue_type"],
        "description": row["description"],
        "expires_at": row["expires_at"].isoformat(),
        "product": product_name_for_order(visitor_id, row["order_no"]),
    }


def delete_draft(draft_id: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM work_order_drafts WHERE id = %s", (draft_id,))


def purge_expired_drafts() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM work_order_drafts WHERE expires_at < %s", (now(),))
        return cur.rowcount
