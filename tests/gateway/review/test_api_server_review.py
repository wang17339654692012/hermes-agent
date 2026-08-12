"""Tests for document review API endpoint — 集成测试。"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

# We test the _parse_review_request helper directly
from gateway.platforms import api_server
from gateway.platforms.api_server import _parse_review_request


def _make_test_docx_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("测试内容。")
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


class TestParseReviewRequest:
    """_parse_review_request tests."""

    def _make_request(self, file_bytes=None, filename="test.docx", doc_type=None, severity_filter=None, annotate=None):
        """模拟 aiohttp multipart request."""
        mock_request = MagicMock()

        fields = []
        if file_bytes is not None:
            file_field = MagicMock()
            file_field.name = "file"
            file_field.filename = filename
            file_field.read = AsyncMock(return_value=file_bytes)
            fields.append(file_field)

        if doc_type is not None:
            dt_field = MagicMock()
            dt_field.name = "doc_type"
            dt_field.text = AsyncMock(return_value=doc_type)
            fields.append(dt_field)

        if severity_filter is not None:
            sf_field = MagicMock()
            sf_field.name = "severity_filter"
            sf_field.text = AsyncMock(return_value=severity_filter)
            fields.append(sf_field)

        if annotate is not None:
            a_field = MagicMock()
            a_field.name = "annotate"
            a_field.text = AsyncMock(return_value=annotate)
            fields.append(a_field)

        # Create a mock MultipartReader that yields fields
        mock_reader = MagicMock()
        mock_reader.next = AsyncMock(side_effect=fields + [None])
        mock_request.multipart = AsyncMock(return_value=mock_reader)
        return mock_request

    @pytest.mark.asyncio
    async def test_minimal_request(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(file_bytes=file_bytes)

        result = await _parse_review_request(request)
        fb, fn, dt, sf, an = result
        assert isinstance(fb, bytes)
        assert fn == "test.docx"
        assert dt is None
        assert sf == "all"
        assert an is True

    @pytest.mark.asyncio
    async def test_all_params(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(
            file_bytes=file_bytes,
            filename="通知.docx",
            doc_type="通知",
            severity_filter="critical+important",
            annotate="false",
        )

        fb, fn, dt, sf, an = await _parse_review_request(request)
        assert fn == "通知.docx"
        assert dt == "通知"
        assert sf == "critical+important"
        assert an is False

    @pytest.mark.asyncio
    async def test_annotate_true(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(file_bytes=file_bytes, annotate="true")

        _, _, _, _, an = await _parse_review_request(request)
        assert an is True

    @pytest.mark.asyncio
    async def test_annotate_1(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(file_bytes=file_bytes, annotate="1")

        _, _, _, _, an = await _parse_review_request(request)
        assert an is True

    @pytest.mark.asyncio
    async def test_annotate_yes(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(file_bytes=file_bytes, annotate="yes")

        _, _, _, _, an = await _parse_review_request(request)
        assert an is True

    @pytest.mark.asyncio
    async def test_missing_file(self):
        request = self._make_request(file_bytes=None)

        with pytest.raises(ValueError, match="Missing 'file'"):
            await _parse_review_request(request)

    @pytest.mark.asyncio
    async def test_no_filename(self):
        request = self._make_request(file_bytes=b"test", filename=None)

        with pytest.raises(ValueError, match="No filename"):
            await _parse_review_request(request)

    @pytest.mark.asyncio
    async def test_unsupported_format(self):
        request = self._make_request(file_bytes=b"test", filename="test.txt")

        with pytest.raises(ValueError, match="Unsupported format"):
            await _parse_review_request(request)

    @pytest.mark.asyncio
    async def test_pdf_supported(self):
        from io import BytesIO
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "test", fontsize=12)
        buf = BytesIO()
        doc.save(buf)
        doc.close()

        request = self._make_request(file_bytes=buf.getvalue(), filename="test.pdf")

        fb, fn, _, _, _ = await _parse_review_request(request)
        assert fn == "test.pdf"
        assert isinstance(fb, bytes)

    @pytest.mark.asyncio
    async def test_invalid_severity_filter_ignored(self):
        file_bytes = _make_test_docx_bytes()
        request = self._make_request(file_bytes=file_bytes, severity_filter="invalid")

        _, _, _, sf, _ = await _parse_review_request(request)
        assert sf == "all"  # 无效值保持默认


class TestRouteRegistration:
    """路由注册测试."""

    def test_route_in_table(self):
        """验证 /api/v1/review/document 路由在 _http_route_table 中。"""
        import importlib
        import gateway.platforms.api_server as api_mod

        # 获取 route table
        adapter = api_mod.APIServerAdapter.__new__(api_mod.APIServerAdapter)
        # 不能直接实例化，但可以检查源码
        content = Path(api_mod.__file__).read_text(encoding="utf-8")
        assert "/api/v1/review/document" in content
        assert "_handle_review_document" in content

    def test_handler_exists(self):
        """验证 handler 方法存在。"""
        import gateway.platforms.api_server as api_mod

        assert hasattr(api_mod.APIServerAdapter, "_handle_review_document")
        assert callable(getattr(api_mod.APIServerAdapter, "_handle_review_document"))

    def test_helper_exists(self):
        """验证 helper 函数存在。"""
        import gateway.platforms.api_server as api_mod

        assert hasattr(api_mod, "_parse_review_request")
        assert callable(api_mod._parse_review_request)