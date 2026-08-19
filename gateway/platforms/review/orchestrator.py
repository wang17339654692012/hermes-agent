"""审核流程编排器 — 代码只做管道，技能提供标准和策略。"""

import asyncio
import datetime
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

from . import format_checker
from .docmodel import REGION_ATTACHMENT_LIST, REGION_ATTACHMENT_PAGES, REGION_SIGNATURE, REGION_TITLE_BLOCK
from .skill_loader import load_review_skill, ReviewSkill
from .models import Annotation, Paragraph
from .parser import parse_document
from .retriever import multi_round_search
from .comparator import compare_paragraphs
from .annotator import generate_annotated_docx

logger = logging.getLogger(__name__)

# ── 区域策略：LLM 内容批注的确定性过滤 ──
# 用户决策：无权威依据的主观措辞建议直接删除；"通知缘由缺失"类结构意见不报；
# 标题块/落款/附件页只接受明确的文本错误类意见；日期类意见一律撤销
# （日期一致性由 format_checker 确定性检查，LLM 报告即冗余）。

# 明确的文本错误类型（错别字/漏字/…）——区域唯一放行类别（只看 issue_type，
# 不信任描述里的"应为/应写"，那是建议措辞而非错误标记）
_CN_TYPO_TYPE = re.compile(r"(错别字|漏字|错字|多字|缺字|笔误)")
# 结构意见（缺通知缘由/层次/衔接/过渡）——不报
_STRUCTURAL_OPINION = re.compile(r"(结构|层次|缘由|衔接|过渡)")
# 日期类意见（LLM 报告日期/成文日期/培训时间问题均为冗余或幻觉）——撤销
_DATE_CLAIM = re.compile(r"(日期|成文日期|培训时间|当前日期)")
# 无权威检索依据
_UNVERIFIED = re.compile(r"(未在权威来源中检索到|未检索到|无权威依据)")
# 事实类（有权威依据或客观事实，不受"未检索到"删除规则影响）
_FACTUAL = re.compile(r"(错别字|漏字|错字|多字|笔误|政治|引用|数据)")
# 推断式结论（无客观依据的推测，如"根据公文内容推断"）
_INFERENCE = re.compile(r"(推断|推测|猜测)")

# 区域由系统确定性审核，LLM 仅可报文本错误
_NON_REVIEW_REGIONS = {REGION_TITLE_BLOCK, REGION_SIGNATURE, REGION_ATTACHMENT_PAGES}


def _apply_region_policy(
    annotations: List[Annotation],
    paragraphs: List[Paragraph],
) -> List[Annotation]:
    """对 LLM 内容批注执行区域策略（只作用于 content_annotations）。"""
    by_index = {p.index: p for p in paragraphs}
    nonempty_indices = sorted(p.index for p in paragraphs if p.text.strip())
    kept: List[Annotation] = []
    for a in annotations:
        p = by_index.get(a.paragraph_index)
        region = p.region if p is not None else ""

        # 1. 标题块 / 落款 / 附件页：格式要素由系统确定性检查，仅放行文本错误类
        if region in _NON_REVIEW_REGIONS and not _CN_TYPO_TYPE.search(a.issue_type):
            continue
        # 2. 结构意见（缺通知缘由等）不报
        if _STRUCTURAL_OPINION.search(a.issue_type):
            continue
        # 3. 日期类意见撤销（格式检查已确定性处理）
        if _DATE_CLAIM.search(a.issue_type + a.description):
            continue
        # 4. 无权威依据的主观措辞意见直接删除（非事实类）
        if _UNVERIFIED.search(a.reference) and not _FACTUAL.search(a.issue_type):
            continue
        # 5. 推断式结论（"根据…推断/推测"）——无客观依据，删除
        if _INFERENCE.search(a.description):
            continue
        # 6. "句末分号应改句号"断言：段后仍有内容时分号是正确的非末项分隔，
        #    断言与文档结构不符 → 删除（不追 LLM 措辞变体，如"最后一项/末项/末条"）
        if (
            "分号" in a.description
            and "句号" in a.description
            and any(i > a.paragraph_index for i in nonempty_indices)
        ):
            continue
        kept.append(a)
    return kept


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

        # ── Step 2: LLM 内容分析（含文种识别）──
        try:
            analysis = await self._analyze_content(
                paragraphs, skill, known_doc_type=self.doc_type
            )
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

        # ── Step 4a: 格式要素确定性审核（基于 docmodel 结构模型，不依赖 LLM）──
        try:
            format_annotations = format_checker.check(
                paragraphs,
                doc_type=analysis.get("doc_type"),
                today=datetime.date.today(),
            )
        except Exception as e:
            logger.error("格式审核失败: %s", e)
            format_annotations = []
        logger.info("Step 4a/6: 格式审核完成, %d 条批注", len(format_annotations))

        # ── Step 4b: LLM 内容审核（标准来自技能，文种来自分析结果）──
        try:
            content_annotations = await compare_paragraphs(
                paragraphs=paragraphs,
                refs=refs,
                skill=skill,
                doc_type=analysis.get("doc_type"),
            )
        except Exception as e:
            logger.error("审核失败: %s", e)
            return {"error": f"审核过程出错: {e}"}

        # ── 区域策略：LLM 内容批注过滤（无依据主观建议删除/结构意见不报/
        #    标题块/落款/附件页仅放行文本错误/日期矛盾幻觉撤销）──
        content_annotations = _apply_region_policy(content_annotations, paragraphs)

        annotations = format_annotations + content_annotations
        logger.info(
            "Step 4/6: 审核完成, %d 条批注 (格式 %d + 内容 %d)",
            len(annotations), len(format_annotations), len(content_annotations),
        )

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
            if Path(self.filename).suffix.lower() == ".pdf":
                # Word 批注只能落在 docx 上；PDF 显式降级，不靠异常兜底
                result["warning"] = "PDF 暂不支持生成批注文档，仅返回文字审核意见"
                logger.info("Step 6-7/6: PDF 跳过批注生成")
            else:
                try:
                    annotated_bytes = await generate_annotated_docx(
                        self.file_bytes, self.filename, annotations
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

    async def _analyze_content(
        self,
        paragraphs: List[Paragraph],
        skill: ReviewSkill,
        known_doc_type: Optional[str] = None,
    ) -> dict:
        """调用 LLM 分析文档，识别主题、政治表述、引用文件、文种。

        known_doc_type：调用方传入的文种；非 None 时跳过自动识别。
        """
        prompt = f"""你是一名公文审核专家。请分析以下文档，识别：

1. 文档主题（3-5 个关键词）
2. 关键政治表述（需要与权威来源核对的固定提法）
3. 引用的文件、会议、讲话（需要验证准确性和时效性）
4. 公文文种（《党政机关公文处理工作条例》15 种法定文种：决议、决定、命令（令）、公报、公告、通告、意见、通知、通报、报告、请示、批复、议案、函、纪要；无法判断时输出"未知"）

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
  ],
  "doc_type": "通知"
}}"""
        response = await _call_llm(prompt)
        analysis = _parse_json_response(response)
        # 调用方传入文种时以传入值为准，跳过 LLM 识别结果
        analysis["doc_type"] = known_doc_type or analysis.get("doc_type")
        return analysis

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