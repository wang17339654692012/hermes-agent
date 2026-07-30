"""Tests for retriever — Tavily 多轮检索。"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from gateway.platforms.review.retriever import (
    multi_round_search,
    _tavily_search,
    _deduplicate,
)


class TestDeduplicate:
    """_deduplicate tests."""

    def test_empty_list(self):
        assert _deduplicate([]) == []

    def test_no_duplicates(self):
        results = [
            {"url": "http://a.com", "title": "A"},
            {"url": "http://b.com", "title": "B"},
        ]
        assert len(_deduplicate(results)) == 2

    def test_duplicates_removed(self):
        results = [
            {"url": "http://a.com", "title": "A"},
            {"url": "http://a.com", "title": "A dup"},
            {"url": "http://b.com", "title": "B"},
        ]
        unique = _deduplicate(results)
        assert len(unique) == 2
        assert unique[0]["title"] == "A"

    def test_no_url_skipped(self):
        results = [
            {"title": "No URL"},
            {"url": "http://a.com", "title": "A"},
        ]
        unique = _deduplicate(results)
        assert len(unique) == 1  # no-url entry skipped


class TestTavilySearch:
    """_tavily_search tests."""

    @pytest.mark.asyncio
    async def test_successful_search(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "results": [{"url": "http://a.com", "title": "A"}]
        })

        # aiohttp.ClientSession is used as `async with ClientSession() as session:`
        # So we need __aenter__ and __aexit__
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        # session.post() is used as `async with session.post(...) as resp:`
        mock_post_ctx = AsyncMock()
        mock_post_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.post = MagicMock(return_value=mock_post_ctx)

        with patch("gateway.platforms.review.retriever.aiohttp.ClientSession", return_value=mock_session):
            with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
                results = await _tavily_search("test query", ["12371.cn"])
                assert len(results) == 1
                assert results[0]["url"] == "http://a.com"

    @patch("gateway.platforms.review.retriever.aiohttp.ClientSession")
    def test_search_no_api_key(self, mock_session):
        with patch.dict("os.environ", {}, clear=True):
            import asyncio
            results = asyncio.get_event_loop().run_until_complete(
                _tavily_search("test", ["12371.cn"])
            )
            assert results == []

    @patch("gateway.platforms.review.retriever.aiohttp.ClientSession")
    def test_search_error_status(self, mock_session):
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_session.return_value.__aenter__.return_value.post.return_value.__aenter__.return_value = mock_resp

        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            import asyncio
            results = asyncio.get_event_loop().run_until_complete(
                _tavily_search("test", ["12371.cn"])
            )
            assert results == []

    @patch("gateway.platforms.review.retriever.aiohttp.ClientSession")
    def test_search_exception(self, mock_session):
        mock_session.return_value.__aenter__.return_value.post.side_effect = Exception("Network error")

        with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            import asyncio
            results = asyncio.get_event_loop().run_until_complete(
                _tavily_search("test", ["12371.cn"])
            )
            assert results == []


class TestMultiRoundSearch:
    """multi_round_search tests."""

    @patch("gateway.platforms.review.retriever._tavily_search")
    @pytest.mark.asyncio
    async def test_round1_only(self, mock_search):
        mock_search.return_value = [{"url": "http://a.com", "title": "A"}]

        analysis = {"themes": ["党建考核"], "expressions": [], "references": []}
        platforms = [{"name": "党建网", "domain": "dangjian.cn"}]
        domains = ["dangjian.cn"]

        results = await multi_round_search(
            analysis=analysis,
            platforms=platforms,
            domains=domains,
            rounds=1,
        )
        assert len(results) >= 1

    @patch("gateway.platforms.review.retriever._tavily_search")
    @pytest.mark.asyncio
    async def test_all_rounds(self, mock_search):
        mock_search.return_value = [{"url": "http://a.com", "title": "A"}]

        analysis = {
            "themes": ["党建"],
            "expressions": [{"text": "全面从严治党"}],
            "references": [{"text": "全国组织工作会议"}],
        }
        platforms = [{"name": "党建网", "domain": "dangjian.cn"}]

        results = await multi_round_search(
            analysis=analysis,
            platforms=platforms,
            domains=["dangjian.cn"],
            rounds=3,
        )
        # 3 rounds: 1 theme = 1 call, 1 ref = 1 call, 1 expr = 1 call = 3 calls
        assert mock_search.call_count >= 3

    @patch("gateway.platforms.review.retriever._tavily_search")
    @pytest.mark.asyncio
    async def test_empty_analysis(self, mock_search):
        mock_search.return_value = []

        analysis = {"themes": [], "expressions": [], "references": []}
        platforms = [{"name": "党建网", "domain": "dangjian.cn"}]

        results = await multi_round_search(
            analysis=analysis,
            platforms=platforms,
            domains=["dangjian.cn"],
            rounds=3,
        )
        assert results == []

    @patch("gateway.platforms.review.retriever._tavily_search")
    @pytest.mark.asyncio
    async def test_duplicate_urls_merged(self, mock_search):
        mock_search.return_value = [
            {"url": "http://a.com", "title": "A"},
            {"url": "http://a.com", "title": "A dup"},
            {"url": "http://b.com", "title": "B"},
        ]

        analysis = {"themes": ["党建"], "expressions": [], "references": []}
        platforms = [{"name": "党建网", "domain": "dangjian.cn"}]

        results = await multi_round_search(
            analysis=analysis,
            platforms=platforms,
            domains=["dangjian.cn"],
            rounds=1,
        )
        # Should be deduplicated
        assert len(results) == 2