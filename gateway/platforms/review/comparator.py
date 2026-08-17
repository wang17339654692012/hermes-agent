"""逐段对比审核 — prompt 全部由技能提供，代码只做拼接。"""

import asyncio
import json
import logging
import os
import re
from typing import List, Optional

import aiohttp

from .skill_loader import ReviewSkill
from .models import Annotation, Paragraph

logger = logging.getLogger(__name__)


async def compare_paragraphs(
    paragraphs: List[Paragraph],
    refs: List[dict],
    skill: ReviewSkill,
    batch_size: int = 8,
    doc_type: Optional[str] = None,
) -> List[Annotation]:
    """分批对比审核。

    技能注入点：
    - skill.review_standard  → 审核标准 + 防编造约束（作为 system prompt）
    - doc_type               → 文种声明，模型按对应文种的格式标准审核
    - refs                   → 多轮检索结果（作为参考材料）
    - paragraphs             → 待审核段落
    """
    all_annotations: List[Annotation] = []

    # 构建参考材料
    reference_text = _build_reference_text(refs, skill.extract_chars)

    # 构建系统级审核 prompt（标准来自技能，文种决定格式标准）
    review_system_prompt = _build_review_system_prompt(skill, doc_type)

    # 分批处理
    total_batches = (len(paragraphs) + batch_size - 1) // batch_size
    for i in range(0, len(paragraphs), batch_size):
        batch = paragraphs[i : i + batch_size]
        batch_num = i // batch_size + 1

        paragraphs_text = "\n\n".join(
            f"[段落 {p.index}] {p.text}" for p in batch
        )

        user_prompt = _build_user_prompt(reference_text, paragraphs_text)

        try:
            response = await _call_llm_with_system(
                system_prompt=review_system_prompt,
                user_prompt=user_prompt,
            )
            batch_annotations = _parse_annotations(response)
            all_annotations.extend(batch_annotations)
            logger.info("批次 %d/%d 完成, 发现 %d 条问题", batch_num, total_batches, len(batch_annotations))
        except Exception as e:
            logger.error("批次 %d/%d 审核失败: %s", batch_num, total_batches, e)

        # 避免 API 限流
        if i + batch_size < len(paragraphs):
            await asyncio.sleep(1)

    return all_annotations


def _build_review_system_prompt(skill: ReviewSkill, doc_type: Optional[str]) -> str:
    """构建逐段审核的 system prompt（标准来自技能，文种决定格式标准）。"""
    if doc_type:
        doc_type_decl = f"本文种为：{doc_type}。请按该文种的格式规范审核。"
    else:
        doc_type_decl = "文种未识别，请勿假设文种，按通用公文规范审核。"

    return f"""你是一名公文审核专家。请严格按照以下审核标准，逐段对比公文与权威参考材料。

## 文种

{doc_type_decl}

## 审核范围

1. 政策符合性审核：将公文与参考材料对比，检查政策引用是否过期、内容是否与官方发布存在冲突。
2. 语言与格式审核：检查错别字、语句表达、用词规范性、文章结构合理性及公文格式合规性。

## 审核标准（来自 document-review 技能）

{skill.review_standard}

## 输出格式

对每个有问题的段落，输出一条 JSON。仅输出有问题的段落，无问题不输出。

[
  {{
    "paragraph_index": 1,
    "severity": "critical|important|suggestion",
    "issue_type": "问题类型",
    "description": "问题说明",
    "suggestion": "修改建议",
    "reference": "参考依据（URL + 日期）",
    "original_text": "原文摘录（前50字）"
  }}
]"""


def _build_user_prompt(reference_text: str, paragraphs_text: str) -> str:
    """构建逐段审核的 user prompt。

    检索无结果时显式注入防编造指令——不依赖模型自觉，
    避免模型凭空编造政策依据。
    """
    if reference_text:
        refs_section = reference_text
    else:
        refs_section = (
            "（未检索到权威参考材料。本批次仅执行语言与格式审核；"
            "政策符合性结论需人工核实，禁止引用任何未提供来源的政策内容。）"
        )

    return f"""## 权威参考材料

{refs_section}

## 待审核段落

{paragraphs_text}"""


def _build_reference_text(refs: List[dict], char_limit: int) -> str:
    """构建参考材料文本"""
    parts = []
    for i, r in enumerate(refs, 1):
        text = r.get("content", r.get("text", ""))[:char_limit]
        parts.append(
            f"【参考 {i}】来源：{r.get('url', '')}\n"
            f"日期：{r.get('date', '未知')}\n"
            f"{text}"
        )
    return "\n\n---\n\n".join(parts)


async def _call_llm_with_system(system_prompt: str, user_prompt: str) -> str:
    """调用 LLM，带 system prompt"""
    from .settings import resolve_llm_settings

    api_url, model = resolve_llm_settings()
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")

    if not api_key:
        raise RuntimeError("LLM_API_KEY 或 DEEPSEEK_API_KEY 未设置")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 4096,
            },
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"LLM API 返回 {resp.status}: {body[:200]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


def _parse_annotations(response: str) -> List[Annotation]:
    """从 LLM 响应中解析 Annotation 列表"""
    # 提取 JSON 数组
    match = re.search(r'\[[\s\S]*\]', response)
    if not match:
        return []

    try:
        items = json.loads(match.group())
    except json.JSONDecodeError:
        return []

    annotations = []
    for item in items:
        annotations.append(Annotation(
            paragraph_index=item.get("paragraph_index", 0),
            severity=item.get("severity", "suggestion"),
            issue_type=item.get("issue_type", ""),
            description=item.get("description", ""),
            suggestion=item.get("suggestion", ""),
            reference=item.get("reference", ""),
            original_text=item.get("original_text", ""),
        ))
    return annotations