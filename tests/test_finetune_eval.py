"""离线评估尺测试：标定与 test 尺的纯函数部分，用假打分器，不打模型。

这里守的是「阈值到底被谁约束住」：只用「最强的错误对手」当负例，阈值
会被压到很低——正例里总有一个很像答案的错误答案，模型给它高分是正常的；
真正能约束误答的是人工标成 missing / noise 的问题，它们在任何条款上都
不该越过闸。丢掉这一路，阈值就只剩半个约束。
"""

from scripts.finetune_eval import calibrate, eval_offline

POOL = [
    {"faq_id": "A-1", "doc_id": "A", "doc": "退货条件：七天无理由。", "category": "退货"},
    {"faq_id": "B-1", "doc_id": "B", "doc": "运费规则：满 99 包邮。", "category": "运费"},
]


def _scorer(table):
    """按 (query, doc) 查表打分，查不到给 0.05。"""
    def score(query, docs):
        return [table.get((query, doc), 0.05) for doc in docs]

    return score


def test_calibration_uses_abstain_queries_as_negatives():
    """人工负例必须进标定的负例集合。

    这里两条正例的最强错误对手都紧贴金标分（0.8 / 0.74 对 0.8 / 0.75），
    所以只靠「最强对手」当负例时，阈值附近的特异度很勉强；补上一条在任何
    条款上只有 0.2 分的无答案问题后，同一阈值的特异度明显上升。
    """
    table = {
        ("退货怎么退", POOL[0]["doc"]): 0.8,
        ("退货怎么退", POOL[1]["doc"]): 0.8,
        ("运费谁出", POOL[0]["doc"]): 0.74,
        ("运费谁出", POOL[1]["doc"]): 0.75,
    }
    positives = [
        {"query": "退货怎么退", "label": 1, "faq_id": "A-1"},
        {"query": "运费谁出", "label": 1, "faq_id": "B-1"},
    ]
    split = positives + [{"query": "能开发票吗", "label": 0, "faq_id": None}]
    scorer = _scorer(table)

    without = calibrate(scorer, positives, POOL)
    with_abstain = calibrate(scorer, split, POOL)

    assert with_abstain["abstain_queries"] == 1
    # 无答案问题的最高分是 0.05，它让同一阈值下的特异度上升，J 更高
    assert with_abstain["youden_j"] > without["youden_j"]
    # 阈值要落在「正例最低分」与「无答案最高分」之间才叫分开了
    assert 0.05 < with_abstain["threshold"] <= 0.75


def test_calibration_without_negatives_says_so():
    split = [{"query": "退货怎么退", "label": 0, "faq_id": None}]
    result = calibrate(_scorer({}), split, POOL)
    assert result["threshold"] is None
    assert result["note"]


def test_offline_reports_abstain_rate_only_with_threshold():
    table = {
        ("退货怎么退", POOL[0]["doc"]): 0.9,
        ("退货怎么退", POOL[1]["doc"]): 0.1,
    }
    split = [
        {"query": "退货怎么退", "label": 1, "faq_id": "A-1"},
        {"query": "能开发票吗", "label": 0, "faq_id": None},
    ]
    scorer = _scorer(table)

    no_threshold = eval_offline(scorer, split, POOL)
    assert no_threshold["abstain_queries"] == 1
    assert no_threshold["abstain_rate"] is None

    with_threshold = eval_offline(scorer, split, POOL, threshold=0.5)
    assert with_threshold["abstain_rate"] == 1.0
    assert with_threshold["hit1"] == 1.0
    assert with_threshold["queries"] == 1


def test_offline_without_positives_does_not_fake_a_score():
    split = [{"query": "能开发票吗", "label": 0, "faq_id": None}]
    result = eval_offline(_scorer({}), split, POOL)
    assert result["queries"] == 0
    assert result["hit1"] == 0.0
    assert result["note"]