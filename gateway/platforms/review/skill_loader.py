"""加载 document-review 技能内容，作为审核流程的运行时配置。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict
import logging
import re

logger = logging.getLogger(__name__)

# 仓库内置技能目录（checkout 部署场景开箱即用，无需手动部署技能）。
# skill_loader.py 位于 gateway/platforms/review/，向上三级即仓库根。
REPO_SKILL_DIR = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "official-document-drafting"
    / "document-review"
)


@dataclass
class ReviewSkill:
    """审核技能的运行时表示"""
    # 审核规范（来自 SKILL.md）
    review_standard: str = ""
    # 检索策略（来自 party-building-search.md）
    search_platforms: List[Dict] = field(default_factory=list)
    search_rounds: int = 3
    search_domains: List[str] = field(default_factory=list)
    extract_top_n: int = 10
    extract_chars: int = 5000
    # 元信息
    skill_path: str = ""


def load_review_skill() -> Optional[ReviewSkill]:
    """加载 document-review 技能。

    查找顺序（用户部署优先，仓库内置兜底）：
    1. $HERMES_HOME/skills/official-document-drafting/document-review/
    2. ~/.hermes/skills/official-document-drafting/document-review/
    3. 仓库 skills/official-document-drafting/document-review/
    """
    try:
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
    except Exception:
        hermes_home = Path.home() / ".hermes"

    skill_dir = _find_skill_dir(hermes_home)
    if not skill_dir:
        logger.warning("document-review 技能目录未找到")
        return None

    # 读取 SKILL.md
    skill_md_path = skill_dir / "SKILL.md"
    if not skill_md_path.exists():
        logger.warning("SKILL.md 未找到: %s", skill_md_path)
        return None

    skill_md = skill_md_path.read_text(encoding="utf-8")

    # 读取检索策略引用文件
    search_md_path = skill_dir / "references" / "party-building-search.md"
    search_md = ""
    if search_md_path.exists():
        search_md = search_md_path.read_text(encoding="utf-8")

    # 从检索策略中提取结构化参数
    search_config = _parse_search_config(search_md)

    defaults = {
        "platforms": [
            {"name": "共产党员网", "domain": "12371.cn"},
            {"name": "党建网", "domain": "dangjian.cn"},
        ],
        "domains": ["12371.cn", "dangjian.cn"],
        "rounds": 3,
        "top_n": 10,
        "extract_chars": 5000,
    }

    return ReviewSkill(
        review_standard=skill_md,
        search_platforms=search_config.get("platforms", defaults["platforms"]),
        search_rounds=search_config.get("rounds", defaults["rounds"]),
        search_domains=search_config.get("domains", defaults["domains"]),
        extract_top_n=search_config.get("top_n", defaults["top_n"]),
        extract_chars=search_config.get("extract_chars", defaults["extract_chars"]),
        skill_path=str(skill_dir),
    )


def _find_skill_dir(hermes_home: Path) -> Optional[Path]:
    """查找 document-review 技能目录。

    优先级：用户部署（hermes home）> 仓库内置。
    """
    candidates = [
        hermes_home / "skills" / "official-document-drafting" / "document-review",
        Path.home() / ".hermes" / "skills" / "official-document-drafting" / "document-review",
        REPO_SKILL_DIR,
    ]
    for c in candidates:
        if (c / "SKILL.md").exists():
            return c
    return None


def _parse_search_config(search_md: str) -> dict:
    """从 party-building-search.md 中提取检索参数。

    解析 Markdown 表格和关键字段，提取：
    - 检索平台列表（name + domain）
    - 检索轮次数
    - Top N 和字符限制
    """
    if not search_md:
        return {}

    config: dict = {"platforms": [], "domains": []}

    # 解析平台表格（"| 平台 | 网址 | ... |"）
    # 匹配三列表格行：| name | domain | notes |
    platform_pattern = re.compile(r'\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|')
    separator_pattern = re.compile(r'^\|[\s\-:]+\|[\s\-:]+\|')
    for match in platform_pattern.finditer(search_md):
        name = match.group(1).strip()
        domain = match.group(2).strip()
        # 跳过表头行和分隔行
        if not name or not domain:
            continue
        if re.match(r'^[-:]+$', name) or re.match(r'^[-:]+$', domain):
            continue
        if domain.startswith('网址') or name.startswith('平台'):
            continue
        if '.' in domain:
            config["platforms"].append({"name": name, "domain": domain})
            if domain not in config["domains"]:
                config["domains"].append(domain)

    # 解析轮次
    all_rounds = re.findall(r'第\s*(\d+)\s*轮', search_md)
    if all_rounds:
        config["rounds"] = max(int(r) for r in all_rounds)

    # 解析 Top N
    top_match = re.search(r'Top\s*(\d+)', search_md)
    if top_match:
        config["top_n"] = int(top_match.group(1))

    # 解析字符限制
    char_match = re.search(r'(\d+)\s*字符', search_md)
    if char_match:
        config["extract_chars"] = int(char_match.group(1))

    return config