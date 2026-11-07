"""发布门禁：三把尺的阈值与判定。

写成纯函数、不碰模型也不碰数据库，因为这份判定要跑在两个地方：远端
容器里给 `activate` 端点用，本地测试里验证判定本身。判定逻辑若各写一份，
迟早出现「页面说达标、端点说不达标」的分裂。

三把尺缺一不可：

- 离线：在自己的 test 集上排序好，只说明拟合了这批标注；
- 回放：低置信度问题里有多少被救回来，是唯一直接回答「飞轮攒的东西
  有没有变成能力」的数字；
- 端到端：指标在真实链路上不劣化，避免「离线涨了、线上没动」。

任何一把缺失都按未达标处理——缺数字与数字难看，在发布这件事上等价。
"""

from __future__ import annotations

# 门槛。数值来自设计文档 §9，改动要同步 docs 与前端文案
HIT1_MIN = 0.75
MRR3_MIN = 0.80
REPLAY_MIN = 0.25
DOC_HIT_TOLERANCE = 0.02


def reciprocal_rank(rank: int, k: int) -> float:
    """第 rank 名（从 1 起）的倒数排名；超出 k 记 0。

    MRR@3 与 MRR 的区别就在这里：排在第四位的金标对 MRR@3 毫无贡献，
    对 MRR 却仍算 0.25。线上只展示前 3 条，所以用 @3 更贴近现实。
    """
    if rank < 1 or rank > k:
        return 0.0
    return 1.0 / rank


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def hit1_rate(ranks: list[int]) -> float:
    """金标排第一的比例。空集按 0——没有样本的指标不能算作达标。"""
    if not ranks:
        return 0.0
    return sum(1 for rank in ranks if rank == 1) / len(ranks)


def mrr_at_k(ranks: list[int], k: int = 3) -> float:
    return mean([reciprocal_rank(rank, k) for rank in ranks])


def auc(positives: list[float], negatives: list[float]) -> float:
    """成对 AUC：随机取一条正例与一条负例，正例分更高的概率。

    用秩统计算而不是逐对比较——47 条池子 × 265 条样本的逐对循环在 CPU
    上要跑很久，而 AUC 本来就有 O(n log n) 的算法。
    """
    if not positives or not negatives:
        return 0.0
    merged = sorted(
        [(score, 1) for score in positives] + [(score, 0) for score in negatives],
        key=lambda item: item[0],
    )
    # 同分按 0.5 计：精排分常有并列，全算正例赢会虚高
    rank_sum = 0.0
    index = 0
    while index < len(merged):
        end = index
        while end < len(merged) and merged[end][0] == merged[index][0]:
            end += 1
        average_rank = (index + end + 1) / 2.0
        for position in range(index, end):
            if merged[position][1] == 1:
                rank_sum += average_rank
        index = end
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def sweep_threshold(
    positives: list[float],
    negatives: list[float],
    *,
    lo: float = 0.1,
    hi: float = 0.9,
    step: float = 0.02,
) -> dict:
    """扫阈值取 Youden J 最大点，并给出整条曲线。

    positives 是「应该答」的样本分（金标分），negatives 是「不该答」的
    样本分（同一 query 下最高的非金标分）。取最坏的那个对手做负例，是因为
    线上拒答与否取决于最高分那条——比错了就答错。

    Youden J = 召回 + 特异度 - 1，等于「既不漏答也不乱答」的净收益。
    单看召回会一路降到 0.1，单看精确率会一路升到 0.9，都没法用。
    """
    curve: list[dict] = []
    best = {"threshold": None, "youden_j": -1.0, "recall": 0.0, "specificity": 0.0}
    # 按整数步索引生成阈值，不做 `threshold += step` 累加：0.1 连加 15 次
    # 0.02 得到的是 0.4000000000000001，用它作比较会把恰好等于 0.4 的负例
    # 判成「低于阈值」，特异度凭空多算一条
    steps = int(round((hi - lo) / step))
    for index in range(steps + 1):
        threshold = round(lo + index * step, 4)
        recall = (
            sum(1 for score in positives if score >= threshold) / len(positives)
            if positives
            else 0.0
        )
        specificity = (
            sum(1 for score in negatives if score < threshold) / len(negatives)
            if negatives
            else 0.0
        )
        youden = recall + specificity - 1.0
        curve.append(
            {
                "threshold": threshold,
                "recall": round(recall, 4),
                "specificity": round(specificity, 4),
                "youden_j": round(youden, 4),
            }
        )
        if youden > best["youden_j"]:
            best = {
                "threshold": threshold,
                "youden_j": round(youden, 4),
                "recall": round(recall, 4),
                "specificity": round(specificity, 4),
            }
    return {**best, "curve": curve}


def _check(key: str, value, ok: bool, note: str) -> dict:
    return {"key": key, "value": value, "ok": ok, "note": note}


def evaluate_gate(result: dict) -> dict:
    """按三把尺判定能不能上线，返回 `{"ok": bool, "checks": [...], "failed": [...]}`。

    `result` 是 `eval.json` 的内容（远端评估产出）。字段缺失一律按未达标，
    不做「缺就跳过」的宽容处理：跳过意味着最该拦的那种情况（压根没评估）
    会被放行。
    """
    test = result.get("test") or {}
    replay = result.get("replay") or {}
    e2e = result.get("e2e") or {}

    hit1 = test.get("hit1")
    mrr3 = test.get("mrr3")
    fix_rate = replay.get("fix_rate")
    replay_total = replay.get("total")
    doc_hit = e2e.get("doc_hit_rate")
    baseline = e2e.get("baseline")

    # 分母为 0 时 fix_rate 必然是 0，但「没样本」和「有样本但没救回来」是
    # 两回事：前者该去补标注，后者该去改模型。提示里必须区分开，否则看到
    # 「修复率 0%」只会去调参
    replay_note = (
        "低置信度回放样本为 0，需先标注低置信度池"
        if replay_total == 0
        else f"低置信度回放修复率需 ≥ {REPLAY_MIN}"
    )

    checks = [
        _check(
            "test_hit1",
            hit1,
            isinstance(hit1, (int, float)) and hit1 >= HIT1_MIN,
            f"test Hit@1 需 ≥ {HIT1_MIN}",
        ),
        _check(
            "test_mrr3",
            mrr3,
            isinstance(mrr3, (int, float)) and mrr3 >= MRR3_MIN,
            f"test MRR@3 需 ≥ {MRR3_MIN}",
        ),
        _check(
            "replay_fix_rate",
            fix_rate,
            isinstance(fix_rate, (int, float)) and fix_rate >= REPLAY_MIN,
            replay_note,
        ),
        _check(
            "e2e_doc_hit",
            doc_hit,
            isinstance(doc_hit, (int, float))
            and isinstance(baseline, (int, float))
            and doc_hit >= baseline - DOC_HIT_TOLERANCE,
            f"端到端 doc_hit_rate 不得比基线低 {DOC_HIT_TOLERANCE} 以上",
        ),
    ]

    failed = [
        f"{check['key']}：{check['note']}（实际 {check['value']}）"
        if check["value"] is not None
        else f"{check['key']}：{check['note']}（未提供）"
        for check in checks
        if not check["ok"]
    ]
    return {"ok": not failed, "checks": checks, "failed": failed}