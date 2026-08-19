"""格式要素确定性审核 — 基于 docmodel 结构模型，不依赖 LLM。

格式要素「是否缺失」是结构事实判断。docmodel 已在解析期完成结构定位
（标题块/正文/落款/附件说明/附件页）与自动编号渲染，本模块只检查模型
字段，不再对平铺文本做正则反推。

各检查项：
- 标题三要素：TITLE_BLOCK 内机关行 + 文种行（含「（代通知）」）
- 主送机关：正文区 role=sender 的行
- 落款：署名段 + 成文日期段（格式/未来日期）
- 章节序号：无编号的小标题（自动编号已渲染进文本，不再误报）
- 附件说明：仅检查 ATTACHMENT_LIST 区（落款前）；附件页不参与
- 日期内部一致性：正文截止日期早于成文日期
"""

import re
from datetime import date as DateType
from typing import List, Optional, Tuple

from .docmodel import (
    REGION_ATTACHMENT_LIST,
    REGION_ATTACHMENT_PAGES,
    REGION_SIGNATURE,
    REGION_TITLE_BLOCK,
    _CHAPTER_HEAD_PATTERN,
    _cn_number,
    _is_sign_date_line,
    build_document,
)
from .models import Annotation, Paragraph

# 参考依据（所有格式批注共用）
_GB_REFERENCE = "GB/T 9704—2012《党政机关公文格式》"

# 文种词（标题第三要素，行尾出现；「（代通知）」是合法文种标记）。
# 覆盖《党政机关公文处理工作条例》全部法定文种（15 种）。
_GENRE_PATTERN = re.compile(
    r"(决议|决定|命令|令|公报|公告|通告|意见|通知|通报|报告|请示|批复|议案|函|纪要|（代通知）)$"
)

# 无序号小标题的特征词（「培训费用」「工作分工」等结构词）
_HEADING_WORD_PATTERN = re.compile(
    r"(关于|开展|专项|专题|培训|活动|会议|报告|通知|方案|工作"
    r"|内容|目标|费用|分工|事项|对象|要求|安排|方式|时间|地点)"
)

# 完整日期（用于解析成文日期）
_FULL_DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
# 严格合规的成文日期：月日不编虚位
_STRICT_DATE_PATTERN = re.compile(r"\d{4}年(?:[1-9]|1[0-2])月(?:[1-9]|[12]\d|3[01])日")
# 正文中不带年份的月日
_MONTH_DAY_PATTERN = re.compile(r"(\d{1,2})月(\d{1,2})日")

# 主送机关按文种要求：公布性文种（公告/通告/公报）、命令（令）、决议与
# 纪要（出席/列席体系）本无主送机关，不报缺失；其余文种（通知/报告/请示/
# 批复/函/通报/意见/决定/议案等）与未知文种一律要求（保守：宁多报不漏报）。
_SENDER_OPTIONAL_GENRES = {"公告", "通告", "纪要", "公报", "决议", "命令", "令"}


def _ann(
    index: int,
    severity: str,
    issue_type: str,
    description: str,
    suggestion: Optional[str] = None,
) -> Annotation:
    return Annotation(
        paragraph_index=index,
        severity=severity,
        issue_type=issue_type,
        description=description,
        suggestion=suggestion or description,
        reference=_GB_REFERENCE,
    )


def check(
    paragraphs: List[Paragraph],
    doc_type: Optional[str] = None,
    today: Optional[DateType] = None,
) -> List[Annotation]:
    """对段落列表执行确定性格式审核，返回格式类批注。

    doc_type：文种（通知/报告），用于标题文种词判定；None 时按通用文种词。
    today：当前日期，用于成文日期未来校验；None 时不校验未来日期。
    """
    doc = build_document(paragraphs)
    by_index = {p.index: p.text for p in paragraphs}
    nonempty = sorted(i for i, t in by_index.items() if t.strip())
    if not nonempty:
        return []
    today = today or DateType.today()

    annotations: List[Annotation] = []

    # ── 1. 标题三要素 ──
    _check_title(annotations, doc, doc_type)

    # ── 2. 主送机关（按文种要求）──
    _check_sender(annotations, doc, doc_type)

    # ── 3. 落款（署名 + 成文日期）与成文日期格式 ──
    sign_date = _check_signature(annotations, doc, today)

    # ── 4. 章节序号体系（无编号小标题）──
    _check_chapter_numbering(annotations, doc)

    # ── 5. 附件说明（仅 ATTACHMENT_LIST 区）──
    _check_attachments(annotations, doc)

    # ── 6. 日期内部一致性 ──
    _check_date_consistency(annotations, doc, sign_date)

    return annotations


