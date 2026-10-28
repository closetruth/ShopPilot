"""FAQ 导入与切分逻辑测试。"""

import pytest

from scripts.import_faq import build_documents, parse_frontmatter, split_sections


SAMPLE = """---
id: P-TEST-001
title: 测试规则
category: 测试
version: 2026-01-01
---

## 第一节

正文一。

## 第二节

正文二，包含 [变量] 占位。
"""


def test_parse_frontmatter():
    meta, body = parse_frontmatter(SAMPLE)
    assert meta["id"] == "P-TEST-001"
    assert meta["title"] == "测试规则"
    assert meta["category"] == "测试"
    assert body.lstrip().startswith("## 第一节")


def test_parse_frontmatter_without_header():
    meta, body = parse_frontmatter("## 无头部\n正文")
    assert meta == {}
    assert body == "## 无头部\n正文"


def test_split_sections():
    _, body = parse_frontmatter(SAMPLE)
    sections = split_sections(body)
    assert len(sections) == 2
    assert sections[0][0] == "第一节"
    assert sections[0][1] == "正文一。"
    assert sections[1][0] == "第二节"
    assert "正文二" in sections[1][1]


def test_split_sections_without_heading():
    sections = split_sections("只有正文，没有小节标题。")
    assert sections == [("全文", "只有正文，没有小节标题。")]


def test_split_sections_skips_empty():
    body = "## 有内容\n正文\n\n## 空小节\n\n"
    sections = split_sections(body)
    assert len(sections) == 1
    assert sections[0][0] == "有内容"


def test_build_documents_from_repo_faq():
    docs = build_documents()
    assert docs, "data/faq 下应有政策文档"

    for doc in docs:
        assert doc["id"]
        assert doc["doc_id"]
        assert doc["title"]
        assert doc["category"]
        assert doc["section"]
        assert doc["content"]
        assert len(doc["content"]) > 0


def test_build_documents_ids_are_unique():
    docs = build_documents()
    ids = [d["id"] for d in docs]
    assert len(ids) == len(set(ids))


def test_faq_covers_core_policy_areas():
    """核心政策域必须都有覆盖，否则顾客问到时检索不到。"""
    docs = build_documents()
    categories = {d["category"] for d in docs}
    for required in ("退货", "退款", "物流"):
        assert required in categories, f"缺少「{required}」类政策"
