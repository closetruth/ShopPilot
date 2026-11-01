"""Query 标准化：售后口语到政策术语的规则词表改写。

顾客不会用政策术语提问——「快递好久没动」「东西坏了想退」与知识库
的「物流延迟」「质量问题退货」隔着语义鸿沟，实测这层差距正好横跨
0.65 拒答闸（r14 已验证阈值两侧没有可靠分界线，措辞问题要在措辞层
解决）。改写是拼接不是替换：保留顾客原话（嵌入信号与具体信息），
追加标准术语（补召回信号）。

设计：docs/superpowers/specs/2026-10-31_r16-query-rewrite-design.md
维护规则见 tests/test_query_rewrite.py 的增益门槛。

r17 起本模块是口语词的**唯一来源**：意图分类的售后诉求词
（aftersale_pattern）从「情况陈述类」规则派生，两处不会漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RewriteRule:
    """一条改写规则。

    name 是类别名（测试与日志用它指代）；aftersale=True 的规则表示
    「顾客在客观陈述出了什么状况」——这类触发词同时是意图分类里
    钉 knowledge 的依据（LLM 兜底会把「破了」理解成情绪）。
    """

    name: str
    pattern: re.Pattern[str]
    terms: str
    aftersale: bool = False


REWRITE_RULES: list[RewriteRule] = [
    RewriteRule("质量问题", re.compile(r"坏了|有问题|有毛病|瑕疵|故障|漏了|开裂|掉漆|失灵|用不了"), "质量问题退换货", aftersale=True),
    RewriteRule("破损", re.compile(r"破了|破损|摔坏|压坏"), "破损 质量问题退换货", aftersale=True),
    RewriteRule("七天无理由", re.compile(r"不想要了|后悔了|拍错了"), "七天无理由退货"),
    RewriteRule("物流延迟", re.compile(r"没动静|没更新|没动|不动了|好久没到|太慢了|迟迟不到"), "物流延迟 停滞 补偿", aftersale=True),
    RewriteRule("丢件", re.compile(r"丢件|丢了|没收到货|没收到"), "丢件处理 理赔", aftersale=True),
    RewriteRule("退款时效", re.compile(r"退钱|退回来|把钱退了|到账"), "退款金额与时效"),
    RewriteRule("运费承担", re.compile(r"运费谁承担|不想出运费|运费我不要出|运费我不想出"), "运费承担"),
    RewriteRule("换货", re.compile(r"换个|换一件|换货"), "换货与维修"),
    RewriteRule("发票", re.compile(r"开票|发票"), "发票 开具条件"),
    RewriteRule("差价", re.compile(r"补差价|降价了|保价"), "差价计算"),
    RewriteRule("赠品", re.compile(r"赠品"), "赠品 商品完好"),
    RewriteRule("投诉", re.compile(r"投诉你们|给个说法"), "投诉处理原则 转人工"),
]


def aftersale_pattern() -> re.Pattern[str]:
    """意图分类用：全部 aftersale 规则触发词的合并。

    意图层拿它把「包装破了」「快递好久没动」这类客观状况陈述直接钉
    knowledge（零 LLM）——触发词只在这一处维护，加词条时意图与改写
    自动同步。
    """
    return re.compile("|".join(rule.pattern.pattern for rule in REWRITE_RULES if rule.aftersale))


def rewrite_query(query: str) -> tuple[str, list[str]]:
    """词表改写：返回 (改写后 query, 命中词条列表)；无命中返回 (原话, [])。

    多词条命中时术语按词表序拼接去重——追加部分是检索信号，顺序
    无语义含义，稳定即可。
    """
    seen_terms: list[str] = []
    for rule in REWRITE_RULES:
        if rule.pattern.search(query):
            for term in rule.terms.split():
                if term not in seen_terms:
                    seen_terms.append(term)

    if not seen_terms:
        return query, []

    return f"{query} {' '.join(seen_terms)}", seen_terms
