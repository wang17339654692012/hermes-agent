"""Tests for skill_loader — 技能加载和参数解析。"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure the review module is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.skill_loader import (
    ReviewSkill,
    load_review_skill,
    _find_skill_dir,
    _parse_search_config,
)


class TestReviewSkill:
    """ReviewSkill dataclass tests."""

    def test_default_values(self):
        skill = ReviewSkill()
        assert skill.review_standard == ""
        assert skill.search_platforms == []
        assert skill.search_rounds == 3
        assert skill.search_domains == []
        assert skill.extract_top_n == 10
        assert skill.extract_chars == 5000

    def test_full_construction(self):
        skill = ReviewSkill(
            review_standard="三级审核标准",
            search_platforms=[{"name": "共产党员网", "domain": "12371.cn"}],
            search_rounds=2,
            search_domains=["12371.cn"],
            extract_top_n=5,
            extract_chars=3000,
            skill_path="/tmp/skills",
        )
        assert skill.review_standard == "三级审核标准"
        assert len(skill.search_platforms) == 1
        assert skill.search_rounds == 2
        assert skill.extract_top_n == 5


class TestFindSkillDir:
    """_find_skill_dir tests."""

    def test_skill_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "official-document-drafting" / "document-review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# 审核标准")

            result = _find_skill_dir(Path(tmp))
            assert result is not None
            assert result == skill_dir

    def test_skill_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _find_skill_dir(Path(tmp))
            assert result is None


class TestParseSearchConfig:
    """_parse_search_config tests."""

    def test_empty_markdown(self):
        config = _parse_search_config("")
        assert config == {}

    def test_platform_table(self):
        md = """
| 平台 | 网址 | 效果 |
|------|------|:---:|
| 共产党员网 | 12371.cn | ✅ |
| 党建网 | dangjian.cn | ✅ |
"""
        config = _parse_search_config(md)
        assert len(config["platforms"]) == 2
        assert config["platforms"][0]["name"] == "共产党员网"
        assert config["platforms"][0]["domain"] == "12371.cn"
        assert config["platforms"][1]["domain"] == "dangjian.cn"
        assert "12371.cn" in config["domains"]
        assert "dangjian.cn" in config["domains"]

    def test_rounds_parsing(self):
        md = "第 1 轮：宽泛检索\n第 2 轮：精准检索\n第 3 轮：深挖疑点"
        config = _parse_search_config(md)
        assert config["rounds"] == 3

    def test_top_n_parsing(self):
        md = "Top 10 结果提取全文"
        config = _parse_search_config(md)
        assert config["top_n"] == 10

    def test_char_limit_parsing(self):
        md = "每篇限制 5000 字符"
        config = _parse_search_config(md)
        assert config["extract_chars"] == 5000

    def test_header_row_skipped(self):
        md = "| 平台 | 网址 | 效果 |\n|------|------|:---:|\n| 共产党员网 | 12371.cn | ✅ |"
        config = _parse_search_config(md)
        # 表头行 "平台 | 网址 | 效果" 应被跳过
        assert len(config.get("platforms", [])) == 1
        assert config["platforms"][0]["domain"] == "12371.cn"
        assert config["platforms"][0]["name"] == "共产党员网"


class TestLoadReviewSkill:
    """load_review_skill integration tests."""

    def test_load_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "official-document-drafting" / "document-review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# 审核标准\n三级审核")
            refs_dir = skill_dir / "references"
            refs_dir.mkdir()
            (refs_dir / "party-building-search.md").write_text(
                "| 平台 | 网址 | 效果 |\n|------|------|:---:|\n| 党建网 | dangjian.cn | ✅ |\n第 1 轮\n第 2 轮\nTop 10\n5000 字符"
            )

            with patch("hermes_constants.get_hermes_home", return_value=Path(tmp)):
                skill = load_review_skill()
                assert skill is not None
                assert "三级审核" in skill.review_standard
                assert len(skill.search_platforms) == 1
                assert skill.search_platforms[0]["domain"] == "dangjian.cn"
                assert skill.search_rounds == 2
                assert skill.extract_top_n == 10

    def test_load_skill_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("hermes_constants.get_hermes_home", return_value=Path(tmp)):
                skill = load_review_skill()
                assert skill is None

    def test_load_no_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "official-document-drafting" / "document-review"
            skill_dir.mkdir(parents=True)
            # No SKILL.md

            with patch("hermes_constants.get_hermes_home", return_value=Path(tmp)):
                skill = load_review_skill()
                assert skill is None

    def test_load_defaults_when_no_search_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "official-document-drafting" / "document-review"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# 审核标准")

            with patch("hermes_constants.get_hermes_home", return_value=Path(tmp)):
                skill = load_review_skill()
                assert skill is not None
                # Should use defaults
                assert len(skill.search_platforms) == 2  # 默认两个平台
                assert skill.search_rounds == 3
                assert skill.extract_top_n == 10