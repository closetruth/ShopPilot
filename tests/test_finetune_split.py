"""分层抽样与数据增强测试：纯函数，不连库不调模型。

这几条断言保护的是「指标不会虚高」：切分单位必须是 query 而不是 pair，
dev/test 不能掺增强样本，hard negative 必须来自候选而不是随机文档。
"""

import json

from scripts.finetune_dataset import (
    GATE_MIN_POSITIVE,
    augment,
    build_pairs,
    export_jsonl,
    split,
)


def _faq(faq_id, category="退货", title="退货条件", content="七天无理由。"):
    return {
        "id": faq_id,
        "doc_id": "P-RET-001",
        "title": title,
        "category": category,
        "section": "条件",
        "content": content,
    }


def _row(query, label="doc", faq_id="P-RET-001-1", category="退货",
         candidates=None, gold=None):
    return {
        "query": query,
        "label": label,
        "faq_id": faq_id,
        "gold": gold if gold is not None else (_faq(faq_id, category) if faq_id else None),
        "category": category,
        "candidates": candidates or [],
        "low_conf_id": None,
    }


def _cand(faq_id, score=0.8, title="退货条件", content="七天无理由。", category="退货"):
    return {
        "faq_id": faq_id,
        "title": title,
        "category": category,
        "section": "条件",
        "content": content,
        "score": score,
    }


def test_split_is_stratified_by_category_and_label():
    rows = [_row(f"退货问题{i}") for i in range(10)]
    rows += [_row(f"运费问题{i}", faq_id="P-SHIP-001-1", category="运费",
                  gold=_faq("P-SHIP-001-1", "运费")) for i in range(10)]
    out = split(rows, seed=1)

    total = sum(len(v) for v in out.values())
    assert total == len(rows)
    for name in ("train", "dev", "test"):
        cats = {row["category"] for row in out[name]}
        assert cats == {"退货", "运费"}, f"{name} 丢了类别：{cats}"


def test_split_is_reproducible_for_same_seed():
    rows = [_row(f"问题{i}") for i in range(20)]
    a = split(rows, seed=7)
    b = split(rows, seed=7)
    assert [[r["query"] for r in a[n]] for n in ("train", "dev", "test")] == [
        [r["query"] for r in b[n]] for n in ("train", "dev", "test")
    ]
    c = split(rows, seed=8)
    assert [r["query"] for r in a["train"]] != [r["query"] for r in c["train"]]


def test_small_stratum_goes_all_to_train():
    rows = [_row("退货怎么弄"), _row("退货要几天")]
    out = split(rows, seed=1)
    assert len(out["train"]) == 2
    assert out["dev"] == [] and out["test"] == []


def test_split_keeps_query_positives_and_negatives_together():
    # 同一句话的正例与负例都在一个 row 里，按 query 切天然不会跨集
    rows = [_row(f"退货问题{i}", candidates=[_cand("P-RET-002-1")]) for i in range(9)]
    out = split(rows, seed=3)
    for name in ("train", "dev", "test"):
        queries = {row["query"] for row in out[name]}
        assert len(queries) == len(out[name]), f"{name} 出现重复 query，说明切分粒度错了"


def test_augment_builds_positive_hard_negative_and_synonym():
    rows = [_row("退货怎么弄", candidates=[_cand("P-RET-001-1"), _cand("P-RET-002-1", 0.7)])]
    pairs, stats = augment(rows, traces=[])

    labels = [(p["label"], p["source"]) for p in pairs]
    assert (1, "human") in labels
    assert (0, "hard_negative") in labels
    assert stats["positive"] == 1
    assert stats["hard_neg"] == 1
    assert stats["synonym"] >= 1

    # 金标条款不能被自己当成负例
    negs = [p for p in pairs if p["label"] == 0]
    assert all(p["faq_id"] != "P-RET-001-1" for p in negs)


def test_augment_uses_trace_rewrite_as_extra_positive():
    rows = [_row("能退吗")]
    traces = [{"message": "能退吗", "resolved_query": "退货的条件是什么"}]
    pairs, stats = augment(rows, traces=traces)
    assert stats["trace"] == 1
    rewritten = [p for p in pairs if p["source"] == "trace_rewrite"][0]
    assert rewritten["query"] == "退货的条件是什么"
    assert rewritten["label"] == 1


def test_augment_ignores_trace_that_did_not_change_query():
    rows = [_row("能退吗")]
    traces = [{"message": "能退吗", "resolved_query": "能退吗？"}]
    _, stats = augment(rows, traces=traces)
    assert stats["trace"] == 0


def test_augment_turns_noise_into_negative_only():
    rows = [_row("我要投诉你们", label="noise", faq_id=None, category="unknown",
                 gold=None, candidates=[_cand("P-RET-001-1")])]
    pairs, stats = augment(rows, traces=[])
    assert stats["positive"] == 0
    assert stats["noise_neg"] == 1
    assert all(p["label"] == 0 for p in pairs)


def test_augment_skips_missing_label():
    # 知识库缺失不是相关性信号，不能造出任何训练对
    rows = [_row("你们支持花呗吗", label="missing", faq_id=None, category="unknown",
                 gold=None, candidates=[_cand("P-RET-001-1")])]
    pairs, stats = augment(rows, traces=[])
    assert pairs == []
    assert stats == {"positive": 0, "trace": 0, "synonym": 0, "hard_neg": 0, "noise_neg": 0}


def test_dev_and_test_carry_no_augmented_pairs():
    rows = [_row(f"退货问题{i}", candidates=[_cand("P-RET-002-1")]) for i in range(12)]
    splits = split(rows, seed=5)
    built = build_pairs(splits, traces=[{"message": "退货问题0", "resolved_query": "退货流程"}])

    for name in ("dev", "test"):
        assert all(p["source"] == "human" for p in built[name])
        assert all(p["label"] == 1 for p in built[name])

    train_sources = {p["source"] for p in built["train"]}
    assert "hard_negative" in train_sources


def test_export_jsonl_writes_one_object_per_line(tmp_path):
    path = tmp_path / "train.jsonl"
    rows = [{"query": "退货", "doc": "退货条件：七天无理由。", "label": 1}]
    export_jsonl(rows, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["label"] == 1


def test_gate_threshold_is_80():
    assert GATE_MIN_POSITIVE == 80