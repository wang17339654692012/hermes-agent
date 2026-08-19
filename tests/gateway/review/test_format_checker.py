"""Tests for format_checker — 基于 docmodel 结构模型的确定性格式审核。

核心回归场景：作业宝文档（多行标题 + 「（代通知）」文种 + Word 自动编号
渲染后的序号 + 附件页），验证「要素齐全不报、真缺失必报」。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.format_checker import check  # noqa: E402
from gateway.platforms.review.models import Paragraph  # noqa: E402

TODAY = date(2026, 8, 18)


def _pars(*texts: str) -> list:
    """按顺序生成连续 index 的段落（含自动编号渲染后的文本）。"""
    return [Paragraph(index=i + 1, text=t) for i, t in enumerate(texts) if t]


# 作业宝结构：多行标题 + （代通知） + 自动编号渲染后的章节 + 附件页
_JOBBAO = [
    "济南能源集团有限公司",
    "作业活动梳理及“作业宝”APP建设",
    "专题培训",
    "（代通知）",
    "一、培训时间和地点",
    "时间：2026年6月17日至18日（共2天）",
    "二、培训对象",
    "集团公司及权属企业M8及以上职级管理人员",
    "三、培训内容和目标",
    "通过分专题、模块化方式，学习作业活动梳理成果与作业宝APP数字化管控思路。",
    "四、培训费用",
    "严格参照《济南市市属国有企业培训费管理办法》执行。",
    "五、工作分工",
    "（一）集团公司综合办公室负责发布培训通知、收集名单。",
    "六、有关事项",
    "（一）参训人员原则上不得请假。",
    "（六）请各单位将参训人员名单于6月16日12:00前报至集团公司综合办公室箱jnny@jinanenergy.cn。",
    "附件：",
    "1.日程安排表",
    "2.参训人员报名表",
    "3.培训请假单",
    "济南能源集团有限公司",
    "2026年6月15日",
    "附件1",
    "日程安排表",
]


class TestJobbao:
    """作业宝文档：多行标题 + 自动编号 + 附件页均合规 → 仅报真实缺失。"""

    def test_no_title_or_attachment_false_positives(self):
        anns = check(_pars(*_JOBBAO), doc_type="通知", today=TODAY)
        assert not [a for a in anns if "标题" in a.issue_type]
        assert not [a for a in anns if "附件" in a.issue_type]
        assert not [a for a in anns if "序号" in a.issue_type]  # 自动编号已渲染

    def test_missing_sender_reported(self):
        """作业宝文档正文前无主送机关 → 报（真实问题）。"""
        anns = check(_pars(*_JOBBAO), doc_type="通知", today=TODAY)
        sender = [a for a in anns if "主送机关" in a.issue_type]
        assert len(sender) == 1


class TestTitle:
    def test_multi_line_title_with_dai_tongzhi_complete(self):
        """（代通知）是合法文种标记；多行标题要素齐全 → 不报。"""
        paras = _pars(
            "济南能源集团有限公司",
            "作业活动梳理及“作业宝”APP建设",
            "专题培训",
            "（代通知）",
            "一、培训时间和地点",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        assert not [a for a in anns if "标题" in a.issue_type]

    def test_missing_genre_reported(self):
        paras = _pars(
            "济南能源集团有限公司",
            "作业活动梳理及“作业宝”APP建设",
            "专题培训",
            "一、培训时间和地点",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        title_anns = [a for a in anns if "标题" in a.issue_type]
        assert len(title_anns) == 1
        assert "文种" in title_anns[0].description

    def test_no_title_at_all_reported(self):
        paras = _pars("一、培训时间和地点", "时间：2026年6月17日")
        anns = check(paras, doc_type="通知", today=TODAY)
        assert any("标题" in a.issue_type for a in anns)

    def test_all_genres_recognized(self):
        """15 种法定文种标题 → 文种齐全，不报缺文种。"""
        genres = [
            "决议", "决定", "命令", "公报", "公告", "通告", "意见",
            "通知", "通报", "报告", "请示", "批复", "议案", "函", "纪要",
        ]
        for g in genres:
            paras = _pars(
                "济南能源集团有限公司",
                f"关于开展安全生产检查的{g}",
                "一、检查目的",
            )
            anns = check(paras, doc_type="通知", today=TODAY)
            assert not [a for a in anns if "标题" in a.issue_type], g


class TestSender:
    def test_sender_present_no_annotation(self):
        paras = _pars(
            "关于开展安全生产培训的通知",
            "各权属企业：",
            "一、培训时间和地点",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        assert not [a for a in anns if "主送机关" in a.issue_type]

    def test_attachment_colon_not_sender(self):
        """「附件：」不是主送机关 → 正文前无主送机关仍报缺失。"""
        paras = _pars(
            "关于开展安全生产培训的通知",
            "一、培训时间和地点",
            "附件：",
            "1.日程安排表",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        sender = [a for a in anns if "主送机关" in a.issue_type]
        assert len(sender) == 1

    def test_public_genre_no_sender_not_reported(self):
        """公告/通告/公报/决议/命令（公布性文种）本无主送机关 → 不报缺失。"""
        for genre in ("公告", "通告", "公报", "决议", "命令"):
            paras = _pars(
                f"关于开展安全生产检查的{genre}",
                "一、检查目的",
                "济南能源集团有限公司",
                "2026年6月15日",
            )
            anns = check(paras, doc_type=genre, today=TODAY)
            assert not [a for a in anns if "主送机关" in a.issue_type], genre

    def test_minutes_no_sender_not_reported(self):
        """纪要采用出席/列席体系，无主送机关 → 不报缺失。"""
        paras = _pars(
            "安全生产专题会议纪要",
            "一、会议情况",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type="纪要", today=TODAY)
        assert not [a for a in anns if "主送机关" in a.issue_type]

    def test_unknown_genre_no_sender_reported(self):
        """文种未知时保守处理：仍要求主送机关。"""
        paras = _pars(
            "关于开展安全生产培训的通知",
            "一、培训时间和地点",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type=None, today=TODAY)
        sender = [a for a in anns if "主送机关" in a.issue_type]
        assert len(sender) == 1


class TestSignature:
    def _base(self, *tail: str) -> list:
        return _pars(
            "一、培训时间和地点",
            "时间：2026年6月17日至18日",
            *tail,
        )

    def test_complete_no_annotation(self):
        paras = self._base("济南能源集团有限公司", "2026年6月15日")
        anns = check(paras, doc_type="通知", today=TODAY)
        assert not [a for a in anns if "落款" in a.issue_type or "成文日期" in a.issue_type]

    def test_without_date_reported(self):
        paras = self._base("济南能源集团有限公司")
        anns = check(paras, doc_type="通知", today=TODAY)
        found = [a for a in anns if "成文日期" in a.description and "缺少" in a.description]
        assert len(found) == 1

    def test_future_date_reported(self):
        paras = self._base("济南能源集团有限公司", "2026年6月15日")
        anns = check(paras, doc_type="通知", today=date(2026, 1, 1))
        found = [a for a in anns if "晚于当前日期" in a.description]
        assert len(found) == 1

    def test_padded_zero_reported(self):
        paras = self._base("济南能源集团有限公司", "2026年06月15日")
        anns = check(paras, doc_type="通知", today=TODAY)
        assert any("虚位" in a.description for a in anns)


class TestChapterNumbering:
    def test_unnumbered_heading_reported_with_suggestion(self):
        """真无序号的小标题 → 报缺序号，且给出具体建议（含推算序号）。"""
        paras = _pars(
            "一、培训时间和地点",
            "二、培训对象",
            "培训内容和目标",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        found = [a for a in anns if "序号" in a.issue_type]
        assert len(found) == 1
        assert found[0].paragraph_index == 3
        # 建议给出具体序号（一、二 之后应为 三）
        assert "三、" in found[0].suggestion

    def test_auto_numbered_heading_not_flagged(self):
        """Word 自动编号已渲染进文本 → 不报缺序号（作业宝核心回归）。"""
        paras = _pars(
            "一、培训时间和地点",
            "二、培训对象",
            "三、培训内容和目标",
            "四、培训费用",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        assert not [a for a in anns if "序号" in a.issue_type]


class TestAttachments:
    def test_list_head_with_colon_no_annotation(self):
        paras = _pars(
            "一、培训时间和地点",
            "附件：",
            "1.日程安排表",
            "济南能源集团有限公司",
            "2026年6月15日",
            "附件1",
            "日程安排表",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        # 附件说明合规；附件页（落款后）不参与检查
        assert not [a for a in anns if "附件" in a.issue_type]

    def test_head_without_colon_reported(self):
        paras = _pars(
            "一、培训时间和地点",
            "附件",
            "1.日程安排表",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        found = [a for a in anns if "附件" in a.issue_type]
        assert len(found) == 1


class TestDateConsistency:
    def test_deadline_before_sign_date_reported(self):
        paras = _pars(
            "一、培训时间和地点",
            "请各单位于6月10日前报送名单。",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        found = [a for a in anns if "早于成文日期" in a.description]
        assert len(found) == 1

    def test_deadline_after_sign_date_no_annotation(self):
        paras = _pars(
            "一、培训时间和地点",
            "请各单位于6月16日12:00前报送名单。",
            "济南能源集团有限公司",
            "2026年6月15日",
        )
        anns = check(paras, doc_type="通知", today=TODAY)
        assert not [a for a in anns if "早于" in a.description]
