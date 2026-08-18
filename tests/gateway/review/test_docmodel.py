"""Tests for docmodel — 自动编号渲染 + 区域/角色结构定位。

核心回归场景：作业宝文档（多行标题 + Word 自动编号 + 附件页）。
"""

import sys
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.docmodel import (  # noqa: E402
    NumberingRenderer,
    build_document,
    REGION_TITLE_BLOCK,
    REGION_BODY,
    REGION_SIGNATURE,
    REGION_ATTACHMENT_LIST,
    REGION_ATTACHMENT_PAGES,
)
from gateway.platforms.review.models import Paragraph  # noqa: E402
from gateway.platforms.review.parser import parse_document  # noqa: E402


# ─── NumberingRenderer ───

_NUMBERING_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="4"/><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="%1、"/>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0">
      <w:start w:val="3"/><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="%1、"/>
    </w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="2">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>"""


def _renderer() -> NumberingRenderer:
    return NumberingRenderer(_NUMBERING_XML.encode("utf-8"))


class TestNumberingRenderer:
    def test_chinese_counting_with_start(self):
        """作业宝真实场景：numId=1 从 3 起（三、四…）。"""
        r = NumberingRenderer(_NUMBERING_XML)
        assert r.render(1) == "三、"
        assert r.render(1) == "四、"  # 同一 numId 递增

    def test_second_list_start_4(self):
        """numId=2 从 4 起（四、五、六…）——作业宝「有关事项」实为六、。"""
        r = NumberingRenderer(_NUMBERING_XML)
        assert r.render(2) == "四、"
        assert r.render(2) == "五、"
        assert r.render(2) == "六、"

    def test_decimal_format(self):
        r = NumberingRenderer(_NUMBERING_XML)
        assert r.render(3) == "1."
        assert r.render(3) == "2."

    def test_nested_level_not_rendered(self):
        """多级列表（ilvl>0）暂不渲染，返回空（已知局限）。"""
        r = NumberingRenderer(_NUMBERING_XML)
        assert r.render(1, ilvl=1) == ""

    def test_unknown_num_id(self):
        r = NumberingRenderer(_NUMBERING_XML)
        assert r.render(999) == ""

    def test_no_numbering_xml(self):
        r = NumberingRenderer(None)
        assert r.render(1) == ""

    def test_malformed_xml(self):
        r = NumberingRenderer(b"<w:numbering>broken")
        assert r.render(1) == ""

    def test_chinese_number_range(self):
        from gateway.platforms.review.docmodel import _cn_number

        assert _cn_number(1) == "一"
        assert _cn_number(11) == "十一"
        assert _cn_number(20) == "二十"
        assert _cn_number(21) == "二十一"


# ─── 区域/角色（作业宝文档结构，纯文本模拟）───

def _pars(*texts: str) -> list:
    return [Paragraph(index=i + 1, text=t) for i, t in enumerate(texts) if t]


_JOBBAO_DOC = [
    "济南能源集团有限公司",              # 1 标题-机关
    "作业活动梳理及“作业宝”APP建设",      # 2 标题-事由
    "专题培训",                          # 3 标题-续行
    "（代通知）",                        # 4 标题-文种
    "一、培训时间和地点",                 # 5 正文-章节
    "时间：2026年6月17日至18日（共2天）",  # 6 正文
    "二、培训对象",                      # 7 正文-章节
    "集团公司及权属企业M8及以上职级管理人员",  # 8 正文
    "附件：",                            # 9 附件说明头
    "1.日程安排表",                      # 10 附件说明项
    "2.参训人员报名表",                  # 11
    "3.培训请假单",                      # 12
    "济南能源集团有限公司",              # 13 落款署名
    "2026年6月15日",                    # 14 成文日期
    "附件1",                            # 15 附件页标记
    "日程安排表",                        # 16 附件页标题
    "附件2",                            # 17
    "参训人员报名表",                    # 18
]


class TestRegions:
    def test_title_block_and_marker(self):
        doc = build_document(_pars(*_JOBBAO_DOC))
        assert doc.first_marker == 5
        assert doc.title_block == {1, 2, 3, 4}

    def test_signature_and_attachment_pages(self):
        doc = build_document(_pars(*_JOBBAO_DOC))
        assert doc.sign_line_index == 13
        assert doc.sign_date_index == 14
        assert doc.attachment_page_start == 15

    def test_roles_and_regions(self):
        paras = _pars(*_JOBBAO_DOC)
        build_document(paras)
        by_index = {p.index: p for p in paras}

        # 标题块
        assert by_index[1].role == "title_org"
        assert by_index[1].region == REGION_TITLE_BLOCK
        assert by_index[2].role == "title_line"
        assert by_index[3].role == "title_line"
        assert by_index[4].role == "title_genre"  # （代通知）是文种标记
        # 正文
        assert by_index[5].role == "chapter_head"
        assert by_index[5].region == REGION_BODY
        assert by_index[8].role == "body"
        # 附件说明（落款前）
        assert by_index[9].role == "attachment_list_head"
        assert by_index[9].region == REGION_ATTACHMENT_LIST
        assert by_index[10].role == "attachment_list_item"
        # 落款
        assert by_index[13].role == "signature"
        assert by_index[13].region == REGION_SIGNATURE
        assert by_index[14].role == "sign_date"
        # 附件页（落款后）
        assert by_index[15].role == "attachment_marker"
        assert by_index[15].region == REGION_ATTACHMENT_PAGES
        assert by_index[16].role == "attachment_title"

    def test_sender_role(self):
        paras = _pars("关于开展安全生产培训的通知", "各科室：", "一、检查目的")
        build_document(paras)
        assert paras[1].role == "sender"
        assert paras[1].region == REGION_BODY

    def test_attachment_colon_not_sender(self):
        """「附件：」是附件说明标记，不是主送机关。"""
        paras = _pars("一、培训时间和地点", "附件：", "1.日程安排表")
        build_document(paras)
        assert paras[1].role == "attachment_list_head"
        assert not paras[1].role == "sender"


# ─── 端到端：含自动编号的 docx ───


def _make_numbered_docx() -> bytes:
    """构造最小 docx：3 个章节标题，其中后 2 个用 Word 自动编号。"""
    from docx import Document as DocxDocument
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = DocxDocument()
    doc.add_paragraph("济南能源集团有限公司")
    doc.add_paragraph("一、培训时间和地点")
    doc.add_paragraph("二、培训对象")
    doc.add_paragraph("有关事项")  # 将渲染为「三、」

    numbering_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '  <w:abstractNum w:abstractNumId="0">'
        '    <w:lvl w:ilvl="0">'
        '      <w:start w:val="3"/><w:numFmt w:val="chineseCounting"/><w:lvlText w:val="%1、"/>'
        "    </w:lvl>"
        "  </w:abstractNum>"
        '  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        "</w:numbering>"
    )
    # 复用或创建 numbering part（python-docx 默认已建空 part；XmlPart 保存时
    # 从 _element 序列化，须替换元素树而非 _blob）
    from docx.oxml import parse_xml

    existing = [
        p for p in doc.part.package.iter_parts()
        if str(p.partname) == "/word/numbering.xml"
    ]
    if existing:
        existing[0]._element = parse_xml(numbering_xml.encode("utf-8"))
    else:
        part = Part(
            PackURI("/word/numbering.xml"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
            numbering_xml.encode("utf-8"),
            doc.part.package,
        )
        doc.part.relate_to(
            part,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering",
        )

    p_pr = doc.paragraphs[3]._p.get_or_add_pPr()
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    num_id = OxmlElement("w:numId")
    num_id.set(qn("w:val"), "1")
    num_pr.append(ilvl)
    num_pr.append(num_id)
    p_pr.append(num_pr)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


class TestParseDocxNumbering:
    def test_auto_numbering_rendered_into_text(self):
        """Word 自动编号必须渲染进文本：'有关事项' → '三、有关事项'。"""
        paras = parse_document(_make_numbered_docx(), "test.docx")
        texts = {p.index: p.text for p in paras}
        assert texts[4] == "三、有关事项"
        assert texts[4].startswith("三、")

    def test_structure_filled(self):
        paras = parse_document(_make_numbered_docx(), "test.docx")
        by_index = {p.index: p for p in paras}
        assert by_index[4].region == REGION_BODY
        assert by_index[4].role == "chapter_head"
