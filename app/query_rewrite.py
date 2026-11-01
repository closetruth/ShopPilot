"""Query 标准化：售后口语到政策术语的规则词表改写。

顾客不会用政策术语提问——「快递好久没动」「东西坏了想退」与知识库
的「物流延迟」「质量问题退货」隔着语义鸿沟，实测这层差距正好横跨
0.65 拒答闸（r14 已验证阈值两侧没有可靠分界线，措辞问题要在措辞层
解决）。改写是拼接不是替换：保留顾客原话（嵌入信号与具体信息），
追加标准术语（补召回信号）。

设计：docs/superpowers/specs/2026-10-31_r16-query-rewrite-design.md
维护规则见 tests/test_query_rewrite.py 的增益门槛。
"""

from __future__ import annotations

import re

# (触发正则, 追加术语)。术语锚定政策小节名，新增词条必须过增益验证。
REWRITE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"坏了|有问题|有毛病|瑕疵|故障"), "质量问题退换货"),
    (re.compile(r"破了|破损|摔坏|压坏"), "破损 质量问题退换货"),
    (re.compile(r"不想要了|后悔了|拍错了"), "七天无理由退货"),
    (re.compile(r"没动静|没更新|没动|不动了|好久没到|太慢了|迟迟不到"), "物流延迟 停滞 补偿"),
    (re.compile(r"丢件|丢了|没收到货|没收到"), "丢件处理 理赔"),
    (re.compile(r"退钱|退回来|把钱退了|到账"), "退款金额与时效"),
    (re.compile(r"运费谁承担|不想出运费|运费我不要出|运费我不想出"), "运费承担"),
    (re.compile(r"换个|换一件|换货"), "换货与维修"),
    (re.compile(r"开票|发票"), "发票 开具条件"),
    (re.compile(r"补差价|降价了|保价"), "差价计算"),
    (re.compile(r"赠品"), "赠品 商品完好"),
    (re.compile(r"投诉你们|给个说法"), "投诉处理原则 转人工"),
]


def rewrite_query(query: str) -> tuple[str, list[str]]:
    """词表改写：返回 (改写后 query, 命中词条列表)；无命中返回 (原话, [])。

    多词条命中时术语按词表序拼接去重——追加部分是检索信号，顺序
    无语义含义，稳定即可。
    """
    seen_terms: list[str] = []
    for pattern, terms in REWRITE_RULES:
        if pattern.search(query):
            for term in terms.split():
                if term not in seen_terms:
                    seen_terms.append(term)

    if not seen_terms:
        return query, []

    return f"{query} {' '.join(seen_terms)}", seen_terms
