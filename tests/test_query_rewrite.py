"""r16 Task 1：query 标准化词表的验证测试。

词表是纯函数零依赖；增益验证用真嵌入 API（skipif 保护），每条词条
必须证明自己：原话不过闸时改写要把它带过闸，或至少显著提升。
术语还必须真实出现在 FAQ 内容里——政策文档更新后词表与条款脱节时
测试会红。
"""

import os

import pytest

from app.retrieval.query_rewrite import REWRITE_RULES, rewrite_query


def test_no_match_returns_original():
    """无命中的 query 原样透传，词条列表为空。"""
    q = "明天几点开会"
    rewritten, terms = rewrite_query(q)
    assert rewritten == q
    assert terms == []


def test_match_appends_not_replaces():
    """拼接不替换：原话保留（含具体信息如快递名），追加术语。"""
    rewritten, terms = rewrite_query("快递好久没动")
    assert rewritten.startswith("快递好久没动")
    assert rewritten != "快递好久没动"
    assert terms, "命中词条应有记录"


def test_multiple_terms_dedup():
    """多词条命中的追加术语去重、按词表序拼接。"""
    rewritten, terms = rewrite_query("东西坏了想退钱")
    # 「坏了」→质量问题、「退钱」→退款金额与时效，两个词条都应命中
    assert len(terms) >= 2
    appended = rewritten[len("东西坏了想退钱"):].strip()
    seen = []
    for t in appended.split():
        assert t not in seen, f"术语重复：{t}"
        seen.append(t)


@pytest.mark.parametrize("query,expected_term", [
    ("快递好久没动", "物流延迟"),
    ("东西坏了想退", "质量问题"),
    ("不想要了能退吗", "七天无理由退货"),
    ("退款多久到账", "退款金额与时效"),
    ("能不能开发票", "开具条件"),
    ("买完就降价了能补差价吗", "差价"),
])
def test_rule_hits(query, expected_term):
    _, terms = rewrite_query(query)
    assert any(expected_term in t for t in terms), f"「{query}」应命中含「{expected_term}」的词条"


def test_aftersale_projection_matches_wordlist():
    """r17 单一来源守卫：意图层的售后词与词表 aftersale 规则同步。

    aftersale 规则里的每个触发分支都必须被派生正则命中——将来有人
    改了词表忘了派生逻辑（或反过来），这条会红。
    """
    from app.retrieval.query_rewrite import REWRITE_RULES, aftersale_pattern

    pattern = aftersale_pattern()
    for rule in REWRITE_RULES:
        if not rule.aftersale:
            continue
        # 每个触发分支取一个代表词验证
        for branch in rule.pattern.pattern.split("|"):
            assert pattern.search(f"测试{branch}用例"), (
                f"规则「{rule.name}」的触发词「{branch}」未进入意图派生正则"
            )


@pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY", "").strip(),
    reason="未配置 SILICONFLOW_API_KEY",
)
def test_terms_exist_in_faq_content():
    """词条术语必须真实出现在 FAQ 内容/标题/小节里——防词表与条款脱节。"""
    from app.core import store

    store.init_db()
    corpus = "\n".join(
        f"{r['title']} {r['section']} {r['content']}" for r in store.list_faq()
    )
    for rule in REWRITE_RULES:
        for term in rule.terms.split():
            assert term in corpus, f"术语「{term}」在 FAQ 全文中不存在，词表与条款脱节"


@pytest.mark.skipif(
    not os.getenv("SILICONFLOW_API_KEY", "").strip(),
    reason="未配置 SILICONFLOW_API_KEY",
)
def test_terms_improve_or_keep_retrieval():
    """增益门槛：每条词条至少满足其一——
    - 原话样本不过闸（<0.65）时改写后过闸
    - 或改写后分数提升 ≥ 0.05
    不满足的词条是噪声源，必须调整或移除。
    """
    from app.retrieval import vector_store

    samples = {  # 每条词条一个代表性口语样本
        "物流延迟": "快递好久没动",
        "质量问题": "东西坏了想退",
        "七天无理由退货": "不想要了能退吗",
        "退款时效": "钱退回来",
        "运费承担": "运费我不想出",
        "换货与维修": "怎么换个新的",
        "发票开具": "帮我开个发票",
        "差价": "买完就降价了",
    }
    for term_group, sample in samples.items():
        base = vector_store.search(sample, "", top_k=1)
        rewritten, terms = rewrite_query(sample)
        assert terms, f"「{sample}」应触发词条"
        after = vector_store.search(rewritten, "", top_k=1)
        s1 = base[0]["score"] if base else 0.0
        s2 = after[0]["score"] if after else 0.0
        passed_gate = (
            (s1 < 0.65 and s2 >= 0.65)
            or (s2 - s1 >= 0.05)
        )
        assert passed_gate, (
            f"词条「{term_group}」样本「{sample}」：{s1:.3f}→{s2:.3f}，"
            "既没过闸也没显著提升，词条无效"
        )