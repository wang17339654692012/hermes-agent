"""Tests for comparator — LLM 逐段审核。"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.comparator import (
    compare_paragraphs,
    _build_reference_text,
    _build_review_system_prompt,
    _build_user_prompt,
    _parse_annotations,
)
from gateway.platforms.review.models import Annotation, Paragraph
from gateway.platforms.review.skill_loader import ReviewSkill


class TestBuildReviewSystemPrompt:
    """_build_review_system_prompt tests — 审核范围 + 文种声明。"""

    def test_includes_skill_standard(self):
        skill = ReviewSkill(review_standard="三级审核标准全文")
        prompt = _build_review_system_prompt(skill, doc_type=None)
        assert "三级审核标准全文" in prompt

    def test_includes_doc_type(self):
        skill = ReviewSkill(review_standard="标准")
        prompt = _build_review_system_prompt(skill, doc_type="通知")
        assert "通知" in prompt

    def test_without_doc_type_declares_unknown(self):
        """未识别文种时显式声明，避免模型凭空假设文种。"""
        skill = ReviewSkill(review_standard="标准")
        prompt = _build_review_system_prompt(skill, doc_type=None)
        assert "文种" in prompt

    def test_covers_language_and_policy_review(self):
        """审核范围必须同时覆盖政策对比与语言/格式审核（需求 3.3）。"""
        skill = ReviewSkill(review_standard="标准")
        prompt = _build_review_system_prompt(skill, doc_type="报告")
        assert "语言" in prompt or "格式" in prompt
        assert "参考材料" in prompt


class TestBuildUserPrompt:
    """_build_user_prompt tests — 空检索时的防编造显式指令。"""

    def test_empty_refs_injects_instruction(self):
        """检索无结果时显式注入防编造指令，不依赖模型自觉。"""
        prompt = _build_user_prompt(reference_text="", paragraphs_text="[段落 1] 内容")
        assert "未检索到" in prompt
        assert "人工核实" in prompt

    def test_with_refs_shows_material(self):
        prompt = _build_user_prompt(
            reference_text="【参考 1】来源：http://a.com",
            paragraphs_text="[段落 1] 内容",
        )
        assert "http://a.com" in prompt
        assert "未检索到" not in prompt


class TestBuildReferenceText:
    """_build_reference_text tests."""

    def test_empty_refs(self):
        text = _build_reference_text([], 5000)
        assert text == ""

    def test_single_ref(self):
        refs = [{
            "url": "http://12371.cn/article",
            "date": "2026-03-15",
            "content": "全面从严治党是党永葆生机活力的必由之路。",
        }]
        text = _build_reference_text(refs, 5000)
        assert "http://12371.cn/article" in text
        assert "2026-03-15" in text
        assert "全面从严治党" in text

    def test_char_limit(self):
        refs = [{
            "url": "http://a.com",
            "date": "2026-01-01",
            "content": "A" * 100,
        }]
        text = _build_reference_text(refs, 50)
        assert len(text) < 200  # includes URL + date + formatting

    def test_multiple_refs(self):
        refs = [
            {"url": "http://a.com", "date": "2026-01-01", "content": "Content A"},
            {"url": "http://b.com", "date": "2026-02-01", "content": "Content B"},
        ]
        text = _build_reference_text(refs, 5000)
        assert "参考 1" in text
        assert "参考 2" in text
        assert "---" in text  # separator


class TestParseAnnotations:
    """_parse_annotations tests."""

    def test_valid_json(self):
        response = '''[
            {"paragraph_index": 3, "severity": "critical", "issue_type": "政治表述错误",
             "description": "描述", "suggestion": "建议", "reference": "参考",
             "original_text": "原文"}
        ]'''
        annotations = _parse_annotations(response)
        assert len(annotations) == 1
        assert annotations[0].paragraph_index == 3
        assert annotations[0].severity == "critical"

    def test_multiple_annotations(self):
        response = '''[
            {"paragraph_index": 1, "severity": "critical", "issue_type": "A", "description": "d", "suggestion": "s", "reference": "r"},
            {"paragraph_index": 5, "severity": "important", "issue_type": "B", "description": "d", "suggestion": "s", "reference": "r"}
        ]'''
        annotations = _parse_annotations(response)
        assert len(annotations) == 2

    def test_no_json(self):
        response = "没有发现问题。"
        annotations = _parse_annotations(response)
        assert annotations == []

    def test_invalid_json(self):
        response = "[{invalid json}]"
        annotations = _parse_annotations(response)
        assert annotations == []

    def test_empty_response(self):
        annotations = _parse_annotations("")
        assert annotations == []

    def test_json_with_text_around(self):
        response = '以下是审核结果：\n[{"paragraph_index": 1, "severity": "suggestion", "issue_type": "T", "description": "d", "suggestion": "s", "reference": "r"}]\n审核完毕。'
        annotations = _parse_annotations(response)
        assert len(annotations) == 1

    def test_missing_fields_default(self):
        response = '[{"paragraph_index": 1, "severity": "critical"}]'
        annotations = _parse_annotations(response)
        assert len(annotations) == 1
        assert annotations[0].issue_type == ""
        assert annotations[0].description == ""


class TestCompareParagraphs:
    """compare_paragraphs integration tests."""

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_successful_comparison(self, mock_llm):
        mock_llm.return_value = '''[
            {"paragraph_index": 1, "severity": "important", "issue_type": "引用不规范",
             "description": "描述", "suggestion": "建议", "reference": "参考"}
        ]'''

        skill = ReviewSkill(
            review_standard="三级审核标准",
            extract_chars=5000,
        )

        paragraphs = [
            Paragraph(index=1, text="第一条内容"),
            Paragraph(index=2, text="第二条内容"),
        ]
        refs = [{"url": "http://a.com", "date": "2026-01-01", "content": "参考内容"}]

        annotations = await compare_paragraphs(
            paragraphs=paragraphs,
            refs=refs,
            skill=skill,
            batch_size=8,
        )
        assert len(annotations) == 1
        assert annotations[0].severity == "important"

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_no_issues(self, mock_llm):
        mock_llm.return_value = "[]"

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=1, text="内容")]
        refs = []

        annotations = await compare_paragraphs(
            paragraphs=paragraphs,
            refs=refs,
            skill=skill,
        )
        assert annotations == []

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_llm_error_handled(self, mock_llm):
        mock_llm.side_effect = Exception("API Error")

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=1, text="内容")]
        refs = []

        annotations = await compare_paragraphs(
            paragraphs=paragraphs,
            refs=refs,
            skill=skill,
        )
        assert annotations == []  # 错误不中断，返回空

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_batching(self, mock_llm):
        mock_llm.return_value = "[]"

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=i, text=f"段落{i}") for i in range(1, 20)]
        refs = []

        await compare_paragraphs(
            paragraphs=paragraphs,
            refs=refs,
            skill=skill,
            batch_size=5,  # 4 batches
        )
        assert mock_llm.call_count == 4  # 19/5 = 4 batches

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_batches_run_concurrently_but_bounded(self, mock_llm):
        """批次之间无数据依赖：并发执行；信号量将并发上限限制在 3。"""
        import asyncio

        from gateway.platforms.review.comparator import MAX_CONCURRENT_BATCHES

        entries = 0
        reached_cap = asyncio.Event()
        release = asyncio.Event()

        async def slow_call(**kwargs):
            nonlocal entries
            entries += 1
            if entries == MAX_CONCURRENT_BATCHES:
                reached_cap.set()
            await release.wait()
            return "[]"

        mock_llm.side_effect = slow_call

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=i, text=f"段落{i}") for i in range(1, 41)]

        task = asyncio.create_task(
            compare_paragraphs(paragraphs=paragraphs, refs=[], skill=skill, batch_size=8)
        )
        # 等信号量被占满（3 个批次并发执行中）
        await asyncio.wait_for(reached_cap.wait(), timeout=5)
        assert entries == MAX_CONCURRENT_BATCHES  # 并发被信号量封顶
        await asyncio.sleep(0.05)
        assert entries == MAX_CONCURRENT_BATCHES  # 第 4 个批次被信号量阻塞
        release.set()
        await task
        assert mock_llm.call_count == 5  # 40/8 = 5 batches

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_doc_type_passed_into_system_prompt(self, mock_llm):
        """doc_type 必须注入审核 prompt，模型才能按文种聚焦格式标准。"""
        mock_llm.return_value = "[]"

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=1, text="内容")]

        await compare_paragraphs(
            paragraphs=paragraphs,
            refs=[],
            skill=skill,
            doc_type="通知",
        )

        system_prompt = mock_llm.call_args.kwargs["system_prompt"]
        assert "通知" in system_prompt

    @patch("gateway.platforms.review.comparator._call_llm_with_system")
    @pytest.mark.asyncio
    async def test_empty_refs_user_prompt_instruction(self, mock_llm):
        """检索结果为空时，user prompt 携带防编造显式指令。"""
        mock_llm.return_value = "[]"

        skill = ReviewSkill(review_standard="标准", extract_chars=5000)
        paragraphs = [Paragraph(index=1, text="内容")]

        await compare_paragraphs(
            paragraphs=paragraphs,
            refs=[],
            skill=skill,
        )

        user_prompt = mock_llm.call_args.kwargs["user_prompt"]
        assert "未检索到" in user_prompt