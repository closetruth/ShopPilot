"""把批注文件合并进 r13-queries.csv。

批注格式：序号|query|expected_doc_ids|confidence|note
多标签用 `|` 分隔，如 P-LOG-001-3|P-REF-002-1

CSV 的 expected_doc_id 列存多标签（`|` 分隔），保持列名不变以兼容
已有评估脚本；空值表示 none。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

CSV_PATH = Path("docs/eval/r13-queries.csv")


VALID_CONFIDENCE = {"high", "medium", "low", "none"}


def parse_line(line: str) -> dict | None:
    """解析批注行：idx|query|doc1[|doc2...]|confidence|note

    多标签与字段都用 `|` 分隔，所以不能按位置取字段。做法是从右往左
    找第一个 confidence 标记，它之前的部分里，除 idx 与 query 外都是
    doc_id。
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 4:
        return None

    conf_at = None
    for i in range(len(parts) - 1, 1, -1):
        if parts[i] in VALID_CONFIDENCE:
            conf_at = i
            break
    if conf_at is None or conf_at < 3:
        return None

    idx = int(parts[0])
    query = parts[1]
    doc_ids = [p for p in parts[2:conf_at] if p and p.lower() != "none"]
    confidence = parts[conf_at]
    note = parts[conf_at + 1] if conf_at + 1 < len(parts) else ""

    return {
        "idx": idx,
        "query": query,
        "doc_ids": "|".join(doc_ids),
        "confidence": confidence,
        "note": note,
    }


def apply_batch(batch_path: Path) -> tuple[int, dict]:
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    by_index = {i: r for i, r in enumerate(rows, 1)}

    applied = 0
    stats: dict[str, int] = {}
    for raw in batch_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue

        parsed = parse_line(raw)
        if parsed is None:
            print(f"跳过格式异常行：{raw[:50]}")
            continue

        row = by_index.get(parsed["idx"])
        if row is None:
            print(f"序号越界：{parsed['idx']}")
            continue

        # 批注里的 query 用于校验，防止序号错位
        if row["query"] != parsed["query"]:
            print(
                f"序号 {parsed['idx']} 的 query 不匹配：\n"
                f"  CSV: {row['query']}\n"
                f"  批注: {parsed['query']}"
            )
            continue

        row["expected_doc_id"] = parsed["doc_ids"]
        row["confidence"] = parsed["confidence"]
        row["note"] = parsed["note"]
        applied += 1

        doc_ids = parsed["doc_ids"]
        if not doc_ids:
            key = "none"
        elif "|" in doc_ids:
            key = "multi"
        else:
            key = parsed["confidence"]
        stats[key] = stats.get(key, 0) + 1

    with CSV_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    return applied, stats


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python -m scripts.apply_annotations <批注文件>")
        sys.exit(1)

    total = 0
    for arg in sys.argv[1:]:
        applied, stats = apply_batch(Path(arg))
        total += applied
        print(f"{arg}: 应用 {applied} 条 -> {stats}")

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    done = sum(1 for r in rows if r["confidence"])
    print(f"\n总计已标注 {done}/{len(rows)}")


if __name__ == "__main__":
    main()
