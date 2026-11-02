"""权限模型：调用者白名单 + 数据范围 + 风险等级。

数据隔离仍由工具实现自己用 visitor_id 保证，权限层只回答
「这个调用者有没有资格调这个工具」。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.tools.registry import ToolSpec

CallerKind = Literal["agent", "admin", "confirm", "system"]


class Denied(Exception):
    """调用被权限层拒绝。消息会回给模型，不能含内部细节。"""


@dataclass(frozen=True)
class Caller:
    kind: CallerKind
    visitor_id: str | None = None


def authorize(spec: "ToolSpec", caller: Caller) -> None:
    """三层判定的前两层；第三层（destructive 必须确认）在注册期拦。"""
    if caller.kind not in spec.allowed_callers:
        raise Denied("调用者不在白名单")
    if spec.scope == "visitor" and not caller.visitor_id:
        raise Denied("缺少访客身份")