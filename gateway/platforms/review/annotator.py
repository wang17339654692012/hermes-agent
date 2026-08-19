"""生成带批注的 Word 文档 — 仅 annotate=true 时调用。"""

import asyncio
from io import BytesIO
from typing import List

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from .models import Annotation


# ─── 级别标签（彩色圆点 + 文字；● U+25CF 几乎所有字体都带，
#     不依赖 emoji 字形，Word/WPS 均正常显示）───
SEVERITY_LABELS = {
    "critical":    "● 严重",
    "important":   "● 重要",
    "suggestion":  "● 建议",
}

# 级别标签颜色（w:color 渲染器属性，任何客户端强制生效）
SEVERITY_LABEL_COLORS = {
    "critical":    "FF0000",   # 红
    "important":   "BF8F00",   # 橙/深黄（浅黄文字不可读）
    "suggestion":  "0070C0",   # 蓝
}


async def generate_annotated_docx(
    file_bytes: bytes,
    filename: str,
    annotations: List[Annotation],
) -> bytes:
    """生成带批注和高亮的 Word 文档。

    在 executor 中运行，避免阻塞 aiohttp 事件循环。
    """
    return await asyncio.get_event_loop().run_in_executor(
        None,
        _generate_sync,
        file_bytes, filename, annotations,
    )


def _generate_sync(
    file_bytes: bytes,
    filename: str,
    annotations: List[Annotation],
) -> bytes:
    """同步生成批注文档"""
    doc = Document(BytesIO(file_bytes))

    # 建立 annotation 索引：{paragraph_index: [Annotation, ...]}
    anno_map: dict = {}
    for a in annotations:
        anno_map.setdefault(a.paragraph_index, []).append(a)

    # ── 第 1 步：逐段添加 Comment ──
    # 解析器 index 即 document.paragraphs 的枚举序号（含空段，见 parser.py），
    # 因此按文档位置锚定，避免文本匹配在重复段落时错位。
    for doc_idx, para in enumerate(doc.paragraphs, start=1):
        if doc_idx not in anno_map:
            continue

        for anno in anno_map[doc_idx]:
            _add_comment(doc, para, anno)

    # ── 第 2 步：将 comments 的 lxml 修改写回 Part blob ──
    _flush_comments(doc)

    # ── 第 4 步：保存到 BytesIO ──
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _add_comment(doc: Document, para, anno: Annotation) -> None:
    """在段落右侧添加 Word 原生 Comment 气泡。

    Word 的 Comment 结构在 OOXML 中：
    - comments.xml 存储所有批注
    - document.xml 中通过 commentRangeStart / commentRangeEnd 标记范围
    - commentReference 标记批注锚点
    """
    comments_element, _ = _ensure_comments_part(doc)
    comment_id = _next_comment_id(comments_element)
    para_element = para._element

    # commentRangeStart
    etree.SubElement(
        para_element,
        qn("w:commentRangeStart"),
        {qn("w:id"): str(comment_id)},
    )

    # commentRangeEnd
    etree.SubElement(
        para_element,
        qn("w:commentRangeEnd"),
        {qn("w:id"): str(comment_id)},
    )

    # commentReference（批注锚点）
    ref = etree.SubElement(para_element, qn("w:r"))
    etree.SubElement(ref, qn("w:rPr"))
    etree.SubElement(
        ref,
        qn("w:commentReference"),
        {qn("w:id"): str(comment_id)},
    )

    # 在 comments.xml 中写入批注内容
    comment = etree.SubElement(
        comments_element,
        qn("w:comment"),
        {
            qn("w:id"): str(comment_id),
            qn("w:author"): "公文审核助手",
            qn("w:date"): "2026-08-11T00:00:00Z",
        },
    )

    comment_text = (
        f"【{SEVERITY_LABELS.get(anno.severity, anno.severity)}】\n"
        f"问题类型：{anno.issue_type}\n"
        f"问题说明：{anno.description}\n"
        f"修改建议：{anno.suggestion}\n"
        f"参考依据：{anno.reference}"
    )

    for idx, line in enumerate(comment_text.split("\n")):
        p = etree.SubElement(comment, qn("w:p"))
        r = etree.SubElement(p, qn("w:r"))
        r_pr = etree.SubElement(r, qn("w:rPr"))
        # 级别行（第一行）：着色 + 加粗，替代 emoji 区分级别
        if idx == 0:
            color = etree.SubElement(r_pr, qn("w:color"))
            color.set(qn("w:val"), SEVERITY_LABEL_COLORS.get(anno.severity, "000000"))
            etree.SubElement(r_pr, qn("w:b"))
        t = etree.SubElement(r, qn("w:t"))
        t.text = line
        t.set(qn("xml:space"), "preserve")


def _ensure_comments_part(doc: Document):
    """确保 Word 文档中存在 comments 部件。

    返回 (comments_element, comments_part) 元组。
    首次调用时创建部件并缓存，后续调用返回缓存的同一对象。
    """
    # 使用缓存
    if hasattr(doc, '_review_comments_element'):
        return doc._review_comments_element, doc._review_comments_part

    # 检查是否已有 comments 部件
    for rel in doc.part.rels.values():
        if "comments" in rel.reltype:
            part = rel.target_part
            element = etree.fromstring(part.blob)
            # 缓存
            doc._review_comments_element = element
            doc._review_comments_part = part
            return element, part

    # 创建新的 comments 部件
    from docx.opc.part import Part
    from docx.opc.packuri import PackURI

    comments_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "</w:comments>"
    )
    comments_element = etree.fromstring(comments_xml.encode("utf-8"))

    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    partname = PackURI("/word/comments.xml")

    comments_part = Part(
        partname, content_type, etree.tostring(comments_element), doc.part.package
    )

    doc.part.relate_to(
        comments_part,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    )

    # 缓存
    doc._review_comments_element = comments_element
    doc._review_comments_part = comments_part

    return comments_element, comments_part


def _flush_comments(doc: Document) -> None:
    """将 lxml 中修改的 comments 内容写回 Part 的 blob。

    _add_comment 修改的是 _ensure_comments_part 返回的 lxml 元素，
    和 Part._blob 是独立的。此处将修改后的元素序列化回 Part._blob。
    """
    if hasattr(doc, '_review_comments_element'):
        element = doc._review_comments_element
        part = doc._review_comments_part
        part._blob = etree.tostring(
            element,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )


def _next_comment_id(comments_element) -> int:
    """获取下一个可用的 comment ID"""
    existing = comments_element.findall(qn("w:comment"))
    if not existing:
        return 0
    max_id = max(int(c.get(qn("w:id"), "0")) for c in existing)
    return max_id + 1