"""示例 MCP 服务器：物流时效查询。

stdio 传输下 stdout 是协议通道，任何日志都必须走 stderr，
否则会把 JSON-RPC 流冲掉。
"""

import logging
import sys

from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO)

mcp = FastMCP("logistics")

CARRIERS = {
    "SF": ("顺丰", 2),
    "YTO": ("圆通", 4),
    "ZTO": ("中通", 4),
    "JD": ("京东物流", 1),
}


@mcp.tool()
def query_shipping_eta(order_no: str, carrier: str | None = None) -> dict:
    """按订单号估算物流时效。carrier 不传时按默认承运商估算。

    Args:
        order_no: 订单号
        carrier: 承运商代码，可选 SF / YTO / ZTO / JD
    """
    code = (carrier or "SF").upper()
    if code not in CARRIERS:
        return {
            "found": False,
            "reason": "unsupported_carrier",
            "note": f"不支持的承运商代码：{carrier}，可用值：{', '.join(CARRIERS)}",
        }

    name, days = CARRIERS[code]
    return {
        "found": True,
        "order_no": order_no,
        "carrier": name,
        "eta_days": days,
        "note": f"{name}预计 {days} 天送达，偏远地区可能顺延 1 天。",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")