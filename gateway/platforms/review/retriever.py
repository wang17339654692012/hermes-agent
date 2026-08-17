"""多轮递进式检索 — 策略由 document-review 技能提供。"""

import asyncio
import logging
import os
from typing import List, Dict

import aiohttp

logger = logging.getLogger(__name__)


async def multi_round_search(
    analysis: dict,
    platforms: List[Dict],
    domains: List[str],
    rounds: int = 3,
) -> List[dict]:
    """技能驱动的多轮递进式检索。

    策略完全由技能文件（party-building-search.md）定义，代码只执行。

    Args:
        analysis: LLM 分析的文档内容 {themes, expressions, references}
        platforms: 检索平台列表 [{"name": "...", "domain": "..."}]
        domains: site: 限定域名列表
        rounds: 检索轮次数

    Returns:
        去重排序后的检索结果列表（含全文）
    """
    all_results: List[dict] = []

    # ── 第 1 轮：宽泛检索 ──
    for theme in analysis.get("themes", [])[:3]:
        for p in platforms:
            query = f"site:{p['domain']} {theme}"
            results = await _tavily_search(query, domains)
            all_results.extend(results)

    # ── 第 2 轮：精准检索 ──
    if rounds >= 2:
        for ref in analysis.get("references", [])[:5]:
            text = ref.get("text", "")
            if not text:
                continue
            for p in platforms:
                query = f"site:{p['domain']} {text}"
                results = await _tavily_search(query, domains)
                all_results.extend(results)

    # ── 第 3 轮：深挖疑点 ──
    if rounds >= 3:
        for expr in analysis.get("expressions", [])[:5]:
            text = expr.get("text", "")
            if not text:
                continue
            for p in platforms:
                query = f"site:{p['domain']} {text} 最新"
                results = await _tavily_search(query, domains)
                all_results.extend(results)

    # 去重 + 排序
    unique = _deduplicate(all_results)
    unique.sort(key=lambda r: r.get("date", ""), reverse=True)

    # 取 Top 10
    top10 = unique[:10]

    logger.info("检索完成: %d 条原始结果, %d 条去重, %d 条最终", len(all_results), len(unique), len(top10))

    return await _extract_full_text(top10)


async def _tavily_search(query: str, domains: List[str]) -> List[dict]:
    """调用 Tavily Search API"""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        logger.warning("TAVILY_API_KEY 未设置，跳过检索")
        return []

    url = "https://api.tavily.com/search"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "api_key": api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": 5,
                    "include_domains": domains,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Tavily 搜索返回 %d: %s", resp.status, query)
                    return []
                data = await resp.json()
                return data.get("results", [])
    except Exception as e:
        logger.warning("Tavily 搜索异常: %s", e)
        return []


def _deduplicate(results: List[dict]) -> List[dict]:
    """按 URL 去重"""
    seen: set = set()
    unique: List[dict] = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
    return unique


async def _extract_full_text(results: List[dict]) -> List[dict]:
    """对每条结果调用 Tavily extract API 提取全文，写入 content 字段。

    - 无 TAVILY_API_KEY 时跳过，保留 Tavily 摘要
    - 提取失败或未返回全文时保留原摘要（不阻断审核流程）
    - 全文截断到 5000 字符（技能规定提取上限）
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key or not results:
        return results

    urls = [r.get("url", "") for r in results if r.get("url")]
    if not urls:
        return results

    extracted: Dict[str, str] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.tavily.com/extract",
                json={"api_key": api_key, "urls": urls},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Tavily extract 返回 %d，保留摘要", resp.status)
                    return results
                data = await resp.json()
                for item in data.get("results", []):
                    raw = item.get("raw_content", "")
                    if raw:
                        extracted[item.get("url", "")] = raw
    except Exception as e:
        logger.warning("Tavily extract 异常: %s，保留摘要", e)
        return results

    if not extracted:
        return results

    enriched = []
    for r in results:
        full = extracted.get(r.get("url", ""), "")
        if full:
            r = dict(r)
            r["content"] = full[:5000]
        enriched.append(r)
    return enriched