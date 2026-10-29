"""Markdown 结构化切分：把文档拆成可直接入库的知识条目。

纯函数，不依赖数据库与向量库。管理界面的批量导入与 `scripts/import_faq.py`
共用同一套解析，后者走 `legacy=True` 保持与历史输出逐字一致。

设计要点：
- 代码围栏与表格视为原子块，内部不识别标题
- 叶子节点成条；父节点的直属正文按长度决定独立成条还是并入首个子块
- 单节超长按段落装箱，单段过长回退到句末或硬切
- 不足 20 字的条目只打警告，不跨节合并——合并会丢掉 section 语义
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# 与 app/main.py 的 FaqPayload 保持一致
MIN_CONTENT = 20
MAX_SECTION = 64
DEFAULT_MAX_CHARS = 500

FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.DOTALL)
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
TABLE_ROW_RE = re.compile(r"^ {0,3}\|")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-{2,})[ \t]*$")

PARAGRAPH_SEP = "\n\n"
SENTENCE_END = "。！？!?；;"


@dataclass
class Chunk:
    section: str
    content: str
    path: list[str] = field(default_factory=list)
    kind: Literal["leaf", "preamble", "fulltext"] = "leaf"
    part: tuple[int, int] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.content)


@dataclass
class SplitResult:
    title: str
    category: str
    frontmatter: dict
    chunks: list[Chunk]
    warnings: list[str] = field(default_factory=list)


# ---------- frontmatter ----------


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML 风格的头部。只支持单层 key: value，够用即可。"""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


# ---------- 原子块识别 ----------


def _atomic_lines(lines: list[str]) -> list[bool]:
    """标记哪些行属于原子块（围栏或表格），这些行内的 # 不当作标题。

    返回与 lines 等长的布尔表。围栏标记的开闭行、表格的每一行都为 True。
    """
    flags = [False] * len(lines)
    fence_char: str | None = None
    fence_len = 0
    in_table = False

    for i, line in enumerate(lines):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
                flags[i] = True
                in_table = False
                continue
            if marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
                fence_len = 0
                flags[i] = True
                continue

        if fence_char is not None:
            flags[i] = True
            continue

        is_row = bool(TABLE_ROW_RE.match(line))
        if is_row:
            flags[i] = True
            in_table = True
        elif in_table and not line.strip():
            # 空行结束表格；空行本身不算原子内容
            in_table = False
        elif in_table:
            in_table = False

    return flags


# ---------- 标题树 ----------


@dataclass
class _Node:
    level: int
    title: str
    body: list[str]
    children: list["_Node"] = field(default_factory=list)

    @property
    def own_text(self) -> str:
        """去掉首尾空行。旧实现用 .strip()，这里保持一致。"""
        return "\n".join(self.body).strip("\n").strip()


def _build_tree(
    lines: list[str], atomic: list[bool], levels: set[int]
) -> tuple[list[_Node], list[str]]:
    """按标题层级建树。返回 (根节点列表, 正文前置内容)。"""
    roots: list[_Node] = []
    stack: list[_Node] = []
    preamble: list[str] = []
    warnings: list[str] = []

    for i, line in enumerate(lines):
        if not atomic[i]:
            heading = HEADING_RE.match(line)
            if heading:
                level = len(heading.group(1))
                if level in levels:
                    node = _Node(level=level, title=heading.group(2).strip(), body=[])
                    # 弹出所有层级 >= 当前节点的祖先：同级标题互为兄弟、
                    # 更高级标题终结当前分支，两种情况都要出栈
                    while stack and stack[-1].level >= level:
                        stack.pop()
                    if stack:
                        stack[-1].children.append(node)
                    else:
                        roots.append(node)
                    stack.append(node)
                    continue
            elif SETEXT_RE.match(line) and i > 0 and not atomic[i - 1]:
                warnings.append("检测到下划线式标题（Setext），暂不支持，按正文处理")

        if stack:
            stack[-1].body.append(line)
        else:
            preamble.append(line)

    return roots, preamble


# ---------- 超长切分 ----------


