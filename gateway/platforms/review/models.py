"""审核模块共享数据类。"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Paragraph:
    """文档段落"""
    index: int
    text: str
    style: str = "Normal"
    runs: Optional[list] = None


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