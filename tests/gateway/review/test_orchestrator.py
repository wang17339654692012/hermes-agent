"""Tests for orchestrator — 审核流程编排。"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.orchestrator import (
    ReviewOrchestrator,
    Annotation,
    _parse_json_response,
)
from gateway.platforms.review.models import Paragraph


class TestParseJsonResponse:
    """_parse_json_response tests."""

    def test_valid_json(self):
        result = _parse_json_response('{"themes": ["党建", "考核"]}')
        assert result == {"themes": ["党建", "考核"]}

    def test_json_with_markdown(self):
        result = _parse_json_response('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_no_json(self):
        result = _parse_json_response("纯文本无JSON")
        assert result == {}

    def test_empty_string(self):
        result = _parse_json_response("")
        assert result == {}


class TestReviewOrchestratorFilter:
    """ReviewOrchestrator._filter tests."""

    def _make_orchestrator(self, severity_filter="all"):
        return ReviewOrchestrator(
            file_bytes=b"test",
            filename="test.docx",
            severity_filter=severity_filter,
        )

    def _make_annotations(self):
        return [
            Annotation(paragraph_index=1, severity="critical", issue_type="", description="", suggestion="", reference=""),
            Annotation(paragraph_index=2, severity="important", issue_type="", description="", suggestion="", reference=""),
            Annotation(paragraph_index=3, severity="suggestion", issue_type="", description="", suggestion="", reference=""),
        ]

    def test_filter_all(self):
        orch = self._make_orchestrator("all")
        annotations = self._make_annotations()
        filtered = orch._filter(annotations)
        assert len(filtered) == 3

    def test_filter_critical(self):
        orch = self._make_orchestrator("critical")
        annotations = self._make_annotations()
        filtered = orch._filter(annotations)
        assert len(filtered) == 1
        assert filtered[0].severity == "critical"

    def test_filter_critical_important(self):
        orch = self._make_orchestrator("critical+important")
        annotations = self._make_annotations()
        filtered = orch._filter(annotations)
        assert len(filtered) == 2
        severities = {a.severity for a in filtered}
        assert "suggestion" not in severities


class TestReviewOrchestratorFormat:
    """ReviewOrchestrator._format_paragraphs tests."""

    def test_format(self):
        orch = ReviewOrchestrator(file_bytes=b"", filename="test.docx")
        paragraphs = [
            Paragraph(index=1, text="第一段"),
            Paragraph(index=2, text="第二段"),
        ]
        text = orch._format_paragraphs(paragraphs)
        assert "[P1]" in text
        assert "[P2]" in text
        assert "第一段" in text

    def test_format_empty(self):
        orch = ReviewOrchestrator(file_bytes=b"", filename="test.docx")
        text = orch._format_paragraphs([])
        assert text == ""


class TestAnalyzeContentDocType:
    """_analyze_content 文种识别 tests — 需求：传参跳过识别，否则自动识别。"""

    @patch("gateway.platforms.review.orchestrator._call_llm")
    @pytest.mark.asyncio
    async def test_known_doc_type_overrides_llm(self, mock_llm):
        """调用方传了 doc_type 时，以传入值为准（跳过自动识别）。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_llm.return_value = (
            '{"themes": ["党建"], "expressions": [], "references": [], "doc_type": "报告"}'
        )
        orch = ReviewOrchestrator(
            file_bytes=b"test", filename="test.docx", doc_type="通知",
        )
        skill = ReviewSkill(review_standard="标准")

        analysis = await orch._analyze_content(
            [Paragraph(index=1, text="内容")], skill, known_doc_type="通知",
        )
        assert analysis["doc_type"] == "通知"

    @patch("gateway.platforms.review.orchestrator._call_llm")
    @pytest.mark.asyncio
    async def test_llm_detected_doc_type_kept(self, mock_llm):
        """未传 doc_type 时，保留 LLM 自动识别的结果。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_llm.return_value = (
            '{"themes": ["考核"], "expressions": [], "references": [], "doc_type": "报告"}'
        )
        orch = ReviewOrchestrator(file_bytes=b"test", filename="test.docx")
        skill = ReviewSkill(review_standard="标准")

        analysis = await orch._analyze_content(
            [Paragraph(index=1, text="内容")], skill, known_doc_type=None,
        )
        assert analysis["doc_type"] == "报告"


class TestReviewOrchestratorRun:
    """ReviewOrchestrator.run integration tests."""

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @pytest.mark.asyncio
    async def test_run_skill_not_found(self, mock_load):
        mock_load.return_value = None

        orch = ReviewOrchestrator(file_bytes=b"test", filename="test.docx")
        result = await orch.run()
        assert "error" in result
        assert "技能未找到" in result["error"]

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @pytest.mark.asyncio
    async def test_run_parse_failure(self, mock_parse, mock_load):
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(review_standard="标准")
        mock_parse.side_effect = ValueError("解析失败")

        orch = ReviewOrchestrator(file_bytes=b"test", filename="test.docx")
        result = await orch.run()
        assert "error" in result
        assert "解析失败" in result["error"]

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @pytest.mark.asyncio
    async def test_run_empty_document(self, mock_parse, mock_load):
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(review_standard="标准")
        mock_parse.return_value = []

        orch = ReviewOrchestrator(file_bytes=b"test", filename="test.docx")
        result = await orch.run()
        assert "error" in result

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @pytest.mark.asyncio
    async def test_run_full_pipeline_annotate_false(
        self, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = [
            Annotation(
                paragraph_index=1, severity="important",
                issue_type="问题", description="描述", suggestion="建议",
                reference="参考",
            )
        ]

        orch = ReviewOrchestrator(
            file_bytes=b"test",
            filename="test.docx",
            annotate=False,
        )
        result = await orch.run()

        assert "error" not in result
        assert result["summary"]["total_paragraphs"] == 1
        assert result["summary"]["important"] == 1
        assert result["summary"]["critical"] == 0
        assert len(result["issues"]) == 1
        assert "download_url" not in result  # annotate=false

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @pytest.mark.asyncio
    async def test_run_with_severity_filter(
        self, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = [
            Annotation(paragraph_index=1, severity="critical", issue_type="", description="", suggestion="", reference=""),
            Annotation(paragraph_index=2, severity="suggestion", issue_type="", description="", suggestion="", reference=""),
        ]

        orch = ReviewOrchestrator(
            file_bytes=b"test",
            filename="test.docx",
            severity_filter="critical",
            annotate=False,
        )
        result = await orch.run()

        assert result["summary"]["critical"] == 1
        assert result["summary"]["suggestion"] == 0  # filtered out
        assert len(result["issues"]) == 1

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @patch("gateway.platforms.review.orchestrator.generate_annotated_docx")
    @patch.object(ReviewOrchestrator, "_upload_to_minio")
    @pytest.mark.asyncio
    async def test_run_full_pipeline_annotate_true(
        self, mock_upload, mock_annotate, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        """annotate=true 路径：返回 download_url + 生成批注文档。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = [
            Annotation(
                paragraph_index=1, severity="important",
                issue_type="问题", description="描述", suggestion="建议",
                reference="参考",
            )
        ]
        mock_annotate.return_value = b"fake annotated docx bytes"
        mock_upload.return_value = "http://minio:9000/bucket/test_reviewed.docx"

        orch = ReviewOrchestrator(
            file_bytes=b"test",
            filename="test.docx",
            annotate=True,
        )
        result = await orch.run()

        assert "error" not in result
        assert "download_url" in result
        assert result["download_url"] == "http://minio:9000/bucket/test_reviewed.docx"
        assert result["summary"]["total_paragraphs"] == 1
        assert result["summary"]["important"] == 1
        assert len(result["issues"]) == 1
        mock_annotate.assert_called_once()
        mock_upload.assert_called_once()

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @patch("gateway.platforms.review.orchestrator.generate_annotated_docx")
    @pytest.mark.asyncio
    async def test_run_annotate_true_upload_fails(
        self, mock_annotate, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        """annotate=true 但生成批注失败：返回 warning 但不崩溃。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = []
        mock_annotate.side_effect = RuntimeError("生成批注失败")

        orch = ReviewOrchestrator(
            file_bytes=b"test",
            filename="test.docx",
            annotate=True,
        )
        result = await orch.run()

        # 不崩溃，返回 warning
        assert "error" not in result
        assert "warning" in result
        assert "download_url" not in result
        assert "issues" in result

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @patch("gateway.platforms.review.orchestrator.generate_annotated_docx")
    @pytest.mark.asyncio
    async def test_run_pdf_annotate_true_warns_and_skips(
        self, mock_annotate, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        """PDF + annotate=true：显式 warning，跳过批注生成（不靠异常降级）。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = []

        orch = ReviewOrchestrator(
            file_bytes=b"%PDF-1.4", filename="test.pdf", annotate=True,
        )
        result = await orch.run()

        assert "error" not in result
        assert "warning" in result
        assert "PDF" in result["warning"]
        assert "download_url" not in result
        mock_annotate.assert_not_called()

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @pytest.mark.asyncio
    async def test_run_passes_known_doc_type_to_compare(
        self, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        """run() 将调用方传入的 doc_type 透传给逐段审核。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = (
            '{"themes": ["党建"], "expressions": [], "references": [], "doc_type": "报告"}'
        )
        mock_search.return_value = []
        mock_compare.return_value = []

        orch = ReviewOrchestrator(
            file_bytes=b"test", filename="test.docx",
            doc_type="通知", annotate=False,
        )
        await orch.run()

        kwargs = mock_compare.call_args.kwargs
        assert kwargs["doc_type"] == "通知"

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @pytest.mark.asyncio
    async def test_run_passes_llm_detected_doc_type_to_compare(
        self, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        """未传 doc_type 时，run() 将 LLM 自动识别的文种透传给逐段审核。"""
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = [Paragraph(index=1, text="测试")]
        mock_llm.return_value = (
            '{"themes": ["党建"], "expressions": [], "references": [], "doc_type": "报告"}'
        )
        mock_search.return_value = []
        mock_compare.return_value = []

        orch = ReviewOrchestrator(
            file_bytes=b"test", filename="test.docx", annotate=False,
        )
        await orch.run()

        kwargs = mock_compare.call_args.kwargs
        assert kwargs["doc_type"] == "报告"