def _check_title(
    annotations: List[Annotation],
    doc,
    doc_type: Optional[str],
) -> None:
    """标题三要素：机关行 + 事由 + 文种行（可分行排版）。"""
    by_index = {p.index: p for p in doc.paragraphs}
    block = [by_index[i] for i in sorted(doc.title_block) if i in by_index]
    block = [p for p in block if p.text.strip()]
    if not block:
        annotations.append(_ann(
            doc.first_marker or 1, "important", "标题要素缺失",
            "缺少标题：文档直接进入正文，未写明发文机关、事由和文种。",
        ))
        return

    has_org = any(p.role == "title_org" for p in block)
    has_genre = any(
        p.role == "title_genre" or bool(_GENRE_PATTERN.search(p.text.strip()))
        for p in block
    )

    if not has_genre:
        genre_word = f"「{doc_type}」" if doc_type else "文种词（如「通知」「报告」）"
        annotations.append(_ann(
            min(doc.title_block), "important", "标题要素缺失",
            f"标题缺少文种：标题各行末尾均无{genre_word}，不符合「发文机关+事由+文种」的标题规范。",
        ))
    if not has_org:
        annotations.append(_ann(
            min(doc.title_block), "important", "标题要素缺失",
            "标题缺少发文机关：标题区未找到机关名称行。",
        ))


def _check_sender(annotations: List[Annotation], doc, doc_type: Optional[str]) -> None:
    """主送机关：正文区存在 role=sender 的行。

    公告/通告/公报（公布性文种）与纪要本无主送机关，跳过检查；
    其余文种与未知文种一律要求（保守）。
    """
    if doc_type and doc_type in _SENDER_OPTIONAL_GENRES:
        return
    if any(p.role == "sender" for p in doc.paragraphs):
        return
    anchor = doc.first_marker or (min(doc.title_block) if doc.title_block else 1)
    annotations.append(_ann(
        anchor, "important", "主送机关缺失",
        "缺少主送机关：正文之前未找到顶格书写、以全角冒号结尾的主送机关行。",
    ))


def _check_signature(
    annotations: List[Annotation],
    doc,
    today: DateType,
) -> Optional[Tuple[int, Tuple[int, int]]]:
    """落款署名 + 成文日期：要素齐全性与日期格式。

    返回 (成文日期行 index, (月, 日))；未识别出成文日期时返回 None。
    """
    by_index = {p.index: p.text for p in doc.paragraphs}
    sign_line = doc.sign_line_index
    date_index = doc.sign_date_index

    if sign_line is None:
        if date_index is not None:
            annotations.append(_ann(
                date_index, "important", "落款要素缺失",
                "落款缺少发文机关署名：文末有成文日期但未找到发文机关署名。",
            ))
        else:
            nonempty = [i for i, t in by_index.items() if t.strip()]
            anchor = nonempty[-1] if nonempty else (doc.first_marker or 1)
            annotations.append(_ann(
                anchor, "important", "落款要素缺失",
                "缺少落款：文末未找到发文机关署名和成文日期。",
            ))
        return None

    if date_index is None:
        annotations.append(_ann(
            sign_line, "important", "落款要素缺失",
            "落款缺少成文日期：发文机关署名之后未找到成文日期。",
        ))
        return None

    date_text = by_index.get(date_index, "")
    full = _FULL_DATE_PATTERN.search(date_text)

    if not _STRICT_DATE_PATTERN.search(date_text):
        if _FULL_DATE_PATTERN.search(date_text):
            annotations.append(_ann(
                date_index, "important", "成文日期格式错误",
                "成文日期月、日不应编虚位（如「06月」「05日」应写为「6月」「5日」）。",
            ))
        else:
            annotations.append(_ann(
                date_index, "important", "成文日期格式错误",
                "成文日期应用阿拉伯数字标注（如「2026年6月15日」），当前为其他数字样式。",
            ))
        if full is None:
            return None

    # 未来日期校验
    if full and DateType(int(full.group(1)), int(full.group(2)), int(full.group(3))) > today:
        annotations.append(_ann(
            date_index, "critical", "成文日期错误",
            f"成文日期晚于当前日期（{today.isoformat()}），请核实。",
        ))

    return (date_index, (int(full.group(2)), int(full.group(3))))


