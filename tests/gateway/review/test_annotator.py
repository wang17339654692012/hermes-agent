"""Tests for annotator — Word 批注生成。"""

import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.annotator import (
    generate_annotated_docx,
    _generate_sync,
    SEVERITY_COLORS,
    SEVERITY_LABELS,
)
from gateway.platforms.review.models import Annotation, Paragraph


def _make_test_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("第一段测试内容。")
    doc.add_paragraph("第二段测试内容，包含问题。")
    doc.add_paragraph("第三段正常内容。")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_test_paragraphs() -> list:
    return [
        Paragraph(index=1, text="第一段测试内容。"),
        Paragraph(index=2, text="第二段测试内容，包含问题。"),
        Paragraph(index=3, text="第三段正常内容。"),
    ]


def _make_test_annotations() -> list:
    return [
        Annotation(
            paragraph_index=2,
            severity="critical",
            issue_type="政治表述错误",
            description="测试描述",
            suggestion="修改建议",
            reference="参考来源",
        ),
        Annotation(
            paragraph_index=1,
            severity="suggestion",
            issue_type="措辞优化",
            description="可以更规范",
            suggestion="优化建议",
            reference="参考来源2",
        ),
    ]


class TestSeverityConstants:
    """常量测试."""

    def test_colors_defined(self):
        assert SEVERITY_COLORS["critical"] == "FF0000"
        assert SEVERITY_COLORS["important"] == "FFC000"
        assert SEVERITY_COLORS["suggestion"] == "00B050"

    def test_labels_defined(self):
        assert "严重" in SEVERITY_LABELS["critical"]
        assert "重要" in SEVERITY_LABELS["important"]
        assert "建议" in SEVERITY_LABELS["suggestion"]


class TestGenerateAnnotatedDocx:
    """generate_annotated_docx tests."""

    def test_generate_with_annotations(self):
        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = _make_test_annotations()

        result = _generate_sync(file_bytes, "test.docx", paragraphs, annotations)
        assert isinstance(result, bytes)
        assert len(result) > 0
        # 应该比原始文件大（多了批注和汇总表）
        assert len(result) > len(file_bytes)

    def test_generate_no_annotations(self):
        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = []

        result = _generate_sync(file_bytes, "test.docx", paragraphs, annotations)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_generate_empty_document(self):
        from docx import Document

        doc = Document()
        buf = BytesIO()
        doc.save(buf)

        result = _generate_sync(buf.getvalue(), "test.docx", [], [])
        assert isinstance(result, bytes)

    def test_output_is_valid_docx(self):
        from docx import Document

        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = _make_test_annotations()

        result = _generate_sync(file_bytes, "test.docx", paragraphs, annotations)

        # 验证可以重新打开
        doc = Document(BytesIO(result))
        # 应该有原始段落 + 汇总表相关内容
        assert len(doc.paragraphs) >= 3

    def test_no_summary_table(self):
        """汇总表已移除，文档中不应出现。"""
        from docx import Document

        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = _make_test_annotations()

        result = _generate_sync(file_bytes, "test.docx", paragraphs, annotations)

        doc = Document(BytesIO(result))
        for para in doc.paragraphs:
            assert "审核汇总表" not in para.text, "汇总表不应出现"

    def test_comments_present(self):
        """批注应写入 comments.xml。"""
        import zipfile
        from lxml import etree

        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = _make_test_annotations()

        result = _generate_sync(file_bytes, "test.docx", paragraphs, annotations)

        with zipfile.ZipFile(BytesIO(result), "r") as z:
            assert "word/comments.xml" in z.namelist()
            xml = z.read("word/comments.xml")
            root = etree.fromstring(xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            comments = root.findall(".//w:comment", ns)
            assert len(comments) == 2  # 2 annotations

    @pytest.mark.asyncio
    async def test_async_wrapper(self):
        file_bytes = _make_test_docx()
        paragraphs = _make_test_paragraphs()
        annotations = _make_test_annotations()

        result = await generate_annotated_docx(
            file_bytes, "test.docx", paragraphs, annotations
        )
        assert isinstance(result, bytes)
        assert len(result) > 0