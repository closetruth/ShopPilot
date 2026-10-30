"""BM25 关键词检索。

中文必须分词，否则 BM25 退化成按字匹配，专有名词和数字条款的精确性
全丢。用 jieba 的默认词典即可——政策文本是通用中文，不需要自定义词典。

索引在内存里构建，47 条政策重算成本可忽略；条目数上千时应改为持久化。
"""

from __future__ import annotations

import logging
import re


import jieba
from rank_bm25 import BM25Okapi

from app import store

logger = logging.getLogger(__name__)

# jieba 默认词典会把业务复合词切错：「退货运费」被切成「退 / 货运费」，
# 「退货」「运费」两个关键 token 全丢，导致 BM25 排序完全跑偏。
# 这里补一批电商售后领域的固定搭配。
DOMAIN_WORDS = (
    "退货", "换货", "退款", "仅退款", "运费", "运费险", "退货运费",
    "退换货", "无理由", "七天无理由", "价保", "差价", "发票", "开票",
    "抬头", "税号", "丢件", "破损", "虚假签收", "超时未发货", "催发货",
    "物流停滞", "会员权益", "极速退款", "投诉", "维权", "转人工",
    "质量问题", "描述不符", "错发", "漏发", "凭证", "保修", "维修",
)

for _word in DOMAIN_WORDS:
    jieba.add_word(_word)

# 只保留中文、字母、数字；标点与空白都是噪声
_TOKEN_RE = re.compile(r"[一-龥a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """分词并归一化。英文统一小写，数字保留原样。"""
    tokens: list[str] = []
    for chunk in _TOKEN_RE.findall(text):
        # 纯数字或中文短语整体保留，混合片段交给 jieba
        for token in jieba.cut(chunk):
            token = token.strip().lower()
            if token and _TOKEN_RE.fullmatch(token):
                tokens.append(token)
    return tokens


def document_text(row: dict) -> str:
    """BM25 用的文档文本。与向量侧的 faq_document 保持同样的字段组合，
    否则两种策略比的不是同一份内容。"""
    return f"{row['title']} {row['section']} {row['content']}"


class BM25Index:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.corpus = [tokenize(document_text(r)) for r in rows]
        self.ids = [r["id"] for r in rows]
        # 空语料时 BM25Okapi 会除零，退化为返回空结果
        self._bm25 = BM25Okapi(self.corpus) if self.corpus else None

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        if self._bm25 is None:
            return []

        tokens = tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            zip(self.ids, scores), key=lambda x: x[1], reverse=True
        )[:top_k]

        # BM25 分数无上界，归一化到 0–1 便于与向量分数并列展示。
        # 只用于展示，排序不受影响。
        peak = max((s for _, s in ranked), default=0.0) or 1.0
        return [
            {"id": doc_id, "score": float(score), "normalized": float(score) / peak}
            for doc_id, score in ranked
            if score > 0
        ]


_index: BM25Index | None = None


def get_index(rows: list[dict] | None = None) -> BM25Index:
    """取索引。

    默认用库里启用的条目，进程内缓存。知识库变更后必须调 reset_index()，
    否则会继续用旧索引——条目数不变时无法自动察觉内容已改。
    """
    global _index
    if rows is not None:
        return BM25Index(rows)
    if _index is None:
        _index = BM25Index(store.all_faq_for_index())
    return _index


def reset_index() -> None:
    """知识库变更后清缓存。"""
    global _index
    _index = None
