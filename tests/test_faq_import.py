"""FAQ 导入与切分逻辑测试。"""

import pytest

from app.retrieval.markdown_split import parse_frontmatter, split_markdown
from scripts.import_faq import build_documents


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


def _sections(body: str) -> list[tuple[str, str]]:
    """脚本使用的 legacy 切分结果。"""
    return [(c.section, c.content) for c in split_markdown(body, legacy=True).chunks]


def test_split_sections():
    _, body = parse_frontmatter(SAMPLE)
    sections = _sections(body)
    assert len(sections) == 2
    assert sections[0][0] == "第一节"
    assert sections[0][1] == "正文一。"
    assert sections[1][0] == "第二节"
    assert "正文二" in sections[1][1]


def test_split_sections_without_heading():
    assert _sections("只有正文，没有小节标题。") == [("全文", "只有正文，没有小节标题。")]


def test_split_sections_skips_empty():
    assert len(_sections("## 有内容\n正文\n\n## 空小节\n\n")) == 1


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


def test_build_documents_skips_readme():
    """README 是给人看的说明，不是政策，不该被解析成知识条目。"""
    docs = build_documents()
    sources = {d["source"] for d in docs}
    assert "README.md" not in sources, f"README 被导入了：{[s for s in sources if 'README' in s.upper()]}"

    categories = {d["category"] for d in docs}
    assert "未分类" not in categories, "无 frontmatter 的说明文件混入了知识库"


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


# ---------- golden：与旧实现逐字一致 ----------


def _old_split_sections(body: str) -> list[tuple[str, str]]:
    """重构前的 split_sections，作为兼容性基准。

    逐行照搬旧实现，用来锁死「legacy 模式输出必须与历史完全一致」。
    脚本的 id 是 {doc_id}-{序号}，切分结果一变，存量条目就会被删重建。
    """
    import re

    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(body))
    if not matches:
        text = body.strip()
        return [("全文", text)] if text else []

    sections = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content:
            sections.append((match.group(1).strip(), content))
    return sections


@pytest.mark.parametrize(
    "path",
    sorted(p for p in (__import__("pathlib").Path("data/faq")).glob("P-*.md")),
    ids=lambda p: p.stem,
)
def test_legacy_matches_old_implementation(path):
    """each(data/faq/*.md) 上，legacy 模式与旧 split_sections 逐字相等。"""
    text = path.read_text(encoding="utf-8-sig")
    _, body = parse_frontmatter(text)

    old = _old_split_sections(body)
    new = _sections(body)

    assert len(old) == len(new), f"{path.name} 切分数量不一致：{len(old)} vs {len(new)}"
    for (old_title, old_body), (new_title, new_body) in zip(old, new):
        assert old_title == new_title, f"{path.name} 小节名不一致"
        assert old_body == new_body, f"{path.name} / {old_title} 正文不一致"


def test_legacy_ids_unchanged():
    """导入产出的 id 集合必须与旧实现相同，否则存量条目会被删重建。"""
    docs = build_documents()
    ids = {d["id"] for d in docs}

    expected = set()
    for path in sorted((__import__("pathlib").Path("data/faq")).glob("P-*.md")):
        text = path.read_text(encoding="utf-8-sig")
        meta, body = parse_frontmatter(text)
        doc_id = meta.get("id") or path.stem
        for i in range(len(_old_split_sections(body))):
            expected.add(f"{doc_id}-{i + 1}")

    assert ids == expected, f"多出 {ids - expected}，缺少 {expected - ids}"