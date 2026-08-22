"""Чекпоинт обхода: сериализация состояния в JSON и атомарная запись.

Этап 9: сосед export.py по духу — чистые функции над уже собранными
структурами данных, только stdlib (json/os/tempfile), без сети и без async.
Модуль не знает про asyncio.Queue/Crawler: принимает и отдаёт простые
python-структуры, упакованные в CrawlSnapshot. Атомарность записи (tmp +
os.replace) — обязательный инвариант: креш посреди записи не должен испортить
последний валидный чекпоинт.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Any

from politecrawl.export import Edge, PageMeta

SCHEMA_VERSION = 1

# Top-level keys every checkpoint file must contain (see load_checkpoint).
_REQUIRED_KEYS = (
    "version",
    "seeds",
    "max_depth",
    "user_agent",
    "frontier",
    "seen",
    "stats",
    "edges",
    "pages",
    "sitemap_hosts",
    "fetched_sitemaps",
)

# Counter keys required inside stats.totals (mirrors CrawlStats fields).
_STATS_TOTAL_KEYS = (
    "visited",
    "skipped_dedup",
    "skipped_robots",
    "errors",
    "sitemap_urls",
)


class CheckpointFormatError(ValueError):
    """Raised when a checkpoint file is corrupt, incomplete, or incompatible."""


@dataclass
class CrawlSnapshot:
    """Serializable snapshot of one crawl's state (see plan, §Что персистится).

    PerDomainLimiter state (_next_dispatch/_backoff) is deliberately absent:
    it is an ephemeral politeness metric of a single run, tied to
    time.monotonic() which is not comparable across processes. Resume starts
    with a fresh limiter by design, not by omission.
    """

    seeds: list[str]
    max_depth: int
    user_agent: str
    frontier: list[tuple[str, int]]  # unprocessed (url, depth), queue + in-flight
    seen: set[str]  # normalized URLs already handled by UrlDedup
    stats_totals: dict[str, int]  # visited/skipped_dedup/skipped_robots/errors/sitemap_urls
    per_host: dict[str, Counter[str]]  # per-host breakdown of the same counters
    edges: list[Edge]
    pages: list[PageMeta]
    sitemap_hosts: set[str]  # hosts whose sitemap discovery already ran
    fetched_sitemaps: set[str]  # sitemap URLs already fetched


def save_checkpoint(snapshot: CrawlSnapshot, path: str) -> None:
    """Atomically write snapshot to path as a single JSON object.

    Sets are serialized as SORTED lists (deterministic format: stable diffs,
    easy tests). The write goes to a temp file in the SAME directory as path
    (so os.replace is atomic on one filesystem), then os.replace(tmp, path):
    a crash mid-write leaves either the previous valid file or the new one,
    never a truncated hybrid.
    """
    payload = {
        "version": SCHEMA_VERSION,
        "seeds": snapshot.seeds,
        "max_depth": snapshot.max_depth,
        "user_agent": snapshot.user_agent,
        "frontier": [[url, depth] for url, depth in snapshot.frontier],
        "seen": sorted(snapshot.seen),
        "stats": {
            "totals": snapshot.stats_totals,
            "per_host": {host: dict(counts) for host, counts in snapshot.per_host.items()},
        },
        "edges": [[source, target] for source, target in snapshot.edges],
        "pages": snapshot.pages,
        "sitemap_hosts": sorted(snapshot.sitemap_hosts),
        "fetched_sitemaps": sorted(snapshot.fetched_sitemaps),
    }
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".checkpoint-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        # Never leave a stray temp file behind; the target path is untouched.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_checkpoint(path: str) -> CrawlSnapshot:
    """Load and validate a checkpoint written by save_checkpoint.

    Raises CheckpointFormatError (a domain error, like ExportFormatError in
    export.py) on corrupt JSON, missing required fields, malformed structure
    or a schema-version mismatch — never a bare JSONDecodeError/KeyError.
    A missing file raises FileNotFoundError as-is: "no checkpoint" and
    "broken checkpoint" are different situations for the caller.
    """
    with open(path, encoding="utf-8") as f:
        try:
            raw: Any = json.load(f)
        except json.JSONDecodeError as exc:
            raise CheckpointFormatError(f"checkpoint {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CheckpointFormatError(f"checkpoint {path!r} must be a JSON object")
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise CheckpointFormatError(
            f"checkpoint {path!r} is missing required fields: {', '.join(missing)}"
        )
    version = raw["version"]
    if version != SCHEMA_VERSION:
        raise CheckpointFormatError(
            f"checkpoint {path!r} has unsupported schema version {version!r} "
            f"(expected {SCHEMA_VERSION}); it cannot be resumed by this build"
        )
    try:
        stats = raw["stats"]
        totals = {str(key): int(value) for key, value in stats["totals"].items()}
        missing_totals = [key for key in _STATS_TOTAL_KEYS if key not in totals]
        if missing_totals:
            raise CheckpointFormatError(
                f"checkpoint {path!r} is missing stats counters: {', '.join(missing_totals)}"
            )
        return CrawlSnapshot(
            seeds=[str(seed) for seed in raw["seeds"]],
            max_depth=int(raw["max_depth"]),
            user_agent=str(raw["user_agent"]),
            frontier=[(str(url), int(depth)) for url, depth in raw["frontier"]],
            seen={str(url) for url in raw["seen"]},
            stats_totals=totals,
            per_host={
                str(host): Counter({str(key): int(value) for key, value in counts.items()})
                for host, counts in stats["per_host"].items()
            },
            edges=[(str(source), str(target)) for source, target in raw["edges"]],
            pages=[dict(page) for page in raw["pages"]],
            sitemap_hosts={str(host) for host in raw["sitemap_hosts"]},
            fetched_sitemaps={str(url) for url in raw["fetched_sitemaps"]},
        )
    except CheckpointFormatError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CheckpointFormatError(f"checkpoint {path!r} is malformed: {exc!r}") from exc
