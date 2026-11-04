"""数据清洗纯函数测试：不连数据库、不调模型，只断言规则与计数。

清洗的每一步都要能被单独解释——线上低置信度数据的清洗比例会直接写进
数据集报告，数字对不上就没法判断「微调为什么没效果」。
"""

from scripts.finetune_dataset import (
    STAT_KEYS,
    clean,
    detect_noise,
    mask_pii,
    normalize,
)


def _row(query, source="low_conf", **extra):
    return {"query": query, "source": source, "low_conf_id": None,
            "retrieved": [], "expected_faq_id": None, **extra}


def test_normalize_flat_width_and_spaces():
    assert normalize("  退货　怎么弄  ") == "退货 怎么弄"
    assert normalize("ＡＢＣ１２３") == "ABC123"


def test_mask_pii_replaces_order_phone_and_long_digits():
    text, changed = mask_pii("订单 ORDER20240001 手机 13800138000")
    assert changed is True
    assert "<ORDER>" in text
    assert "<PHONE>" in text
    assert "20240001" not in text


def test_mask_pii_leaves_normal_text_alone():
    text, changed = mask_pii("退货要几天")
    assert changed is False
    assert text == "退货要几天"


def test_detect_noise_by_intent_rules():
    # 寒暄、情绪投诉、订单查询都不该进政策检索训练集
    assert detect_noise("你好") is True
    assert detect_noise("我要投诉你们") is True
    assert detect_noise("我的订单到哪了") is True
    # 政策问法保留
    assert detect_noise("退货怎么弄") is False
    assert detect_noise("运费谁出") is False
    # 规则分不出类的保留给人工，宁可多标不可误删
    assert detect_noise("能便宜点吗") is False


def test_clean_merges_exact_duplicates_across_sources():
    rows = [
        _row("退货怎么弄", "low_conf", low_conf_id=1),
        _row("退货怎么弄？", "r13", expected_faq_id="P-RET-001-1"),
    ]
    cleaned, stats = clean(rows)
    assert len(cleaned) == 1
    assert cleaned[0]["source"] == "both"
    assert cleaned[0]["low_conf_id"] == 1
    assert cleaned[0]["expected_faq_id"] == "P-RET-001-1"
    assert stats["dup_exact"] == 1
    assert stats["merged"] == 1


def test_clean_drops_near_duplicates():
    rows = [_row("退货怎么弄"), _row("退货怎么弄呀")]
    cleaned, stats = clean(rows)
    assert len(cleaned) == 1
    assert stats["dup_near"] == 1


def test_clean_drops_too_short_and_truncates_long():
    long_query = "退" * 300
    rows = [_row("嗯"), _row(long_query), _row("退货要几天")]
    cleaned, stats = clean(rows)
    assert stats["drop_len"] == 1
    assert stats["truncated"] == 1
    kept = [row["query"] for row in cleaned]
    assert "退货要几天" in kept
    assert all(len(row["query"]) <= 200 for row in cleaned)


def test_clean_marks_noise_after_pii_masking():
    # 噪声判定必须在掩码前完成，否则订单号被吃掉就判不出业务问题了
    rows = [_row("我的 ORDER20240001 到哪了"), _row("退货要几天")]
    cleaned, stats = clean(rows)
    assert stats["noise"] == 1
    assert stats["masked"] == 0
    assert [row["query"] for row in cleaned] == ["退货要几天"]


def test_clean_masks_pii_in_kept_rows():
    rows = [_row("手机号 13800138000 能改吗")]
    cleaned, stats = clean(rows)
    assert stats["masked"] == 1
    assert "<PHONE>" in cleaned[0]["query"]


def test_stats_keys_are_stable():
    _, stats = clean([_row("退货怎么弄")])
    assert set(stats) == set(STAT_KEYS)