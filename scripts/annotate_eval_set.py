"""为 r13 评估集打标注：给每条 query 判定期望命中的政策条目。

标注策略：用业务规则把 query 映射到政策主题族，再按候选分数选具体小节。
规则是显式的，便于抽样复核；无法归类的标 none。

用法：uv run python -m scripts.annotate_eval_set
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

EVAL_CSV = Path("docs/eval/r13-queries.csv")

# 主题族 → 该族的政策条目（doc_id 前缀）与判定关键词
# 顺序即优先级：先匹配到的胜出
RULES: list[tuple[str, list[str], tuple[str, ...]]] = [
    # 物流类要先于退货，因为「快递破损」既像退货又像物流
    ("P-LOG-001", ["P-LOG-001"], (
        "快递", "物流", "包裹", "运单", "签收", "配送", "派送", "中转",
        "没动", "不动", "停滞", "延迟", "迟迟", "丢", "破损", "压坏",
        "没发货", "未发货", "催", "什么时候到", "几天到", "没收到",
    )),
    ("P-LOG-002", ["P-LOG-002"], ("运费险",)),
    ("P-PRI-001", ["P-PRI-001"], ("降价", "价保", "差价", "便宜了", "补差")),
    ("P-INV-001", ["P-INV-001"], ("发票", "开票", "抬头", "税号", "报销")),
    ("P-MEM-001", ["P-MEM-001"], ("会员", "plus", "PLUS", "权益", "极速退款")),
    ("P-COM-001", ["P-COM-001"], ("投诉", "维权", "客服态度", "举报", "315")),
    ("P-REF-003", ["P-REF-003"], ("大额", "退款要审核", "审核")),
    ("P-REF-002", ["P-REF-002"], (
        "质量", "瑕疵", "破损", "坏", "破", "洞", "裂", "臭", "脏",
        "次品", "假", "不符", "描述不", "不一样", "色差", "发错", "漏发",
    )),
    ("P-RET-002", ["P-RET-002"], ("定制", "生鲜", "虚拟", "内衣", "不支持退")),
    ("P-RET-001", ["P-RET-001"], (
        "退货", "退了", "退掉", "不要了", "无理由", "退回去", "退",
    )),
    ("P-REF-001", ["P-REF-001"], ("仅退款", "只退款", "不退货", "退款不")),
]

# 各主题族内部的「小节选择」提示词 → 具体 doc_id 后缀
SECTION_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("运费", "邮费"), "-3"),
    (("时效", "多久", "几天", "什么时候"), "-4"),
    (("补偿", "赔付", "赔偿"), "-2"),
    (("换", "换货"), "-4"),
]

NOT_POLICY = (
    "尺码", "偏大", "偏小", "多大", "多大码", "几码", "材质", "纯棉",
    "显黑", "显白", "好看", "库存", "有货", "补货", "颜色有多少",
    "优惠券", "coupon", "满减", "活动", "买一送一", "参数", "尺寸",
    "怎么用", "好用吗", "推荐", "防水", "续航", "充电", "膜",
)


def classify(query: str) -> tuple[str, str, str]:
    """返回 (expected_doc_id, confidence, note)。"""
    q = query.lower()

    for word in NOT_POLICY:
        if word in q:
            return "", "none", f"非政策类：{word}"

    for family, _prefixes, words in RULES:
        if any(w in q for w in words):
            suffix = ""
            for hint_words, idx in SECTION_HINTS:
                if any(w in q for w in hint_words):
                    suffix = idx
                    break
            return f"{family}{suffix}" if suffix else family, "medium", ""
    return "", "none", "无匹配主题"


def pick_by_candidates(doc_id: str, candidates: list[tuple[str, float]]) -> str:
    """doc_id 不含后缀时，从候选里挑同族分数最高的具体条目。"""
    if "-" in doc_id and doc_id.count("-") >= 2:
        return doc_id  # 已带后缀
    same = [(c, s) for c, s in candidates if c.startswith(doc_id)]
    if same:
        same.sort(key=lambda x: -x[1])
        return same[0][0]
    return doc_id


def parse_candidates(raw: str) -> list[tuple[str, float]]:
    out = []
    for part in raw.split("|"):
        part = part.strip()
        if ":" in part:
            cid, _, score = part.rpartition(":")
            try:
                out.append((cid.strip(), float(score)))
            except ValueError:
                continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-place", action="store_true")
    args = parser.parse_args()

    rows = list(csv.DictReader(EVAL_CSV.open(encoding="utf-8")))
    stats: dict[str, int] = {}

    for row in rows:
        raw_doc, confidence, note = classify(row["query"])
        if raw_doc:
            cands = parse_candidates(row["candidates"])
            row["expected_doc_id"] = pick_by_candidates(raw_doc, cands)
            # 规则匹配到主题但选了具体小节，置信度降一档
            row["confidence"] = confidence
        else:
            row["expected_doc_id"] = ""
            row["confidence"] = confidence
        row["note"] = note

        key = row["confidence"]
        stats[key] = stats.get(key, 0) + 1

    with EVAL_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"标注完成，共 {len(rows)} 条")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
