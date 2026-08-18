"""文档结构模型 — 一次性确定结构，供格式审核与 LLM 审核共享。

职责：
1. Word 自动编号渲染：numbering.xml + 段落 numPr → 把「六、」等编号
   拼进段落文本（Word 的编号存在 XML 里，不在段落文本中，此前解析层
   直接丢弃，导致格式审核与 LLM 都看不到真实序号）。
2. 段落角色与区域切分：标题块 / 正文 / 落款 / 附件说明 / 附件页。

结构只在此处解析一次，format_checker 与 comparator 消费同一模型，
不再各自用正则反推（此前每层各自猜测是误报反复出现的根因）。
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .models import Paragraph

logger = logging.getLogger(__name__)

# ── 区域常量 ──
REGION_TITLE_BLOCK = "TITLE_BLOCK"        # 正文之前的多行标题区
REGION_BODY = "BODY"                      # 正文（含主送机关/章节/条款）
REGION_SIGNATURE = "SIGNATURE"            # 落款（署名 + 成文日期）
REGION_ATTACHMENT_LIST = "ATTACHMENT_LIST"  # 正文尾部的附件说明（落款前）
REGION_ATTACHMENT_PAGES = "ATTACHMENT_PAGES"  # 落款之后的附件页（标记+标题+表格）

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{_W}}}{tag}"


# ── 结构判定正则（与历史 format_checker 同一套，收敛到此处）──
_CHAPTER_HEAD_PATTERN = re.compile(r"^[一二三四五六七八九十\d]+[、.．]")
_SENDER_PATTERN = re.compile(r"[:：]$")
_BODY_OPENING_PATTERN = re.compile(r"^(为|根据|按照|现|经|接)")
_SIGNATURE_PATTERN = re.compile(
    r"(公司|集团|委员会|人民政府|党委|局|厅|部|院|中心|队|学校)$"
)
_FULL_DATE_PATTERN = re.compile(r"^[（(]?\d{4}年\d{1,2}月\d{1,2}日")
_ATTACHMENT_PATTERN = re.compile(r"^附件")
_ATTACHMENT_NUMBER_ONLY_PATTERN = re.compile(r"^附件\s*\d+$")
_ATTACHMENT_ITEM_PATTERN = re.compile(r"^\d+[、.．]")


def _cn_number(n: int) -> str:
    """阿拉伯数字 → 中文数字（1-99，用于 chineseCounting 编号）。"""
    cn = "一二三四五六七八九十"
    if n <= 0:
        return str(n)
    if n <= 10:
        return cn[n - 1]
    if n < 20:
        return "十" + (cn[n - 11] if n > 10 else "")
    tens, ones = divmod(n, 10)
    return cn[tens - 1] + "十" + (cn[ones - 1] if ones else "")


def _format_number(value: int, num_fmt: Optional[str]) -> str:
    """按 numFmt 渲染编号值。"""
    if num_fmt in (None, "decimal"):
        return str(value)
    if num_fmt == "chineseCounting":
        return _cn_number(value)
    # 其他格式（lowerLetter/upperLetter/…）少见，原样输出数字，不阻断
    return str(value)


class NumberingRenderer:
    """Word 自动编号渲染器。

    OOXML 中列表编号存储在 word/numbering.xml：numId → abstractNumId →
    lvl{numFmt, lvlText, start}。段落通过 numPr/numId 引用，渲染值按
    同一 numId 的段落顺序递增（同一 numId 即一个列表实例）。
    """

    def __init__(self, numbering_xml: Optional[bytes] = None):
        self._num_to_abstract: Dict[int, int] = {}
        self._levels: Dict[int, Dict[int, Dict[str, str]]] = {}
        self._counters: Dict[int, int] = {}
        if numbering_xml:
            self._parse(numbering_xml)

    def _parse(self, xml_bytes: bytes) -> None:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            logger.warning("numbering.xml 解析失败，跳过自动编号: %s", e)
            return
        for num in root.findall(_qn("num")):
            num_id_el = num.get(_qn("numId"))
            abs_el = num.find(_qn("abstractNumId"))
            if num_id_el is not None and abs_el is not None:
                try:
                    self._num_to_abstract[int(num_id_el)] = int(abs_el.get(_qn("val")))
                except (TypeError, ValueError):
                    continue
        for an in root.findall(_qn("abstractNum")):
            abs_id_el = an.get(_qn("abstractNumId"))
            if abs_id_el is None:
                continue
            try:
                abs_id = int(abs_id_el)
            except (TypeError, ValueError):
                continue
            for lvl in an.findall(_qn("lvl")):
                try:
                    ilvl = int(lvl.get(_qn("ilvl"), "0"))
                except (TypeError, ValueError):
                    ilvl = 0

                def _val(tag: str) -> Optional[str]:
                    el = lvl.find(_qn(tag))
                    return el.get(_qn("val")) if el is not None else None

                self._levels.setdefault(abs_id, {})[ilvl] = {
                    "numFmt": _val("numFmt"),
                    "lvlText": _val("lvlText"),
                    "start": _val("start"),
                }

    def render(self, num_id: Optional[int], ilvl: int = 0) -> str:
        """返回该段落渲染出的编号文本（如「六、」）；不可识别时返回 ''。

        计数器按 numId 递增（同一 numId 的段落共享一个列表实例）。
        仅处理一级列表（ilvl=0）——公文章节基本使用一级编号，多级列表
        暂不渲染（记录为已知局限，不阻断解析）。
        """
        if num_id is None or ilvl != 0:
            return ""
        counter = self._counters.get(num_id, 0) + 1
        self._counters[num_id] = counter
        abstract_id = self._num_to_abstract.get(num_id)
        if abstract_id is None:
            return ""
        lvl = self._levels.get(abstract_id, {}).get(0)
        if not lvl:
            return ""
        try:
            start = int(lvl.get("start") or "1")
        except (TypeError, ValueError):
            start = 1
        value = start + counter - 1
        number = _format_number(value, lvl.get("numFmt"))
        lvl_text = lvl.get("lvlText") or "%1."
        return lvl_text.replace("%1", number)


# ── 文档模型 ──


@dataclass
class Document:
    """解析后的文档结构模型（区域定位，供审核层消费）。"""

    paragraphs: List[Paragraph] = field(default_factory=list)
    first_marker: Optional[int] = None            # 正文第一个标记段 index
    title_block: Set[int] = field(default_factory=set)
    sign_line_index: Optional[int] = None         # 落款署名段 index
    sign_date_index: Optional[int] = None         # 成文日期段 index
    attachment_page_start: Optional[int] = None   # 附件页起始 index（落款后）


def _is_sender_line(text: str) -> bool:
    text = text.strip()
    return (
        len(text) <= 40
        and bool(_SENDER_PATTERN.search(text))
        and not _BODY_OPENING_PATTERN.match(text)
        and not text.startswith("附件")
    )


def _is_signature_line(text: str) -> bool:
    text = text.strip()
    return len(text) <= 30 and bool(_SIGNATURE_PATTERN.search(text))


def _is_sign_date_line(text: str) -> bool:
    return bool(_FULL_DATE_PATTERN.match(text.strip()))


def _is_attachment_list_head(text: str) -> bool:
    """正文附件说明头：「附件：」或「附件1：」样式（非附件页纯序号行）。"""
    text = text.strip()
    return bool(_ATTACHMENT_PATTERN.match(text)) and not _ATTACHMENT_NUMBER_ONLY_PATTERN.match(text)


def build_document(paragraphs: List[Paragraph]) -> Document:
    """对段落列表执行一次结构定位，填充 role/region 并返回 Document。

    纯函数：不依赖 Word 元数据，docx 与 pdf 解析共用。
    """
    doc = Document(paragraphs=paragraphs)
    by_index = {p.index: p.text for p in paragraphs}
    nonempty = sorted(i for i, t in by_index.items() if t.strip())
    if not nonempty:
        return doc

    # ── 标题块：正文第一个标记之前的段落 ──
    first_marker = None
    for i in nonempty:
        t = by_index[i].strip()
        if (
            _CHAPTER_HEAD_PATTERN.match(t)
            or _is_sender_line(t)
            or _BODY_OPENING_PATTERN.match(t)
        ):
            first_marker = i
            break
    if first_marker is None:
        first_marker = nonempty[-1]
    doc.first_marker = first_marker
    doc.title_block = {i for i in nonempty if i < first_marker}

    # ── 落款：最后一个署名段及其附近的成文日期 ──
    sign_line = None
    for i in reversed(nonempty):
        if i > first_marker and _is_signature_line(by_index[i]):
            sign_line = i
            break
    doc.sign_line_index = sign_line

    sign_date = None
    if sign_line is not None:
        for i in range(max(sign_line - 2, min(nonempty)), min(sign_line + 3, max(nonempty) + 1)):
            if i == sign_line or i not in by_index:
                continue
            if _is_sign_date_line(by_index[i]):
                sign_date = i
                break
    if sign_date is None:
        for i in reversed(nonempty):
            if i <= first_marker:
                break
            if _is_sign_date_line(by_index[i]):
                sign_date = i
                break
    doc.sign_date_index = sign_date

    # ── 附件页起始：落款之后第一个「附件N」纯序号行 ──
    boundary = sign_date if sign_date is not None else sign_line
    if boundary is not None:
        for i in nonempty:
            if i > boundary and _ATTACHMENT_NUMBER_ONLY_PATTERN.match(by_index[i].strip()):
                doc.attachment_page_start = i
                break

    # ── 逐段填 role + region ──
    for p in paragraphs:
        t = p.text.strip()
        idx = p.index
        if idx in doc.title_block:
            p.region = REGION_TITLE_BLOCK
            p.role = "title_line"
            if _is_signature_line(t) and len(t) <= 30:
                p.role = "title_org"  # 机关名称行
            elif re.search(r"（代通知）$", t):
                p.role = "title_genre"  # 文种标记行
            continue
        if doc.attachment_page_start is not None and idx >= doc.attachment_page_start:
            p.region = REGION_ATTACHMENT_PAGES
            if _ATTACHMENT_NUMBER_ONLY_PATTERN.match(t):
                p.role = "attachment_marker"
            elif _CHAPTER_HEAD_PATTERN.match(t):
                p.role = "chapter_head"
            else:
                p.role = "attachment_title"
            continue
        if sign_date is not None and idx == sign_date:
            p.region = REGION_SIGNATURE
            p.role = "sign_date"
            continue
        if sign_line is not None and idx == sign_line:
            p.region = REGION_SIGNATURE
            p.role = "signature"
            continue
        if _is_attachment_list_head(t):
            p.region = REGION_ATTACHMENT_LIST
            p.role = "attachment_list_head"
            continue
        if (
            _ATTACHMENT_ITEM_PATTERN.match(t)
            and sign_date is not None
            and idx < sign_date
        ):
            p.region = REGION_ATTACHMENT_LIST
            p.role = "attachment_list_item"
            continue
        if _is_sender_line(t):
            p.region = REGION_BODY
            p.role = "sender"
            continue
        if _CHAPTER_HEAD_PATTERN.match(t):
            p.region = REGION_BODY
            p.role = "chapter_head"
            continue
        p.region = REGION_BODY
        p.role = "body"

    return doc


def enrich(paragraphs: List[Paragraph], numbering: Optional[NumberingRenderer]) -> List[Paragraph]:
    """渲染自动编号 + 结构定位，原地填充并返回段落列表。"""
    for p in paragraphs:
        if p.number:
            p.text = p.number + p.text
    build_document(paragraphs)
    return paragraphs
