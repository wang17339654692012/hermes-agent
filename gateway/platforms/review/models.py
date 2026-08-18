"""审核模块共享数据类。"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Paragraph:
    """文档段落

    text：已包含渲染编号前缀（如「六、有关事项」）的完整文本。
    role / region：docmodel 结构定位填充（标题行/正文/落款/附件等）。
    number：渲染出的编号前缀（如「六、」），无编号时为空串。
    """
    index: int
    text: str
    style: str = "Normal"
    runs: Optional[list] = None
    # 结构化字段（docmodel 填充，缺省保持旧行为）
    role: str = ""
    region: str = ""
    number: str = ""
    in_table: bool = False
    outline_level: Optional[int] = None


@dataclass
class Annotation:
    """审核批注"""
    paragraph_index: int
    severity: str          # critical / important / suggestion
    issue_type: str
    description: str
    suggestion: str
    reference: str         # URL + 日期
    original_text: str = ""
