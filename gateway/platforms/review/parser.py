"""文档解析 — .docx / .pdf → 段落列表。"""

from pathlib import Path
from typing import List, Optional

from .models import Paragraph


def parse_document(file_bytes: bytes, filename: str) -> List[Paragraph]:
    """解析文档为段落列表。

    Args:
        file_bytes: 文档二进制内容
        filename: 原始文件名（用于判断扩展名）

    Returns:
        Paragraph 列表

    Raises:
        ValueError: 不支持的文档格式
    """
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        return _parse_docx(file_bytes)
    elif ext == ".pdf":
        return _parse_pdf(file_bytes)
    else:
        raise ValueError(f"Unsupported format: {ext}. Use .docx or .pdf")


def _parse_docx(file_bytes: bytes) -> List[Paragraph]:
    """解析 .docx，保留段落结构和样式信息"""
    from io import BytesIO
    from docx import Document

    doc = Document(BytesIO(file_bytes))
    paragraphs = []

    for i, para in enumerate(doc.paragraphs, start=1):
        text = para.text.strip()
        if not text:
            continue  # 跳过空段落
        paragraphs.append(Paragraph(
            index=i,
            text=text,
            style=para.style.name if para.style else "Normal",
            runs=list(para.runs),
        ))

    return paragraphs


def _parse_pdf(file_bytes: bytes) -> List[Paragraph]:
    """解析 PDF，按段落分割"""
    import fitz  # pymupdf

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    paragraphs = []
    para_index = 0

    for page in doc:
        text = page.get_text("text")
        # 按双换行分割段落
        for block in text.split("\n\n"):
            block = block.strip()
            if block:
                para_index += 1
                paragraphs.append(Paragraph(
                    index=para_index,
                    text=block,
                    style="Normal",
                ))

    return paragraphs