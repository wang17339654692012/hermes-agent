"""Tests for comparator — LLM 逐段审核。"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.comparator import (
    compare_paragraphs,
    _build_reference_text,
    _parse_annotations,
)
from gateway.platforms.review.models import Annotation, Paragraph
from gateway.platforms.review.skill_loader import ReviewSkill


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