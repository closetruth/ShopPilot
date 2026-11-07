"""清理四张微调表里由测试写出的残留行。

默认 dry-run，只统计不删除；加 --apply 才真正执行。

识别依据都是测试用例的固定形态，真人操作不会产生：
- labels:   query 以 `-` + 8 位 hex 结尾（`退货怎么弄-1a2b3c4d`）
- datasets: name 为 `r21-test` / `r21-job` / `r21-run` /
            `test-build-xxxxxx` / `test-jobs-xxxxxxxx`
- jobs:     id 为 `remote-` + 8 位 hex（假 trainer 返回的 id）
            或 `job-` + 10 位 hex
- models:   version 为 `v-xxxxxxxx` / `r21-test-xxxxxx` /
            `r21-idem-xxxxxx` / `r21-act-xxxxxx`

任何不符合上述形态的行都会单独列出来并跳过，不做「整表清空」。
"""

from __future__ import annotations

import re
import sys

from dotenv import load_dotenv

from app.core.db import connect

HEX6 = r"[0-9a-f]{6}"
HEX8 = r"[0-9a-f]{8}"
HEX10 = r"[0-9a-f]{10}"

PATTERNS = {
    "finetune_labels": re.compile(r"-" + HEX8 + r"$"),
    "finetune_datasets": re.compile(
        r"^(r21-test|r21-job|r21-run|test-build-"
        + HEX6
        + r"|test-jobs-"
        + HEX8
        + r")$"
    ),
    "finetune_jobs": re.compile(r"^(remote-" + HEX8 + r"|job-" + HEX10 + r")$"),
    "finetune_models": re.compile(
        r"^(v-"
        + HEX8
        + r"|r21-test-"
        + HEX6
        + r"|r21-idem-"
        + HEX6
        + r"|r21-act-"
        + HEX6
        + r")$"
    ),
}

COLUMNS = {
    "finetune_labels": "query",
    "finetune_datasets": "name",
    "finetune_jobs": "id",
    "finetune_models": "version",
}


def main() -> None:
    load_dotenv()
    apply = "--apply" in sys.argv

    with connect() as conn, conn.cursor() as cur:
        for table, pattern in PATTERNS.items():
            col = COLUMNS[table]
            cur.execute(f"SELECT id, {col} AS key_value FROM {table}")
            rows = cur.fetchall()
            hit = [r for r in rows if pattern.search(r["key_value"] or "")]
            keep = [r for r in rows if not pattern.search(r["key_value"] or "")]

            print(
                "%-18s total=%-4d test_rows=%-4d keep=%d"
                % (table, len(rows), len(hit), len(keep))
            )
            for row in keep:
                print("    KEEP:", row["id"], repr(row["key_value"]))

            if not apply or not hit:
                continue

            ids = [r["id"] for r in hit]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
            print("    deleted:", len(ids))

    print("APPLIED" if apply else "DRY-RUN（加 --apply 才真正删除）")


if __name__ == "__main__":
    main()