def _is_unnumbered_heading(text: str) -> bool:
    """无序号小标题：短行 + 标题特征词 + 无冒号 + 无句末标点 + 无编号。"""
    text = text.strip()
    return (
        len(text) <= 20
        and _HEADING_WORD_PATTERN.search(text)
        and "：" not in text
        and not re.search(r"[。；，？！]$", text)
        and not _CHAPTER_HEAD_PATTERN.match(text)
        and not re.match(r"^\d+[、.．]", text)
    )


def _check_chapter_numbering(annotations: List[Annotation], doc) -> None:
    """章节序号体系：正文中无编号小标题 → 报缺少序号（含具体建议）。"""
    body = [
        p for p in doc.paragraphs
        if p.text.strip()
        and p.region not in (
            REGION_TITLE_BLOCK, REGION_ATTACHMENT_LIST, REGION_ATTACHMENT_PAGES,
        )
    ]
    if not body:
        return
    has_numbered = any(
        _CHAPTER_HEAD_PATTERN.match(p.text.strip()) for p in body
    )
    if not has_numbered:
        return

    for p in body:
        text = p.text.strip()
        if not _is_unnumbered_heading(text):
            continue
        # 序号推算：此前标题行（有序号+无序号）数量 + 1
        prior = [
            q for q in body
            if q.index < p.index
            and (
                _CHAPTER_HEAD_PATTERN.match(q.text.strip())
                or _is_unnumbered_heading(q.text.strip())
            )
        ]
        next_seq = len(prior) + 1
        annotations.append(_ann(
            p.index, "important", "章节序号缺失",
            f"该小标题「{text[:10]}」缺少序号：与上下文有序号章节的编号体系不衔接，应使用「一、」「二、」等序号标注。",
            f"建议改为「{_cn_number(next_seq)}、{text[:10]}」，使序号与上下文章节体系衔接。",
        ))


def _check_attachments(annotations: List[Annotation], doc) -> None:
    """附件说明（仅 ATTACHMENT_LIST 区）：说明头应为「附件：」样式。

    附件页（ATTACHMENT_PAGES，落款后）是另面编排的标准排版，不参与检查。
    """
    heads = [p for p in doc.paragraphs if p.role == "attachment_list_head"]
    for p in heads:
        text = p.text.strip()
        if not text.endswith("："):
            annotations.append(_ann(
                p.index, "important", "附件说明格式不规范",
                "附件说明头应为「附件：」样式（全角冒号），如「附件：1.日程安排表」。",
            ))


def _check_date_consistency(
    annotations: List[Annotation],
    doc,
    sign_date: Optional[Tuple[int, Tuple[int, int]]],
) -> None:
    """日期内部一致性：正文截止/培训日期早于成文日期 → 报自相矛盾。"""
    if sign_date is None:
        return
    by_index = {p.index: p.text for p in doc.paragraphs}
    for p in doc.paragraphs:
        if p.region in (REGION_TITLE_BLOCK, REGION_SIGNATURE, REGION_ATTACHMENT_PAGES):
            continue
        text = p.text.strip()
        if _is_sign_date_line(text):
            continue  # 成文日期行本身不是「正文日期」
        for m in _MONTH_DAY_PATTERN.finditer(text):
            md = (int(m.group(1)), int(m.group(2)))
            if (md[0], md[1]) < sign_date[1]:
                annotations.append(_ann(
                    p.index, "critical", "日期自相矛盾",
                    f"正文中的日期 {md[0]}月{md[1]}日 早于成文日期，存在日期自相矛盾，请核实。",
                ))
                break
