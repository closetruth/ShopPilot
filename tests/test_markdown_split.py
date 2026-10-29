"""切分核心的纯函数测试。不依赖数据库与向量库。"""

import pytest

from app.markdown_split import (
    DEFAULT_MAX_CHARS,
    MAX_SECTION,
    MIN_CONTENT,
    parse_frontmatter,
    split_markdown,
)


# ---------- frontmatter ----------


def test_parse_frontmatter():
    text = "---\nid: P-1\ntitle: 测试\n---\n正文"
    meta, body = parse_frontmatter(text)
    assert meta == {"id": "P-1", "title": "测试"}
    assert body == "正文"


def test_parse_frontmatter_absent():
    meta, body = parse_frontmatter("没有头部\n正文")
    assert meta == {}
    assert body == "没有头部\n正文"


# ---------- 基本切分 ----------


def test_two_h2_sections():
    text = "## 第一节\n\n正文一。\n\n## 第二节\n\n正文二。"
    result = split_markdown(text, title="测试")
    assert [c.section for c in result.chunks] == ["第一节", "第二节"]
    assert result.chunks[0].content == "正文一。"
    assert result.chunks[1].content == "正文二。"


def test_no_heading_whole_text():
    text = "这是一段没有标题的正文内容，长度足够通过校验。"
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 1
    assert result.chunks[0].section == "全文"
    assert result.chunks[0].kind == "fulltext"


def test_empty_document():
    result = split_markdown("   \n\n  ", title="空")
    assert result.chunks == []
    assert result.warnings


def test_crlf_normalized():
    text = "## 一节\r\n\r\n正文一。\r\n\r\n## 二节\r\n\r\n正文二。"
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 2
    assert "\r" not in result.chunks[0].content


# ---------- 多级标题 ----------


def test_nested_headings_leaf_only():
    text = (
        "# 文档\n\n"
        "## 章一\n\n"
        "### 节一\n\n正文甲，足够长以满足最小长度要求。\n\n"
        "### 节二\n\n正文乙，同样需要足够的长度才能独立成条。\n"
    )
    result = split_markdown(text, title="测试")
    sections = [c.section for c in result.chunks]
    assert "节一" in sections
    assert "节二" in sections
    assert "章一" not in sections


def test_parent_with_substantial_body_becomes_preamble():
    text = (
        "## 章\n\n"
        "这是章级别的引言，长度足够独立成为一条知识记录。\n\n"
        "### 节\n\n子节正文，内容也要足够长以通过最小长度校验。\n"
    )
    result = split_markdown(text, title="测试")
    kinds = [c.kind for c in result.chunks]
    assert "preamble" in kinds
    assert "leaf" in kinds


def test_short_parent_body_merges_into_first_child():
    text = "## 章\n\n引言。\n\n### 节\n\n子节正文，内容要足够长以通过校验。\n"
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 1
    assert result.chunks[0].section == "节"
    assert "引言。" in result.chunks[0].content


def test_path_mode_joins_ancestors():
    text = (
        "## 退货\n\n"
        "### 七天无理由\n\n正文内容足够长以通过最小长度校验要求。\n"
    )
    result = split_markdown(text, title="测试", section_mode="path")
    assert result.chunks[0].section == "退货 / 七天无理由"


def test_path_mode_truncated_to_max_section():
    long_a = "甲" * 40
    long_b = "乙" * 40
    text = f"## {long_a}\n\n### {long_b}\n\n正文内容足够长以通过最小长度校验。\n"
    result = split_markdown(text, title="测试", section_mode="path")
    assert len(result.chunks[0].section) <= MAX_SECTION


# ---------- 围栏与表格 ----------


def test_hash_inside_fence_not_heading():
    text = (
        "## 示例\n\n"
        "```bash\n"
        "# 这是注释不是标题\n"
        "echo hi\n"
        "```\n\n"
        "正文内容足够长以通过最小长度校验要求。\n"
    )
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 1
    assert "# 这是注释不是标题" in result.chunks[0].content


def test_tilde_fence():
    text = "## 示例\n\n~~~\n## 不切\n~~~\n\n正文足够长以通过最小长度校验。\n"
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 1
    assert "## 不切" in result.chunks[0].content


def test_unclosed_fence_swallows_rest():
    text = "## 示例\n\n```\n未闭合的内容\n"
    result = split_markdown(text, title="测试")
    assert len(result.chunks) == 1


