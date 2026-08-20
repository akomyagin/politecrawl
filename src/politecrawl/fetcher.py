"""HTTP-загрузка страниц и извлечение ссылок.

Этап 1: базовый asyncio-fetch через httpx.AsyncClient + парсинг ссылок.
"""

from __future__ import annotations

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