def _split_long(text: str, max_chars: int) -> list[str]:
    """把超长正文切成多个不超过 max_chars 的块。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in text.split(PARAGRAPH_SEP) if p.strip()]

    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
        else:
            pieces.extend(_split_paragraph(para, max_chars))

    blocks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + len(PARAGRAPH_SEP) + len(piece) <= max_chars:
            current = current + PARAGRAPH_SEP + piece
        else:
            blocks.append(current)
            current = piece
    if current:
        blocks.append(current)

    return blocks


def _split_paragraph(para: str, max_chars: int) -> list[str]:
    """单段超长：优先在句末断开，退而求其次硬切。"""
    pieces: list[str] = []
    rest = para

    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = -1
        for i in range(len(window) - 1, max(0, len(window) - 100) - 1, -1):
            if window[i] in SENTENCE_END:
                cut = i + 1
                break
        if cut <= 0:
            cut = max_chars
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()

    if rest:
        pieces.append(rest)
    return pieces


# ---------- 命名 ----------


def _section_name(
    path: list[str], mode: Literal["leaf", "path"], suffix: str = ""
) -> str:
    base = path[-1] if mode == "leaf" else " / ".join(path)
    room = MAX_SECTION - len(suffix)
    if len(base) > room:
        base = base[: max(1, room - 1)] + "…"
    return base + suffix


# ---------- 主流程 ----------


def _flag_tiny(chunks: list[Chunk]) -> list[Chunk]:
    """给不足 20 字的条目打警告，但不跨节合并。

    早先的实现会把短条目并进相邻条目，结果两节含义不同的知识被合成一条、
    丢掉了 section 名。宁可让它在预览里显示「过短」由人来补充，
    也不要静默篡改结构。
    """
    for chunk in chunks:
        if len(chunk.content) < MIN_CONTENT:
            chunk.warnings.append(
                f"正文仅 {len(chunk.content)} 字，不足 {MIN_CONTENT} 字，入库前请补充"
            )
    return chunks


def split_markdown(
    text: str,
    *,
    title: str | None = None,
    category: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    section_mode: Literal["leaf", "path"] = "leaf",
    legacy: bool = False,
) -> SplitResult:
    """把 Markdown 切成知识条目。

    legacy=True 时只识别 `##` 且平铺，与旧的 split_sections 行为一致，
    供 scripts/import_faq.py 保持存量数据不变。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    meta, body = parse_frontmatter(normalized)

    resolved_title = (title or meta.get("title") or "").strip()
    resolved_category = (category or meta.get("category") or "未分类").strip()

    lines = body.split("\n")
    atomic = _atomic_lines(lines)
    levels = {2} if legacy else set(range(1, 7))

    roots, preamble = _build_tree(lines, atomic, levels)
    preamble_text = "\n".join(preamble).strip()
    warnings: list[str] = []

    # legacy 用于脚本导入，必须保持历史输出：不切长、不合并碎块、不做
    # preamble 拆分，一切按旧 split_sections 的语义来
    if legacy:
        return _legacy_result(
            roots, preamble_text, meta, resolved_title, resolved_category, warnings
        )

    # 无任何标题：整篇作一条
    if not roots:
        content = preamble_text
        if not content:
            return SplitResult(
                title=resolved_title,
                category=resolved_category,
                frontmatter=meta,
                chunks=[],
                warnings=["文档为空"],
            )
        blocks = _split_long(content, max_chars)
        chunks = [
            Chunk(
                section=_section_name(
                    ["全文"], section_mode, _part_suffix(i, len(blocks))
                ),
                content=block,
                path=["全文"],
                kind="fulltext",
                part=(i + 1, len(blocks)) if len(blocks) > 1 else None,
            )
            for i, block in enumerate(blocks)
        ]
        return SplitResult(
            title=resolved_title,
            category=resolved_category,
            frontmatter=meta,
            chunks=_flag_tiny(chunks),
            warnings=warnings,
        )

    chunks: list[Chunk] = []

    def emit(node: _Node, path: list[str]) -> None:
        here = path + [node.title]
        own = node.own_text
        carried = ""

        if node.children:
            # 父节点：直属正文够长才独立成条，否则并入首个子块
            if own and len(own) >= MIN_CONTENT:
                chunks.extend(
                    _make_chunks(own, here, section_mode, max_chars, "preamble")
                )
            elif own:
                carried = own

            first_child_index = len(chunks)
            for child in node.children:
                emit(child, here)

            if carried and first_child_index < len(chunks):
                chunks[first_child_index].content = (
                    carried + PARAGRAPH_SEP + chunks[first_child_index].content
                ).strip()
            elif carried:
                chunks.extend(
                    _make_chunks(carried, here, section_mode, max_chars, "preamble")
                )
            return

        if own:
            chunks.extend(_make_chunks(own, here, section_mode, max_chars, "leaf"))

    for root in roots:
        emit(root, [])

    # 文档开头的无标题内容：并入第一条
    if preamble_text:
        if chunks:
            chunks[0].content = (preamble_text + PARAGRAPH_SEP + chunks[0].content).strip()
        else:
            chunks.extend(
                _make_chunks(
                    preamble_text, ["全文"], section_mode, max_chars, "preamble"
                )
            )

    return SplitResult(
        title=resolved_title,
        category=resolved_category,
        frontmatter=meta,
        chunks=_flag_tiny(chunks),
        warnings=warnings,
    )


def _make_chunks(
    text: str,
    path: list[str],
    section_mode: Literal["leaf", "path"],
    max_chars: int,
    kind: Literal["leaf", "preamble"],
) -> list[Chunk]:
    blocks = _split_long(text, max_chars)
    total = len(blocks)
    return [
        Chunk(
            section=_section_name(path, section_mode, _part_suffix(i, total)),
            content=block,
            path=list(path),
            kind=kind,
            part=(i + 1, total) if total > 1 else None,
        )
        for i, block in enumerate(blocks)
    ]


def _part_suffix(index: int, total: int) -> str:
    if total <= 1:
        return ""
    return f"（{index + 1}/{total}）"


def _legacy_result(
    roots: list[_Node],
    preamble_text: str,
    meta: dict,
    title: str,
    category: str,
    warnings: list[str],
) -> SplitResult:
    """复现旧 split_sections 的行为，供 scripts/import_faq.py 保持存量数据不变。

    与新版的三处差异：
    - 不切长：一条小节再长也整条输出
    - 不合并碎块：短小节原样保留（脚本不走 API 校验）
    - 无标题时的「全文」用整段原文，而不是从标题树推导
    """
    chunks: list[Chunk] = []

    if not roots:
        if preamble_text:
            chunks.append(
                Chunk(
                    section="全文",
                    content=preamble_text,
                    path=["全文"],
                    kind="fulltext",
                )
            )
        return SplitResult(
            title=title, category=category, frontmatter=meta,
            chunks=chunks, warnings=warnings,
        )

    # legacy 只认 ##，树是平铺的，逐个输出即可
    for node in roots:
        own = node.own_text
        if own:
            chunks.append(
                Chunk(section=node.title, content=own, path=[node.title], kind="leaf")
            )

    return SplitResult(
        title=title, category=category, frontmatter=meta,
        chunks=chunks, warnings=warnings,
    )
