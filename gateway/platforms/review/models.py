"""审核模块共享数据类。"""

from dataclasses import dataclass, field
from typing import Optional, List

# 纯内容审核文种：非《党政机关公文处理工作条例》15 种法定公文。
# 这类文种只做内容审核（政策符合性 + 事实 + 语言）与防编造约束，
# 跳过公文格式要素的确定性检查（标题三要素/主送机关/落款/附件/序号等）。
CONTENT_ONLY_DOC_TYPES = frozenset({"宣传稿件"})


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