def test_table_kept_in_body():
    text = (
        "## 价目\n\n"
        "| 规格 | 价格 |\n| --- | --- |\n| 小 | 10 |\n\n"
        "上表为参考价格，具体以商品页为准。\n"
    )
    result = split_markdown(text, title="测试")
    assert "| 小 | 10 |" in result.chunks[0].content


def test_list_kept_in_body():
    text = "## 步骤\n\n1. 第一步\n2. 第二步\n3. 第三步，内容够长了\n"
    result = split_markdown(text, title="测试")
    assert "1. 第一步" in result.chunks[0].content


# ---------- 超长切分 ----------


def test_long_section_split_with_suffix():
    para = "这是一段用于测试超长切分的正文内容。" * 20
    text = f"## 长节\n\n{para}\n\n{para}\n\n{para}\n"
    result = split_markdown(text, title="测试", max_chars=200)
    assert len(result.chunks) > 1
    assert all(len(c.content) <= 200 for c in result.chunks)
    assert all(c.part is not None for c in result.chunks)
    assert result.chunks[0].section.endswith(f"（1/{len(result.chunks)}）")


def test_single_huge_paragraph_splits_at_sentence():
    sentence = "客户提交退货申请后需在七日内寄回商品。"
    text = "## 长段\n\n" + sentence * 40
    result = split_markdown(text, title="测试", max_chars=200)
    assert len(result.chunks) > 1
    for chunk in result.chunks:
        assert len(chunk.content) <= 200
        assert chunk.content.endswith("。")


def test_default_max_chars_applies():
    para = "正文内容。" * 120
    text = f"## 节\n\n{para}\n"
    result = split_markdown(text, title="测试")
    assert all(len(c.content) <= DEFAULT_MAX_CHARS for c in result.chunks)


# ---------- 碎块合并 ----------


def test_tiny_chunk_flagged_not_merged():
    """短条目应保留各自的 section，只打警告，不跨节合并。

    合并会让两节含义不同的知识变成一条，丢掉 section 语义。
    """
    text = (
        "## 一节\n\n短。\n\n"
        "## 二节\n\n这一节的正文足够长，可以独立成条不需要合并。\n"
    )
    result = split_markdown(text, title="测试")

    sections = [c.section for c in result.chunks]
    assert sections == ["一节", "二节"], "短条目不应被并掉"

    tiny = [c for c in result.chunks if len(c.content) < MIN_CONTENT]
    assert len(tiny) == 1
    assert tiny[0].warnings, "过短的条目必须带警告"


# ---------- legacy 兼容 ----------


def test_legacy_ignores_h3():
    text = (
        "## 章\n\n### 是正文不是标题\n\n正文内容足够长以通过最小长度校验。\n"
    )
    result = split_markdown(text, title="测试", legacy=True)
    assert len(result.chunks) == 1
    assert result.chunks[0].section == "章"
    assert "### 是正文不是标题" in result.chunks[0].content


def test_legacy_no_length_split():
    para = "正文内容。" * 200
    text = f"## 节\n\n{para}\n"
    result = split_markdown(text, title="测试", legacy=True, max_chars=500)
    assert len(result.chunks) == 1
    assert len(result.chunks[0].content) > 500


# ---------- 默认值 ----------


def test_frontmatter_supplies_title_and_category():
    text = "---\ntitle: 来自头部\ncategory: 退货\n---\n\n## 节\n\n正文足够长以通过校验。\n"
    result = split_markdown(text)
    assert result.title == "来自头部"
    assert result.category == "退货"


def test_explicit_args_override_frontmatter():
    text = "---\ntitle: 头部\ncategory: 头部类\n---\n\n## 节\n\n正文足够长以通过校验。\n"
    result = split_markdown(text, title="参数", category="参数类")
    assert result.title == "参数"
    assert result.category == "参数类"


def test_missing_title_falls_back_to_empty():
    result = split_markdown("## 节\n\n正文足够长以通过校验要求。\n")
    assert result.title == ""


# ---------- 结构完整性 ----------


def test_chunks_have_nonempty_content():
    text = "# A\n\n## B\n\n### C\n\n正文内容足够长以通过最小长度校验。\n"
    result = split_markdown(text, title="测试")
    assert all(c.content.strip() for c in result.chunks)


def test_section_within_length_limit():
    text = "## " + "很长的标题" * 20 + "\n\n正文足够长以通过最小长度校验。\n"
    result = split_markdown(text, title="测试")
    assert all(len(c.section) <= MAX_SECTION for c in result.chunks)
