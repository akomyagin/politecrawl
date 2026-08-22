"""HTTP-загрузка страниц и извлечение ссылок.

Этап 1: базовый asyncio-fetch через httpx.AsyncClient + парсинг ссылок.
Этап 8: extract_sitemap_urls — парсинг sitemap XML (urlset / sitemapindex).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx


@dataclass(frozen=True)
class FetchResult:
    url: str  # requested URL (before redirects)
    status: int | None  # HTTP status; None if request never completed (network/timeout)
    body: str  # response body ("" on error)
    content_type: str | None  # value of Content-Type header (or None)
    error: str | None  # repr() of exception, or None on success


async def fetch(client: httpx.AsyncClient, url: str) -> FetchResult:
    """Fetch a URL, returning a FetchResult instead of raising on network errors.

    HTTP 4xx/5xx statuses are not crawl errors: they come back as a normal
    FetchResult with the corresponding status and error=None. Only transport
    failures (httpx.HTTPError: timeouts, connection errors) produce an error
    result with status=None.
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return FetchResult(
            url=url,
            status=None,
            body="",
            content_type=None,
            error=repr(exc),
        )
    return FetchResult(
        url=url,
        status=response.status_code,
        body=response.text,
        content_type=response.headers.get("content-type"),
        error=None,
    )


class _LinkParser(HTMLParser):
    """Collects href attribute values from <a> tags."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def extract_links(base_url: str, html: str) -> list[str]:
    """Extract outgoing absolute http/https links from HTML.

    Relative hrefs are resolved against base_url. Non-http(s) schemes
    (mailto:, javascript:, tel:) are filtered out. Order is preserved and
    duplicates are kept (deduplication is Stage 4's responsibility).
    """
    parser = _LinkParser()
    parser.feed(html)
    links: list[str] = []
    for href in parser.hrefs:
        absolute = urljoin(base_url, href)
        if urlsplit(absolute).scheme in {"http", "https"}:
            links.append(absolute)
    return links


class _TitleParser(HTMLParser):
    """Captures the text of the first <title>…</title> element."""

    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._done = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self._done:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self._done:
            self._chunks.append(data)

    @property
    def title(self) -> str | None:
        text = " ".join("".join(self._chunks).split())
        return text or None


def extract_title(html: str) -> str | None:
    """Return the text of the first <title>…</title>, or None if absent.

    Whitespace is collapsed to single spaces and trimmed; an empty or
    whitespace-only title yields None. Only the first <title> is used.
    """
    parser = _TitleParser()
    parser.feed(html)
    return parser.title


def _local_tag(tag: str) -> str:
    """Local element name with any {namespace} prefix stripped."""
    return tag.rsplit("}", 1)[-1]


def extract_sitemap_urls(xml: str) -> tuple[list[str], list[str]]:
    """Parse a sitemap XML, returning (page_urls, nested_sitemap_urls).

    Handles both sitemaps.org 0.9 document types:
      - <urlset>:      <url><loc> entries -> page_urls
      - <sitemapindex>: <sitemap><loc> entries -> nested_sitemap_urls
    Namespace-agnostic (matches by local tag name, tolerating a missing or
    unexpected xmlns). Malformed XML yields ([], []) — a bad sitemap must not
    crash the crawl. Only http/https <loc> values are kept.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return [], []
    page_urls: list[str] = []
    nested_sitemap_urls: list[str] = []
    root_tag = _local_tag(root.tag)
    if root_tag == "urlset":
        target, entry_tag = page_urls, "url"
    elif root_tag == "sitemapindex":
        target, entry_tag = nested_sitemap_urls, "sitemap"
    else:
        return [], []
    for entry in root:
        if _local_tag(entry.tag) != entry_tag:
            continue
        for child in entry:
            if _local_tag(child.tag) != "loc" or not child.text:
                continue
            loc = child.text.strip()
            if urlsplit(loc).scheme in {"http", "https"}:
                target.append(loc)
    return page_urls, nested_sitemap_urls
