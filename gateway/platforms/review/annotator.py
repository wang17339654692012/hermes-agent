"""生成带批注的 Word 文档 — 仅 annotate=true 时调用。"""

import asyncio
from io import BytesIO
from typing import List

from docx import Document
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml
from lxml import etree

from .models import Annotation, Paragraph


# ─── 高亮颜色映射 ───
SEVERITY_COLORS = {
    "critical":    "FF0000",   # 🔴 红色
    "important":   "FFC000",   # 🟡 黄色
    "suggestion":  "00B050",   # 🔵 绿色
}

SEVERITY_LABELS = {
    "critical":    "🔴 严重",
    "important":   "🟡 重要",
    "suggestion":  "🔵 建议",
}


async def generate_annotated_docx(
    file_bytes: bytes,
    filename: str,
    paragraphs: List[Paragraph],
    annotations: List[Annotation],
) -> bytes:
    """生成带批注和高亮的 Word 文档。

    在 executor 中运行，避免阻塞 aiohttp 事件循环。
    """
    return await asyncio.get_event_loop().run_in_executor(
        None,
        _generate_sync,
        file_bytes, filename, paragraphs, annotations,
    )


def _generate_sync(
    file_bytes: bytes,
    filename: str,
    paragraphs: List[Paragraph],
    annotations: List[Annotation],
) -> bytes:
    """同步生成批注文档"""
    doc = Document(BytesIO(file_bytes))

    # 建立 annotation 索引：{paragraph_index: [Annotation, ...]}
    anno_map: dict = {}
    for a in annotations:
        anno_map.setdefault(a.paragraph_index, []).append(a)

    # ── 第 1 步：逐段添加 Comment 和高亮 ──
    for para in doc.paragraphs:
        para_idx = _get_paragraph_index(para, paragraphs)
        if para_idx not in anno_map:
            continue

        for anno in anno_map[para_idx]:
            _add_comment(doc, para, anno)
            _highlight_paragraph(para, anno.severity)

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

    for line in comment_text.split("\n"):
        p = etree.SubElement(comment, qn("w:p"))
        r = etree.SubElement(p, qn("w:r"))
        etree.SubElement(r, qn("w:rPr"))
        t = etree.SubElement(r, qn("w:t"))
        t.text = line
        t.set(qn("xml:space"), "preserve")


def _highlight_paragraph(para, severity: str) -> None:
    """对段落中的所有 Run 应用高亮背景色"""
    color = SEVERITY_COLORS.get(severity, "FFFF00")

    for run in para.runs:
        rPr = run._element.find(qn("w:rPr"))
        if rPr is None:
            rPr = etree.SubElement(run._element, qn("w:rPr"))
            run._element.insert(0, rPr)

        highlight = etree.SubElement(rPr, qn("w:highlight"))
        highlight.set(qn("w:val"), color)


def _add_summary_table(doc: Document, annotations: List[Annotation]) -> None:
    """在文末添加审核汇总表"""
    # 添加分页
    doc.add_page_break()

    # 标题
    doc.add_heading("审核汇总表", level=1)

    # 统计摘要
    critical = sum(1 for a in annotations if a.severity == "critical")
    important = sum(1 for a in annotations if a.severity == "important")
    suggestion = sum(1 for a in annotations if a.severity == "suggestion")

    summary = doc.add_paragraph()
    run_c = summary.add_run(f"🔴 严重（必须修改）：{critical} 条    ")
    run_c.bold = True
    run_i = summary.add_run(f"🟡 重要（建议修改）：{important} 条    ")
    run_i.bold = True
    run_s = summary.add_run(f"🔵 建议（可选优化）：{suggestion} 条")
    run_s.bold = True

    # 表格
    table = doc.add_table(rows=1, cols=6, style="Table Grid")
    table.autofit = True

    # 表头
    headers = ["段落", "级别", "问题类型", "问题说明", "修改建议", "参考依据"]
    for i, header in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = header
        shading = parse_xml(
            f'<w:shd {nsdecls("w")} w:fill="D9E2F3" w:val="clear"/>'
        )
        cell._element.get_or_add_tcPr().append(shading)
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    # 数据行
    for anno in annotations:
        row = table.add_row()
        row.cells[0].text = str(anno.paragraph_index)
        row.cells[1].text = SEVERITY_LABELS.get(anno.severity, anno.severity)
        row.cells[2].text = anno.issue_type
        row.cells[3].text = anno.description
        row.cells[4].text = anno.suggestion
        row.cells[5].text = anno.reference


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


def _get_paragraph_index(para, paragraphs: List[Paragraph]) -> int:
    """通过文本匹配找到段落在原始解析结果中的索引"""
    para_text = para.text.strip()
    for p in paragraphs:
        if p.text.strip() == para_text:
            return p.index
    return 0