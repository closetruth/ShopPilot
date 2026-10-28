"""把 data/faq 下的政策 Markdown 导入 MySQL。

文件格式：
    ---
    id: P-RET-001
    title: 七天无理由退货规则
    category: 退货
    version: 2026-09-01
    ---
    ## 小节名
    正文…

按「## 」小节切分：一个小节是一条独立知识，检索粒度更细，模型拿到的
上下文更聚焦。文档级 content 往往混着多个主题，检索出来会带噪声。

用法：uv run python -m scripts.import_faq
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from app import store, vector_store

FAQ_DIR = Path("data/faq")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def split_sections(body: str) -> list[tuple[str, str]]:
    """按二级标题切分，返回 [(小节名, 正文), ...]。无小节时整篇作一节。"""
    matches = list(SECTION_RE.finditer(body))
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


def build_documents() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(FAQ_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8-sig")
        meta, body = parse_frontmatter(text)

        doc_id = meta.get("id") or path.stem
        title = meta.get("title") or path.stem
        category = meta.get("category") or "未分类"

        for index, (section, content) in enumerate(split_sections(body)):
            docs.append(
                {
                    "id": f"{doc_id}-{index + 1}",
                    "doc_id": doc_id,
                    "title": title,
                    "category": category,
                    "version": meta.get("version", ""),
                    "section": section,
                    "content": content,
                    "source": path.name,
                }
            )
    return docs


def main() -> None:
    if not FAQ_DIR.exists():
        print(f"未找到 {FAQ_DIR}")
        return

    store.init_db()
    docs = build_documents()
    if not docs:
        print("没有可导入的文档")
        return

    for doc in docs:
        store.upsert_faq_document(doc)

    # 文件里已删掉的小节要同步从库里移除，否则作废的政策仍会被检索到。
    # 只清理来自文件的条目（source 为文件名），Web 录入的 source='web' 不受影响。
    sources = {d["source"] for d in docs}
    removed = 0
    for source in sources:
        keep = [d["id"] for d in docs if d["source"] == source]
        stale = store.delete_stale_faq(source, keep)
        for faq_id in stale:
            vector_store.delete_by_source("faq", faq_id)
        removed += len(stale)

    files = len(sources)
    print(f"已导入 {files} 个文件，共 {len(docs)} 条知识小节")
    if removed:
        print(f"已移除文件中不再存在的 {removed} 条")
    print("提示：内容变更需重建向量索引才生效——")
    print("      uv run python -m scripts.reindex --drop")
    print("      或服务运行中调用 POST /api/admin/reindex?drop=true")


if __name__ == "__main__":
    main()
    sys.exit(0)
