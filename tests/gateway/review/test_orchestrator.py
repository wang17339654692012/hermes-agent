"""Tests for orchestrator — 审核流程编排。"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.orchestrator import (
    ReviewOrchestrator,
    Annotation,
    _apply_region_policy,
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

    @pytest.fixture(autouse=True)
    def _no_format_checks(self, monkeypatch):
        """run 测试聚焦管线接线，屏蔽确定性格式检查（格式层单独测试）。"""
        from gateway.platforms.review import format_checker

        monkeypatch.setattr(
            format_checker, "check", lambda *a, **k: []
        )

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


def _pars(*texts: str) -> list:
    return [Paragraph(index=i + 1, text=t) for i, t in enumerate(texts) if t]


_JOBBAO_DOC = [
    "济南能源集团有限公司",
    "作业活动梳理及“作业宝”APP建设",
    "专题培训",
    "（代通知）",
    "一、培训时间和地点",
    "时间：2026年6月17日至18日（共2天）",
    "二、培训对象",
    "集团公司及权属企业M8及以上职级管理人员",
    "附件：",
    "1.日程安排表",
    "济南能源集团有限公司",
    "2026年6月15日",
    "附件1",
    "日程安排表",
]


class TestRegionPolicy:
    """_apply_region_policy — 用户决策落地的确定性过滤。"""

    def _paras_with_regions(self):
        from gateway.platforms.review.docmodel import build_document

        paras = _pars(*_JOBBAO_DOC)
        build_document(paras)
        return paras

    def _mk(self, index, issue_type, description, reference, severity="important"):
        return Annotation(
            paragraph_index=index, severity=severity, issue_type=issue_type,
            description=description, suggestion="s", reference=reference,
        )

    def test_title_block_format_claim_dropped(self):
        paras = self._paras_with_regions()
        annos = [self._mk(1, "政治表述", "标题只有机关名称，缺少事由和文种", "r")]
        assert _apply_region_policy(annos, paras) == []

    def test_title_block_typo_kept(self):
        paras = self._paras_with_regions()
        annos = [self._mk(1, "错别字", "机关名称漏字，应为完整名称", "r")]
        kept = _apply_region_policy(annos, paras)
        assert len(kept) == 1

    def test_attachment_page_claim_dropped(self):
        paras = self._paras_with_regions()
        annos = [self._mk(13, "用词规范性", "附件1 名称过于简略", "r")]
        assert _apply_region_policy(annos, paras) == []

    def test_signature_date_hallucination_dropped(self):
        """成文日期段（SIGNATURE 区）的日期类批注一律撤销。"""
        paras = self._paras_with_regions()
        annos = [self._mk(12, "政治表述", "成文日期与当前日期矛盾，日期数字间有空格", "r")]
        assert _apply_region_policy(annos, paras) == []

    def test_structural_opinion_dropped(self):
        paras = self._paras_with_regions()
        annos = [self._mk(5, "结构不合理", "缺少通知缘由部分", "r")]
        assert _apply_region_policy(annos, paras) == []

    def test_unverified_subjective_dropped(self):
        paras = self._paras_with_regions()
        annos = [self._mk(8, "用词规范性", "M8 表述不规范", "未在权威来源中检索到对应表述，请人工核实")]
        assert _apply_region_policy(annos, paras) == []

    def test_unverified_factual_kept(self):
        """事实类（错别字/漏字）即使无权威依据也保留。"""
        paras = self._paras_with_regions()
        annos = [self._mk(6, "错别字", "综合办公室箱漏字，应为邮箱", "未在权威来源中检索到对应表述")]
        kept = _apply_region_policy(annos, paras)
        assert len(kept) == 1

    def test_title_block_claim_with_yingwei_not_exempted(self):
        """标题区"缺文种"批注描述含"应为"（建议措辞）不豁免 → 删除。"""
        paras = self._paras_with_regions()
        annos = [
            self._mk(1, "标题不完整", "标题缺少文种要素。标题应为“济南能源集团有限公司关于开展…的通知”。", "r", severity="critical")
        ]
        assert _apply_region_policy(annos, paras) == []

    def test_date_claim_in_body_dropped(self):
        """正文区的日期类批注（含培训时间/成文日期）一律撤销。"""
        paras = self._paras_with_regions()
        annos = [
            self._mk(6, "日期错误", "培训时间为2026年6月17日至18日，但成文日期早于该时间。", "r", severity="critical")
        ]
        assert _apply_region_policy(annos, paras) == []

    def test_inference_claim_dropped(self):
        """推断式结论（"根据…推断"）无客观依据 → 删除（含 SIGNATURE 区错别字类）。"""
        paras = self._paras_with_regions()
        annos = [
            self._mk(
                12, "错别字",
                "成文日期中的年份存在笔误。根据公文内容推断，该通知涉及的培训规划依据为《规划（2024—2028年）》。",
                "r", severity="critical",
            )
        ]
        assert _apply_region_policy(annos, paras) == []

    def test_last_item_claim_on_non_last_dropped(self):
        """"段末分号应改句号"断言：段后仍有内容时分号是正确的非末项分隔 → 删除。"""
        paras = self._paras_with_regions()
        annos = [
            self._mk(6, "标点符号使用不当", "段落末尾使用分号，但该句为通知事项的完整陈述句，应使用句号。", "r")
        ]
        assert _apply_region_policy(annos, paras) == []

    def test_last_item_claim_variant_mo_xiang_dropped(self):
        """"末项"措辞变体（LLM 换词规避）：段后仍有内容 → 删除。"""
        paras = self._paras_with_regions()
        annos = [
            self._mk(6, "标点符号使用不当", "段落末尾使用分号，但该句为通知事项的末项，此处应使用句号。", "r")
        ]
        assert _apply_region_policy(annos, paras) == []

    def test_last_item_claim_on_actual_last_kept(self):
        """最后一段上的"最后一项"断言属实 → 保留。"""
        from gateway.platforms.review.docmodel import build_document

        paras = _pars("一、培训时间和地点", "（六）各参训人员须按时到场；")
        build_document(paras)
        annos = [self._mk(2, "标点符号使用不当", "该句为通知事项最后一条，段末分号应使用句号。", "r")]
        kept = _apply_region_policy(annos, paras)
        assert len(kept) == 1

    def test_body_annotation_kept(self):
        paras = self._paras_with_regions()
        annos = [self._mk(6, "标点符号", "逗号使用不当", "参考材料")]
        kept = _apply_region_policy(annos, paras)
        assert len(kept) == 1


class TestRunMergesFormatAndContent:
    """run() 合并确定性格式批注 + LLM 内容批注（真实 format_checker）。"""

    @patch("gateway.platforms.review.orchestrator.load_review_skill")
    @patch("gateway.platforms.review.orchestrator.parse_document")
    @patch("gateway.platforms.review.orchestrator._call_llm")
    @patch("gateway.platforms.review.orchestrator.multi_round_search")
    @patch("gateway.platforms.review.orchestrator.compare_paragraphs")
    @pytest.mark.asyncio
    async def test_format_and_content_merged_with_policy(
        self, mock_compare, mock_search, mock_llm, mock_parse, mock_load
    ):
        from gateway.platforms.review.skill_loader import ReviewSkill

        mock_load.return_value = ReviewSkill(
            review_standard="标准",
            search_platforms=[{"name": "党建网", "domain": "dangjian.cn"}],
        )
        mock_parse.return_value = _pars(*_JOBBAO_DOC)
        mock_llm.return_value = '{"themes": ["党建"], "expressions": [], "references": []}'
        mock_search.return_value = []
        mock_compare.return_value = [
            Annotation(
                paragraph_index=1, severity="critical",
                issue_type="政治表述", description="标题只有机关名称", suggestion="s", reference="r",
            ),  # 标题块 → 策略撤销
            Annotation(
                paragraph_index=8, severity="important",
                issue_type="用词规范性", description="M8 不规范", suggestion="s",
                reference="未在权威来源中检索到对应表述",
            ),  # 无依据主观 → 删除
            Annotation(
                paragraph_index=6, severity="critical",
                issue_type="错别字", description="综合办公室箱漏字", suggestion="s", reference="r",
            ),  # 事实类 → 保留
        ]

        orch = ReviewOrchestrator(
            file_bytes=b"test", filename="test.docx", annotate=False,
        )
        result = await orch.run()

        assert "error" not in result
        types = {i["type"] for i in result["issues"]}
        # 确定性格式：主送机关缺失（作业宝文档真实存在）
        assert "主送机关缺失" in types
        # LLM 内容：错别字保留；标题块政治表述、无依据主观被过滤
        assert "错别字" in types
        assert "政治表述" not in types
        assert "用词规范性" not in types