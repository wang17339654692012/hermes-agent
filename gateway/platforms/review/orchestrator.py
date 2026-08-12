"""审核流程编排器 — 代码只做管道，技能提供标准和策略。"""

import asyncio
import json
import logging
import os
import re
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import aiohttp

from .skill_loader import load_review_skill, ReviewSkill
from .models import Annotation, Paragraph
from .parser import parse_document
from .retriever import multi_round_search
from .comparator import compare_paragraphs
from .annotator import generate_annotated_docx

logger = logging.getLogger(__name__)


class ReviewOrchestrator:
    """审核流程编排器。

    代码层只做管道：解析 → 分析 → 检索 → 审核 → 批注 → 上传。
    审核标准、检索策略全部由 document-review 技能在运行时提供。
    """

    def __init__(
        self,
        file_bytes: bytes,
        filename: str,
        doc_type: Optional[str] = None,
        severity_filter: str = "all",
        annotate: bool = True,
    ):
        self.file_bytes = file_bytes
        self.filename = filename
        self.doc_type = doc_type
        self.severity_filter = severity_filter
        self.annotate = annotate

    async def run(self) -> dict:
        t0 = time.time()

        # ── 加载技能（运行时，非编译时）──
        skill = load_review_skill()
        if not skill:
            return {"error": "document-review 技能未找到，请确认技能已安装"}

        logger.info("开始审核: %s, annotate=%s", self.filename, self.annotate)

        # ── Step 1: 解析文档 ──
        try:
            paragraphs = parse_document(self.file_bytes, self.filename)
        except Exception as e:
            logger.error("文档解析失败: %s", e)
            return {"error": f"文档解析失败: {e}"}

        if not paragraphs:
            return {"error": "文档为空或无法解析"}

        logger.info("Step 1/6: 解析完成, %d 个段落", len(paragraphs))

        # ── Step 2: LLM 内容分析 ──
        try:
            analysis = await self._analyze_content(paragraphs, skill)
        except Exception as e:
            logger.error("内容分析失败: %s", e)
            return {"error": f"内容分析失败: {e}"}

        logger.info("Step 2/6: 分析完成, themes=%d, expressions=%d, refs=%d",
                     len(analysis.get("themes", [])),
                     len(analysis.get("expressions", [])),
                     len(analysis.get("references", [])))

        # ── Step 3: 多轮检索（策略来自技能）──
        try:
            refs = await multi_round_search(
                analysis=analysis,
                platforms=skill.search_platforms,
                domains=skill.search_domains,
                rounds=skill.search_rounds,
            )
        except Exception as e:
            logger.error("检索失败: %s", e)
            refs = []  # 检索失败不阻断流程，继续审核

        logger.info("Step 3/6: 检索完成, %d 条参考材料", len(refs))

        # ── Step 4: 逐段对比审核（标准来自技能）──
        try:
            annotations = await compare_paragraphs(
                paragraphs=paragraphs,
                refs=refs,
                skill=skill,
            )
        except Exception as e:
            logger.error("审核失败: %s", e)
            return {"error": f"审核过程出错: {e}"}

        logger.info("Step 4/6: 审核完成, %d 条批注", len(annotations))

        # ── Step 5: 过滤 ──
        annotations = self._filter(annotations)
        logger.info("Step 5/6: 过滤后 %d 条批注 (filter=%s)", len(annotations), self.severity_filter)

        # ── 构建基础响应 ──
        duration = round(time.time() - t0, 1)
        result = {
            "summary": {
                "total_paragraphs": len(paragraphs),
                "critical": sum(1 for a in annotations if a.severity == "critical"),
                "important": sum(1 for a in annotations if a.severity == "important"),
                "suggestion": sum(1 for a in annotations if a.severity == "suggestion"),
                "duration_seconds": duration,
            },
            "issues": [
                {
                    "paragraph": a.paragraph_index,
                    "severity": a.severity,
                    "type": a.issue_type,
                    "description": a.description,
                    "suggestion": a.suggestion,
                    "reference": a.reference,
                }
                for a in annotations
            ],
        }

        # ── Step 6-7: 生成批注 + 上传（仅 annotate=true 时）──
        if self.annotate:
            try:
                annotated_bytes = await generate_annotated_docx(
                    self.file_bytes, self.filename, paragraphs, annotations
                )
                download_url = await self._upload_to_minio(
                    annotated_bytes, self.filename
                )
                result["download_url"] = download_url
                result["summary"]["duration_seconds"] = round(time.time() - t0, 1)
                logger.info("Step 6-7/6: 批注文档已生成并上传")
            except Exception as e:
                logger.error("批注文档生成/上传失败: %s", e)
                result["warning"] = f"批注文档生成失败: {e}，仅返回文字审核意见"
        else:
            logger.info("Step 6-7/6: 跳过 (annotate=false)")

        logger.info("审核完成: %s, 耗时 %.1fs", self.filename, result["summary"]["duration_seconds"])
        return result

    async def _analyze_content(self, paragraphs: List[Paragraph], skill: ReviewSkill) -> dict:
        """调用 LLM 分析文档，识别主题、政治表述、引用文件。"""
        prompt = f"""你是一名公文审核专家。请分析以下文档，识别：

1. 文档主题（3-5 个关键词）
2. 关键政治表述（需要与权威来源核对的固定提法）
3. 引用的文件、会议、讲话（需要验证准确性和时效性）

## 审核标准（来自 document-review 技能）

{skill.review_standard}

## 待分析文档

{self._format_paragraphs(paragraphs)}

请以 JSON 格式输出：
{{
  "themes": ["主题1", "主题2"],
  "expressions": [
    {{"text": "关键表述原文", "type": "政治术语/固定提法/领导人讲话引用"}}
  ],
  "references": [
    {{"text": "引用文件/会议/讲话名称", "context": "出现该引用的上下文"}}
  ]
}}"""
        response = await _call_llm(prompt)
        return _parse_json_response(response)

    def _filter(self, annotations: List[Annotation]) -> List[Annotation]:
        if self.severity_filter == "critical":
            return [a for a in annotations if a.severity == "critical"]
        if self.severity_filter == "critical+important":
            return [a for a in annotations if a.severity in ("critical", "important")]
        return annotations

    def _format_paragraphs(self, paragraphs: List[Paragraph]) -> str:
        return "\n\n".join(f"[P{p.index}] {p.text}" for p in paragraphs)

    async def _upload_to_minio(self, file_bytes: bytes, filename: str) -> str:
        """上传到 MinIO，返回 presigned URL"""
        stem = Path(filename).stem
        output_name = f"{stem}_reviewed.docx"

        tmp_path = Path(tempfile.gettempdir()) / output_name
        tmp_path.write_bytes(file_bytes)

        try:
            url = await asyncio.to_thread(
                _upload_file_bytes, str(tmp_path), output_name
            )
            return url
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


async def _call_llm(prompt: str) -> str:
    """调用 LLM Provider API"""
    api_url = os.getenv(
        "LLM_API_URL",
        "https://api.deepseek.com/v1/chat/completions",
    )
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("LLM_MODEL", "deepseek-chat")

    if not api_key:
        raise RuntimeError("LLM_API_KEY 或 DEEPSEEK_API_KEY 未设置")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
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


def _parse_json_response(response: str) -> dict:
    """从 LLM 响应中提取 JSON"""
    match = re.search(r'\{[\s\S]*\}', response)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _upload_file_bytes(file_path: str, object_name: str) -> str:
    """上传文件到 MinIO，返回 presigned URL"""
    from tools.minio_upload import upload_file

    # 临时修改路径以匹配 upload_file 的期望
    url = upload_file(file_path)
    if url:
        return url
    raise RuntimeError("MinIO 上传失败")