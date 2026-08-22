"""Дедупликация URL: нормализация + проверка «видели ли уже».

Этап 4: нормализация URL (схема/хост в нижний регистр, отбрасывание
фрагмента, сортировка query, дефолтные порты) + отметка «видели».

Выбор структуры для MVP: обычный in-memory set нормализованных URL.
Bloom filter отложен в POST_MVP — на single-machine скоупе с ограниченной
глубиной множество URL умещается в памяти, а set даёт нулевой false-positive
(bloom может ошибочно счесть новый URL уже посещённым и пропустить его).
См. docs/TECHNICAL_PLAN.md §Этап 4.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _strip_default_port(scheme: str, netloc: str) -> str:
    """Remove a default port (80 for http, 443 for https) from netloc.

    netloc is lower-cased by the caller already (including userinfo, if
    present); userinfo is otherwise left untouched here.
    """
    userinfo, _, hostport = netloc.rpartition("@")
    host, sep, port = hostport.partition(":")
    if sep and port:
        default_port = _DEFAULT_PORTS.get(scheme)
        if default_port is not None and port.isdigit() and int(port) == default_port:
            hostport = host
    return f"{userinfo}@{hostport}" if userinfo else hostport


def normalize(url: str) -> str:
    """Canonicalize a URL for dedup comparison.

    - scheme and host are lower-cased;
    - the fragment is dropped;
    - the default port (:80 for http, :443 for https) is stripped;
    - query parameters are sorted into a stable order;
    - an empty path is normalized to "/".
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = _strip_default_port(scheme, parts.netloc.lower())
    path = parts.path if parts.path else "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, ""))


class UrlDedup:
    """Synchronous wrapper over a set[str] of normalized, already-seen URLs.

    add() combines the "have we seen this?" check and the insertion into one
    synchronous method call, so it is atomic within a single event-loop step
    even though it is not itself an async def (there is no await inside, so
    no other task can interleave between the check and the insertion).
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add(self, url: str) -> bool:
        """Normalize url, add it, and return True iff it was not seen before."""
        key = normalize(url)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def seen(self, url: str) -> bool:
        """Return whether an equivalent URL has already been added."""
        return normalize(url) in self._seen

    def snapshot_seen(self) -> set[str]:
        """Return a copy of the seen-set for checkpointing (Stage 9).

        Entries are already normalized (add() stores normalized keys only).
        A copy, so the caller's snapshot stays stable while the crawl keeps
        mutating this dedup instance.
        """
        return set(self._seen)

    def load_seen(self, seen: Iterable[str]) -> None:
        """Replace the seen-set with a checkpoint snapshot (Stage 9).

        Snapshot entries were produced by add(), i.e. they are ALREADY
        normalized. They are loaded as-is, deliberately WITHOUT re-running
        normalize(): re-normalizing an already-normalized URL is redundant
        work, and doing it here would hide format drift instead of exposing
        it in tests.
        """
        self._seen = set(seen)
