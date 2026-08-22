"""Тесты Этапа 2: RobotsCache — правила robots.txt и per-domain кеш."""

from __future__ import annotations

import asyncio

import httpx
import respx

from politecrawl.robots import RobotsCache

UA = "politecrawl/0.0"

# --- rules -----------------------------------------------------------------


@respx.mock
async def test_disallow_blocks_path() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        blocked = await cache.allowed("https://example.com/private/page", UA)
        allowed = await cache.allowed("https://example.com/public/page", UA)
    assert blocked is False
    assert allowed is True


@respx.mock
async def test_ua_specific_rules() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text="User-agent: badbot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n",
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.com/public", "badbot") is False
        assert await cache.allowed("https://example.com/public", "goodbot") is True
        assert await cache.allowed("https://example.com/private", "goodbot") is False


# --- failures -> allow-all -------------------------------------------------


@respx.mock
async def test_robots_404_allows_everything() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.com/anything", UA) is True


@respx.mock
async def test_robots_connect_error_allows_everything() -> None:
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.com/anything", UA) is True


@respx.mock
async def test_robots_timeout_allows_everything() -> None:
    respx.get("https://example.com/robots.txt").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.com/anything", UA) is True


# --- caching ---------------------------------------------------------------


@respx.mock
async def test_robots_downloaded_exactly_once_under_concurrency() -> None:
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        results = await asyncio.gather(
            *(cache.allowed(f"https://example.com/p{i}", UA) for i in range(10))
        )
    assert route.call_count == 1
    assert len(cache._parsers) == 1
    assert all(isinstance(r, bool) for r in results)


# --- crawl-delay (Этап 6) ---------------------------------------------------


@respx.mock
async def test_crawl_delay_parsed() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nCrawl-delay: 10\nDisallow: /private\n"
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.crawl_delay("https://example.com/page", UA) == 10.0


@respx.mock
async def test_crawl_delay_absent_is_none() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.crawl_delay("https://example.com/page", UA) is None


# --- sitemaps (Этап 8) -------------------------------------------------------


@respx.mock
async def test_sitemaps_parsed() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=(
                "User-agent: *\nDisallow: /private\n"
                "Sitemap: https://example.com/sitemap.xml\n"
                "Sitemap: https://example.com/sitemap-news.xml\n"
            ),
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.sitemaps("https://example.com/page", UA) == [
            "https://example.com/sitemap.xml",
            "https://example.com/sitemap-news.xml",
        ]


@respx.mock
async def test_sitemaps_absent_is_empty() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.sitemaps("https://example.com/page", UA) == []


@respx.mock
async def test_sitemaps_uses_cached_parser() -> None:
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text="User-agent: *\nSitemap: https://example.com/sitemap.xml\n",
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("https://example.com/page", UA) is True
        sitemaps = await cache.sitemaps("https://example.com/page", UA)
    assert sitemaps == ["https://example.com/sitemap.xml"]
    assert route.call_count == 1  # no second robots.txt download


@respx.mock
async def test_http_and_https_cached_separately() -> None:
    respx.get("http://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\n")
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        cache = RobotsCache(client)
        assert await cache.allowed("http://example.com/private/x", UA) is False
        assert await cache.allowed("https://example.com/private/x", UA) is True
    assert len(cache._parsers) == 2
