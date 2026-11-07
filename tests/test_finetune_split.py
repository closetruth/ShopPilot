"""分层抽样与数据增强测试：纯函数，不连库不调模型。

这几条断言保护的是「指标不会虚高」：切分单位必须是 query 而不是 pair，
dev/test 不能掺增强样本，hard negative 必须来自候选而不是随机文档。
另外守住一条底线：训练集必须有负例——全正例上 BCE 的最优解是给所有
输入打高分，loss 会降但模型学不到区分。
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
        "doc_id": faq_id.rsplit("-", 1)[0],
        "title": title,
        "category": category,
        "section": "条件",
        "content": content,
        "enabled": 1,
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


def _pool(faq_id, category="退货", title="退货条件", content="七天无理由。",
          doc_id=None):
    """全池快照的一条，格式与 load_faq_pool 的产物一致。"""
    return {
        "faq_id": faq_id,
        "doc_id": doc_id or faq_id.rsplit("-", 1)[0],
        "doc": f"{title}：{content}",
        "category": category,
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


def test_augment_turns_missing_into_negative_pairs():
    # missing 不是相关性信号，但它是「该拒」的信号：给定候选条款必须判 0。
    # 旧版这里返回空，导致闸的负例只剩 noise 一路，阈值扫不出分界点。
    rows = [_row("你们支持花呗吗", label="missing", faq_id=None, category="unknown",
                 gold=None, candidates=[_cand("P-RET-001-1")])]
    pairs, stats = augment(rows, traces=[])
    assert stats["positive"] == 0
    assert stats["missing_neg"] == 1
    assert all(p["label"] == 0 for p in pairs)
    assert pairs[0]["faq_id"] == "P-RET-001-1"


def test_augment_falls_back_to_random_negatives_without_candidates():
    """候选快照缺失时，正例仍要有负例可学。

    没有这一路，训练集就是全正例：BCE 的最优解是给所有输入打高分，
    loss 漂亮下降但模型没有任何区分能力。
    """
    pool = [_pool(f"P-RET-00{i}-1") for i in range(1, 6)]
    rows = [_row("退货怎么弄", candidates=[])]
    pairs, stats = augment(rows, traces=[], pool=pool)

    assert stats["random_neg"] == 2
    negs = [p for p in pairs if p["label"] == 0]
    assert len(negs) == 2
    assert all(p["source"] == "random_negative" for p in negs)
    # 金标条款不能被抓来当负例
    assert all(p["faq_id"] != "P-RET-001-1" for p in negs)


def test_augment_does_not_top_up_when_candidates_suffice():
    pool = [_pool(f"P-RET-00{i}-1") for i in range(1, 6)]
    rows = [_row("退货怎么弄", candidates=[_cand("P-RET-002-1"), _cand("P-RET-003-1", 0.6)])]
    _, stats = augment(rows, traces=[], pool=pool)

    assert stats["hard_neg"] == 2
    assert stats["random_neg"] == 0


def test_augment_random_negatives_are_reproducible():
    pool = [_pool(f"P-RET-00{i}-1") for i in range(1, 9)]
    rows = [_row(f"退货问题{i}", candidates=[]) for i in range(4)]
    first, _ = augment(rows, traces=[], pool=pool, seed=7)
    second, _ = augment(rows, traces=[], pool=pool, seed=7)
    assert [p["faq_id"] for p in first] == [p["faq_id"] for p in second]


def test_random_negatives_exclude_gold_document():
    """同一篇文档的其他小节不能当负例：往往部分相关，标 0 会教坏模型。"""
    pool = [
        _pool("P-RET-001-1"),
        _pool("P-RET-001-2"),
        _pool("P-RET-001-3"),
        _pool("P-SHIP-001-1", category="运费"),
        _pool("P-PAY-001-1", category="支付"),
    ]
    rows = [_row("退货怎么弄", candidates=[])]
    pairs, _ = augment(rows, traces=[], pool=pool)

    negs = {p["faq_id"] for p in pairs if p["label"] == 0}
    assert negs == {"P-SHIP-001-1", "P-PAY-001-1"}


def test_noise_falls_back_to_random_negative_without_candidates():
    pool = [_pool("P-RET-002-1"), _pool("P-SHIP-001-1", category="运费")]
    rows = [_row("我要投诉你们", label="noise", faq_id=None, category="unknown",
                 gold=None, candidates=[])]
    pairs, stats = augment(rows, traces=[], pool=pool)

    assert stats["noise_neg"] == 1
    assert all(p["label"] == 0 for p in pairs)


def test_dev_and_test_carry_no_augmented_pairs():
    rows = [_row(f"退货问题{i}", candidates=[_cand("P-RET-002-1")]) for i in range(12)]
    splits = split(rows, seed=5)
    built = build_pairs(splits, traces=[{"message": "退货问题0", "resolved_query": "退货流程"}])

    for name in ("dev", "test"):
        assert all(p["source"] == "human" for p in built[name])
        assert all(p["label"] == 1 for p in built[name])

    train_sources = {p["source"] for p in built["train"]}
    assert "hard_negative" in train_sources


def test_hard_negatives_prefer_other_documents():
    """同篇文档的其他小节要排在候选末尾。

    候选来自线上检索，往往把同一份政策的第 2 节一起召回。把第 2 节标成 0
    会教模型压掉「近义但正确」的条款，线上表现是该答的答不上——比召回不
    到更难查。所以只有整批候选都同篇时才用它们。
    """
    gold = _faq("P-RET-001-1")
    rows = [_row("退货怎么退", gold=gold, candidates=[
        _cand("P-RET-001-2"),          # 同篇，另一节
        _cand("P-SHP-001-1"),          # 不同篇
    ])]
    pool = [
        {"faq_id": "P-RET-001-1", "doc_id": "P-RET-001", "doc": "七天无理由。"},
        {"faq_id": "P-RET-001-2", "doc_id": "P-RET-001", "doc": "退货运费。"},
        {"faq_id": "P-SHP-001-1", "doc_id": "P-SHP-001", "doc": "满 99 包邮。"},
    ]
    pairs = build_pairs({"train": rows, "dev": [], "test": []}, traces=[], pool=pool)["train"]

    hard = [p for p in pairs if p["source"] == "hard_negative"]
    assert [p["faq_id"] for p in hard] == ["P-SHP-001-1"]


def test_candidates_fall_back_to_stored_retrieval(monkeypatch):
    """快照文件缺失时，候选要从库里 `retrieved` 还原。

    候选为空 → hard_neg 归零 → 模型只学「主题不匹配判 0」，线上遇到同主题
    干扰就全给高分（实测 abstain 全部不拒）。而 `retrieved` 本来就在 MySQL
    里，没理由再要求人工拷一份 candidates.jsonl。
    """
    from app.core import store
    from app.repo import finetune as finetune_repo
    import scripts.finetune_dataset as fd

    faq = [
        _faq("P-RET-001-1"),
        _faq("P-SHP-001-1", category="物流", title="运费规则", content="满 99 包邮。"),
    ]
    monkeypatch.setattr(store, "list_faq", lambda _limit: faq)
    monkeypatch.setattr(
        fd,
        "fetch_low_conf",
        lambda limit=None: [
            {"query": "退货怎么退", "source": "low_conf", "low_conf_id": 1,
             "retrieved": ["P-RET-001-1"], "expected_faq_id": None},
            {"query": "包裹在哪", "source": "low_conf", "low_conf_id": 2,
             "retrieved": ["P-SHP-001-1", "P-GONE-9"], "expected_faq_id": None},
        ],
    )

    mapping = fd._candidates_from_retrieved()

    assert mapping[finetune_repo.query_hash("退货怎么退")][0]["faq_id"] == "P-RET-001-1"
    # 库里已停用/不存在的 id 必须丢掉，不能让幽灵条款混进训练
    assert [c["faq_id"] for c in mapping[finetune_repo.query_hash("包裹在哪")]] == [
        "P-SHP-001-1"
    ]


def test_missing_labels_become_training_negatives():
    """missing 标注必须进训练集，否则闸分不开「该答」与「该拒」。

    missing 是「看着像政策问题、但库里确实没有条款能答」。它和 noise
    在精排眼里是同一种输入：给定任何条款都该判 0。原先只有 noise 进
    训练，dev 上模型对没有答案的问题照样打高分，阈值怎么扫都分不开。
    """
    pool = [_pool("P-RET-002-1"), _pool("P-SHIP-001-1", category="运费")]
    rows = [
        _row("我的包裹到哪了", label="missing", faq_id=None, category="unknown",
             gold=None, candidates=[]),
        _row("发票抬头能改吗", label="missing", faq_id=None, category="unknown",
             gold=None, candidates=[]),
    ]
    pairs, stats = augment(rows, traces=[], pool=pool, negatives_per_positive=2)

    assert stats["missing_neg"] == 4
    assert stats["noise_neg"] == 0
    assert all(p["label"] == 0 for p in pairs)
    assert all(p["source"] == "missing_negative" for p in pairs)
    # 同一条问题要配到不同条款，否则等于只教它认一条文档
    for query in {p["query"] for p in pairs}:
        assert len({p["faq_id"] for p in pairs if p["query"] == query}) == 2


def test_missing_without_pool_is_skipped_not_crashed():
    """池为空时抽不出负例，只能跳过；不能因此把 missing 变成正例。"""
    rows = [
        _row("我的包裹到哪了", label="missing", faq_id=None, category="unknown",
             gold=None, candidates=[]),
    ]
    pairs, stats = augment(rows, traces=[], pool=[])

    assert pairs == []
    assert stats["missing_neg"] == 0


def test_dev_and_test_keep_human_negative_queries():
    """missing / noise 的人工负例必须留在 dev/test。

    丢掉它们的后果很具体：dev/test 只剩正例，Hit@1 的最小刻度变成
    1/正例数，MRR 早停会在噪声上挑轮次；阈值标定也只剩「最强的错误
    对手」这一种负例，量不到「本来就不该答」。
    """
    rows = [_row(f"退货问题{i}", candidates=[]) for i in range(12)]
    rows += [
        _row(f"没有答案的问题{i}", label="missing", faq_id=None, category="unknown")
        for i in range(6)
    ]
    built = build_pairs(split(rows, seed=5), traces=[])

    for name in ("dev", "test"):
        negatives = [p for p in built[name] if p["label"] == 0]
        positives = [p for p in built[name] if p["label"] == 1]
        assert positives, f"{name} 丢了正例"
        assert negatives, f"{name} 丢了人工负例"
        assert all(p["faq_id"] is None for p in negatives)
        assert all(p["expect"] == "abstain" for p in negatives)
        assert all(p["source"] == "human" for p in built[name])


def test_build_pairs_passes_pool_through_to_negatives():
    """没有候选快照时，pool 必须一路传到增强层，否则训练集没有负例。"""
    rows = [_row(f"退货问题{i}", candidates=[]) for i in range(12)]
    pool = [_pool(f"P-RET-00{i}-1") for i in range(1, 9)]
    built = build_pairs(split(rows, seed=5), traces=[], pool=pool)

    assert built["augment"]["random_neg"] > 0
    assert any(p["label"] == 0 for p in built["train"])


def test_export_jsonl_writes_one_object_per_line(tmp_path):
    path = tmp_path / "train.jsonl"
    rows = [{"query": "退货", "doc": "退货条件：七天无理由。", "label": 1}]
    export_jsonl(rows, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["label"] == 1


def test_gate_threshold_is_80():
    assert GATE_MIN_POSITIVE == 80