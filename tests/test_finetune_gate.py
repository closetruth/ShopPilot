"""发布门禁测试：三把尺的判定与两个指标的算法。

判定是发布前最后一道闸，所以这里全部用纯函数验证，不打模型也不连远端。
要守住的核心不是「达标能过」，而是「不达标与没评估都过不去」——门禁
在错的方向上放行一次，代价是一次线上回滚。
"""

import pytest

from app.finetune.gate import (
    DOC_HIT_TOLERANCE,
    HIT1_MIN,
    MRR3_MIN,
    REPLAY_MIN,
    auc,
    evaluate_gate,
    hit1_rate,
    mrr_at_k,
    reciprocal_rank,
    sweep_threshold,
)


def _passing(**overrides) -> dict:
    result = {
        "test": {"hit1": 0.8, "mrr3": 0.85, "auc": 0.9},
        "replay": {"fix_rate": 0.3, "total": 40, "answered": 12},
        "e2e": {"doc_hit_rate": 0.92, "baseline": 0.93},
    }
    result.update(overrides)
    return result


def test_all_rulers_passing_allows_release():
    gate = evaluate_gate(_passing())
    assert gate["ok"], gate["failed"]
    assert gate["failed"] == []
    assert len(gate["checks"]) == 4


def test_low_offline_hit1_blocks():
    gate = evaluate_gate(_passing(test={"hit1": HIT1_MIN - 0.01, "mrr3": 0.85}))
    assert not gate["ok"]
    assert any("test_hit1" in item for item in gate["failed"])


def test_low_mrr3_blocks():
    gate = evaluate_gate(_passing(test={"hit1": 0.8, "mrr3": MRR3_MIN - 0.01}))
    assert not gate["ok"]
    assert any("test_mrr3" in item for item in gate["failed"])


def test_low_replay_rate_blocks():
    gate = evaluate_gate(_passing(replay={"fix_rate": REPLAY_MIN - 0.01, "total": 40}))
    assert not gate["ok"]
    assert any("replay_fix_rate" in item for item in gate["failed"])


def test_empty_replay_pool_says_so():
    """分母为 0 与「救了但没救回来」要能分开：前者补标注，后者调模型。"""
    gate = evaluate_gate(_passing(replay={"fix_rate": 0.0, "total": 0}))
    assert not gate["ok"]
    message = "；".join(gate["failed"])
    assert "样本为 0" in message, message


def test_e2e_within_tolerance_passes():
    gate = evaluate_gate(
        _passing(e2e={"doc_hit_rate": 0.93 - DOC_HIT_TOLERANCE, "baseline": 0.93})
    )
    assert gate["ok"], gate["failed"]


def test_e2e_degradation_beyond_tolerance_blocks():
    gate = evaluate_gate(
        _passing(e2e={"doc_hit_rate": 0.90, "baseline": 0.93})
    )
    assert not gate["ok"]
    assert any("e2e_doc_hit" in item for item in gate["failed"])


def test_missing_e2e_blocks():
    """没跑端到端回归不能当达标：跳过等于放行最该拦的那种情况。"""
    gate = evaluate_gate(_passing(e2e=None))
    assert not gate["ok"]
    assert any("未提供" in item for item in gate["failed"])


def test_missing_test_metrics_blocks():
    gate = evaluate_gate(
        {
            "replay": {"fix_rate": 0.3, "total": 10},
            "e2e": {"doc_hit_rate": 0.9, "baseline": 0.9},
        }
    )
    assert not gate["ok"]
    failed = "；".join(gate["failed"])
    assert "test_hit1" in failed and "test_mrr3" in failed


def test_empty_result_blocks_everything():
    gate = evaluate_gate({})
    assert not gate["ok"]
    assert len(gate["failed"]) == 4


def test_reciprocal_rank_truncates_at_k():
    assert reciprocal_rank(1, 3) == 1.0
    assert reciprocal_rank(2, 3) == 0.5
    assert reciprocal_rank(3, 3) == pytest.approx(1 / 3)
    assert reciprocal_rank(4, 3) == 0.0
    assert reciprocal_rank(0, 3) == 0.0


def test_hit1_and_mrr_at_3():
    ranks = [1, 1, 2, 5]
    assert hit1_rate(ranks) == 0.5
    assert mrr_at_k(ranks, 3) == pytest.approx((1 + 1 + 0.5 + 0) / 4)
    assert hit1_rate([]) == 0.0, "没有样本不能算作达标"


def test_auc_handles_ties():
    """同分按 0.5 计。全算正例赢会让 AUC 虚高，虚高的指标会放行坏模型。"""
    assert auc([0.9, 0.9], [0.9, 0.9]) == pytest.approx(0.5)
    assert auc([0.8], [0.2]) == pytest.approx(1.0)
    assert auc([0.2], [0.8]) == pytest.approx(0.0)
    assert auc([], [0.5]) == 0.0


def test_auc_matches_pairwise_count():
    positives = [0.9, 0.6, 0.7]
    negatives = [0.1, 0.5, 0.8]
    pairwise = sum(
        1.0 if pos > neg else 0.5 if pos == neg else 0.0
        for pos in positives
        for neg in negatives
    ) / (len(positives) * len(negatives))
    assert auc(positives, negatives) == pytest.approx(pairwise)


def test_sweep_picks_separating_threshold():
    """正例都在 0.8 以上、负例都在 0.4 以下时，最优阈值应落在中间。"""
    positives = [0.85, 0.9, 0.82]
    negatives = [0.3, 0.35, 0.4]
    best = sweep_threshold(positives, negatives)
    assert 0.4 < best["threshold"] <= 0.82
    assert best["youden_j"] == pytest.approx(1.0)
    assert best["recall"] == 1.0 and best["specificity"] == 1.0


def test_sweep_returns_full_curve():
    curve = sweep_threshold([0.9], [0.2])["curve"]
    assert len(curve) == 41, "0.1–0.9 步长 0.02 应有 41 个点"
    assert curve[0]["threshold"] == 0.1
    assert curve[-1]["threshold"] == pytest.approx(0.9)


def test_sweep_without_negatives_degrades_to_lowest_threshold():
    """没有负例时特异度恒为 0，J 在最低阈值处最大——宁可多答也不漏答，
    但这说明标定数据不足，调用方要靠回放尺兜住。"""
    best = sweep_threshold([0.9], [])
    assert best["threshold"] == 0.1
    assert best["specificity"] == 0.0