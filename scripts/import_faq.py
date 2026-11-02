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

from app.core import store
from app.retrieval import vector_store
from app.retrieval.markdown_split import parse_frontmatter, split_markdown

FAQ_DIR = Path("data/faq")


def build_documents() -> list[dict]:
    docs: list[dict] = []
    for path in sorted(FAQ_DIR.glob("*.md")):
        # README 是给人看的说明，不是政策内容。早先没排除，它被当成一篇
        # 政策解析出 4 条「未分类」知识，顾客检索时会命中这些说明文字。
        if path.name.lower() == "readme.md":
            continue

        text = path.read_text(encoding="utf-8-sig")
        meta, body = parse_frontmatter(text)

        doc_id = meta.get("id") or path.stem
        title = meta.get("title") or path.stem
        category = meta.get("category") or "未分类"

        # legacy 模式：只认 ##、不切长、不合并，输出与旧的 split_sections
        # 逐字一致。脚本的 id 是 {doc_id}-{序号}，切分结果一变就会导致
        # 存量条目被删重建，所以这里必须保持历史行为。
        sections = split_markdown(body, legacy=True).chunks

        for index, chunk in enumerate(sections):
            docs.append(
                {
                    "id": f"{doc_id}-{index + 1}",
                    "doc_id": doc_id,
                    "title": title,
                    "category": category,
                    "version": meta.get("version", ""),
                    "section": chunk.section,
                    "content": chunk.content,
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
