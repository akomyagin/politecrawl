"""robots.txt parsing and per-domain caching."""

from __future__ import annotations

import asyncio
import urllib.robotparser
from urllib.parse import urlsplit

import httpx


def _host_key(url: str) -> str:
    """Cache key for a URL: scheme + netloc (port included when present).

    Scheme is part of the key on purpose: http:// and https:// of the same
    host are separate robots.txt entries.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RobotsCache:
    """Lazily loading per-domain cache of parsed robots.txt rules.

    robots.txt of each domain (keyed by scheme://netloc) is downloaded exactly
    once, even under N concurrent callers. A failed download (network error or
    HTTP >= 400) is treated as "everything allowed" and never raises.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._registry_lock = asyncio.Lock()

    async def allowed(self, url: str, user_agent: str) -> bool:
        """Return whether robots.txt of the URL's host permits fetching it."""
        key = _host_key(url)
        rfp = await self._get_parser(key)
        return rfp.can_fetch(user_agent, url)

    async def _get_parser(self, key: str) -> urllib.robotparser.RobotFileParser:
        """Return the cached parser for key, loading robots.txt exactly once.

        Double-checked locking: fast path without the lock for already-cached
        hosts; slow path re-checks under the lock so a waiter never re-downloads.

        Unlike PerDomainLimiter (Stage 3), the network request here happens
        DELIBERATELY while holding _registry_lock: releasing it during the
        download would let two workers racing on a fresh host both fetch
        robots.txt, breaking the "downloaded exactly once" invariant. The cost
        is that first-time loads of different hosts are serialized by the
        single lock — acceptable for this single-machine scope: robots.txt is
        small, loaded once per host, and the warmed-up fast path takes no lock.
        """
        # Fast path: parser already cached, no lock needed.
        rfp = self._parsers.get(key)
        if rfp is not None:
            return rfp
        # Slow path: create exactly once under the registry lock.
        async with self._registry_lock:
            rfp = self._parsers.get(key)
            if rfp is None:
                rfp = await self._load_parser(key)
                self._parsers[key] = rfp
            return rfp

    async def _load_parser(self, key: str) -> urllib.robotparser.RobotFileParser:
        """Download and parse robots.txt for key; any failure means allow-all.

        parse([]) puts the parser into a valid "no rules" state, in which
        can_fetch returns True for any URL and user agent.
        """
        rfp = urllib.robotparser.RobotFileParser()
        try:
            response = await self._client.get(f"{key}/robots.txt")
        except httpx.HTTPError:
            rfp.parse([])  # network/timeout -> allow-all
            return rfp
        if response.status_code >= 400:
            rfp.parse([])  # 4xx/5xx robots.txt -> allow-all
            return rfp
        rfp.parse(response.text.splitlines())
        return rfp
