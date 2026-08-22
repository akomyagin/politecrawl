"""Тесты Этапа 1: fetch() и extract_links()."""

from __future__ import annotations

import asyncio

import httpx
import respx

from politecrawl.fetcher import extract_links, extract_sitemap_urls, extract_title, fetch

# --- fetch -----------------------------------------------------------------


@respx.mock
async def test_fetch_ok() -> None:
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            html="<a href='/next'>x</a>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/")
    assert result.status == 200
    assert result.error is None
    assert "next" in result.body
    assert result.content_type is not None
    assert result.content_type.startswith("text/html")
    assert result.url == "https://example.com/"


@respx.mock
async def test_fetch_404_is_not_crawl_error() -> None:
    respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/missing")
    assert result.status == 404
    assert result.error is None
    assert result.body == ""


@respx.mock
async def test_fetch_follows_redirect() -> None:
    respx.get("https://example.com/old").mock(
        return_value=httpx.Response(301, headers={"location": "https://example.com/new"})
    )
    respx.get("https://example.com/new").mock(return_value=httpx.Response(200, html="ok"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/old")
    assert result.status == 200
    assert result.error is None
    assert "ok" in result.body
    assert result.url == "https://example.com/old"


@respx.mock
async def test_fetch_timeout_returns_error_result() -> None:
    respx.get("https://example.com/slow").mock(side_effect=httpx.TimeoutException("timed out"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/slow")
    assert result.status is None
    assert result.error is not None
    assert "Timeout" in result.error
    assert result.body == ""
    assert result.content_type is None
    assert result.url == "https://example.com/slow"


@respx.mock
async def test_fetch_connect_error() -> None:
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/down")
    assert result.status is None
    assert result.error is not None
    assert "ConnectError" in result.error


# --- extract_links ---------------------------------------------------------


def test_extract_links_relative_are_absolutized() -> None:
    html = '<a href="/next">a</a><a href="sub/page">b</a><a href="../up">c</a>'
    result = extract_links("https://example.com/dir/", html)
    assert result == [
        "https://example.com/next",
        "https://example.com/dir/sub/page",
        "https://example.com/up",
    ]


def test_extract_links_absolute_kept() -> None:
    html = '<a href="https://other.example/x">x</a>'
    result = extract_links("https://example.com/", html)
    assert "https://other.example/x" in result


def test_extract_links_anchor_kept_with_base() -> None:
    html = '<a href="#section">s</a>'
    result = extract_links("https://example.com/page", html)
    assert "https://example.com/page#section" in result


def test_extract_links_non_http_schemes_filtered() -> None:
    html = (
        '<a href="mailto:a@b.com">m</a><a href="javascript:void(0)">j</a><a href="tel:+123">t</a>'
    )
    result = extract_links("https://example.com/", html)
    assert result == []


def test_extract_links_empty_and_missing_href_skipped() -> None:
    html = '<a href="">empty</a><a>no href</a><a href="/ok">ok</a>'
    result = extract_links("https://example.com/", html)
    assert result == ["https://example.com/ok"]


def test_extract_links_preserves_order_and_duplicates() -> None:
    html = '<a href="/a">1</a><a href="/b">2</a><a href="/a">3</a>'
    result = extract_links("https://example.com/", html)
    assert result == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/a",
    ]


# --- extract_title ---------------------------------------------------------


def test_extract_title_basic() -> None:
    assert extract_title("<html><head><title>Hi</title></head></html>") == "Hi"


def test_extract_title_absent() -> None:
    assert extract_title("<html><body>x</body></html>") is None


def test_extract_title_whitespace_collapsed() -> None:
    assert extract_title("<title>\n  Hello   World\n</title>") == "Hello World"


def test_extract_title_empty_is_none() -> None:
    assert extract_title("<title>   </title>") is None


def test_extract_title_first_wins() -> None:
    assert extract_title("<title>First</title><title>Second</title>") == "First"


# --- extract_sitemap_urls (Этап 8) ------------------------------------------

SITEMAP_XMLNS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def test_extract_sitemap_urls_urlset() -> None:
    xml = (
        f"<urlset {SITEMAP_XMLNS}>"
        "<url><loc>https://x/a</loc></url>"
        "<url><loc>https://x/b</loc></url>"
        "</urlset>"
    )
    assert extract_sitemap_urls(xml) == (["https://x/a", "https://x/b"], [])


def test_extract_sitemap_urls_index() -> None:
    xml = (
        f"<sitemapindex {SITEMAP_XMLNS}>"
        "<sitemap><loc>https://x/sm1.xml</loc></sitemap>"
        "<sitemap><loc>https://x/sm2.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    assert extract_sitemap_urls(xml) == ([], ["https://x/sm1.xml", "https://x/sm2.xml"])


def test_extract_sitemap_urls_malformed() -> None:
    assert extract_sitemap_urls("this is not xml <<<") == ([], [])


def test_extract_sitemap_urls_no_namespace() -> None:
    xml = "<urlset><url><loc>https://x/a</loc></url></urlset>"
    assert extract_sitemap_urls(xml) == (["https://x/a"], [])


def test_extract_sitemap_urls_filters_non_http() -> None:
    xml = (
        f"<urlset {SITEMAP_XMLNS}>"
        "<url><loc>ftp://x/file</loc></url>"
        "<url><loc>https://x/ok</loc></url>"
        "</urlset>"
    )
    assert extract_sitemap_urls(xml) == (["https://x/ok"], [])


def test_extract_sitemap_urls_empty() -> None:
    assert extract_sitemap_urls(f"<urlset {SITEMAP_XMLNS}/>") == ([], [])


# --- integration -----------------------------------------------------------


@respx.mock
async def test_fetch_then_extract_links_end_to_end() -> None:
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            html=(
                '<a href="/page1">1</a>'
                '<a href="https://other.example/x">2</a>'
                '<a href="mailto:z@z">3</a>'
            ),
        )
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        result = await fetch(client, "https://example.com/")
    assert result.error is None
    links = extract_links("https://example.com/", result.body)
    assert links == ["https://example.com/page1", "https://other.example/x"]


@respx.mock
async def test_fetch_two_seeds_concurrently() -> None:
    respx.get("https://a.example/").mock(return_value=httpx.Response(200, html="a"))
    respx.get("https://b.example/").mock(return_value=httpx.Response(200, html="b"))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            fetch(client, "https://a.example/"),
            fetch(client, "https://b.example/"),
        )
    assert [r.status for r in results] == [200, 200]
