"""CLI-точка входа politecrawl.

Этап 5: разбор аргументов (seed-URL, max-depth, per-domain concurrency),
запуск конкурентного обхода с ограничением глубины и печать итогового отчёта.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx

from politecrawl.dedup import UrlDedup
from politecrawl.export import (
    Edge,
    ExportFormatError,
    PageMeta,
    validate_extension,
    write_edges,
    write_pages,
    write_sitemap,
)
from politecrawl.fetcher import extract_links, extract_title, fetch
from politecrawl.ratelimit import PerDomainLimiter
from politecrawl.robots import RobotsCache


class CrawlStats:
    """Aggregated crawl counters: totals plus a per-host breakdown.

    Mutated only from crawl workers running in ONE event loop. Every increment
    is a plain dict/Counter mutation with no await between read and write, so it
    is atomic within an event-loop step — no lock is needed (single-threaded
    asyncio, cooperative scheduling). See TECHNICAL_PLAN §Этап 5.
    """

    def __init__(self) -> None:
        self.visited = 0
        self.skipped_dedup = 0
        self.skipped_robots = 0
        self.errors = 0
        self.per_host: dict[str, Counter[str]] = {}

    def _bump(self, host: str, key: str) -> None:
        self.per_host.setdefault(host, Counter())[key] += 1

    def record_visited(self, host: str) -> None:
        self.visited += 1
        self._bump(host, "visited")

    def record_skipped_dedup(self, host: str) -> None:
        self.skipped_dedup += 1
        self._bump(host, "skipped_dedup")

    def record_skipped_robots(self, host: str) -> None:
        self.skipped_robots += 1
        self._bump(host, "skipped_robots")

    def record_error(self, host: str) -> None:
        self.errors += 1
        self._bump(host, "errors")


class Crawler:
    """Depth-limited concurrent crawler assembled from Stage 1-4 modules.

    Must be constructed inside a running event loop: the frontier queue and
    the locks inside RobotsCache/PerDomainLimiter bind to the current loop.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        robots: RobotsCache,
        limiter: PerDomainLimiter,
        dedup: UrlDedup,
        max_depth: int,
        user_agent: str,
    ) -> None:
        self._client = client
        self._robots = robots
        self._limiter = limiter
        self._dedup = dedup
        self._max_depth = max_depth
        self._user_agent = user_agent
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.stats = CrawlStats()
        # Export accumulators (Stage 7). Mutated only from crawl workers in ONE
        # event loop with no await between read and write (list.append), so the
        # mutation is atomic within an event-loop step — no lock is needed
        # (same precedent as CrawlStats/UrlDedup, Stages 4-5).
        self.edges: list[Edge] = []
        self.pages: list[PageMeta] = []

    async def run(self, seeds: list[str], total_workers: int) -> None:
        """Crawl from seeds with a pool of workers until the frontier drains."""
        # Enqueue seeds BEFORE starting workers so queue.join() cannot return
        # on a momentarily empty queue.
        for seed in seeds:
            self._queue.put_nowait((seed, 0))
        workers = [asyncio.create_task(self._worker()) for _ in range(total_workers)]
        try:
            await self._queue.join()  # wait until the frontier is drained
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self) -> None:
        """Take frontier items forever; task_done() strictly in finally.

        Network errors are absorbed by fetch() and counted in stats, but
        _process can still raise on malformed input (e.g. urlsplit() rejects
        some malformed hrefs found on real pages). Such exceptions are caught
        here and counted as errors instead of killing this worker task, which
        would otherwise shrink the pool and could hang queue.join() forever.
        CancelledError is a BaseException and is not caught, so run()'s
        cancellation still stops the worker promptly.
        """
        while True:
            url, depth = await self._queue.get()
            try:
                await self._process(url, depth)
            except Exception:
                self.stats.record_error(self._safe_host(url))
            finally:
                self._queue.task_done()

    @staticmethod
    def _safe_host(url: str) -> str:
        """urlsplit(url).netloc, falling back to the raw url if unparsable."""
        try:
            return urlsplit(url).netloc
        except ValueError:
            return url

    async def _process(self, url: str, depth: int) -> None:
        """Pipeline for one URL: dedup -> robots -> rate-limited fetch -> links."""
        host = self._safe_host(url)

        # 1. dedup: atomic check-and-insert. Already seen -> skipped_dedup.
        if not self._dedup.add(url):
            self.stats.record_skipped_dedup(host)
            return

        # 2. robots: disallowed -> skipped_robots (checked BEFORE the fetch).
        if not await self._robots.allowed(url, self._user_agent):
            self.stats.record_skipped_robots(host)
            return

        # 3-4. per-domain slot held only around the fetch itself. Crawl-delay
        # comes from the already-warmed RobotsCache (same robots.txt loaded on
        # step 2 for allowed() — no extra network round-trip).
        delay = await self._robots.crawl_delay(url, self._user_agent)
        async with self._limiter.slot(host, crawl_delay=delay or 0.0):
            result = await fetch(self._client, url)
        # 4b. adjust adaptive backoff AFTER the slot is released (backoff must
        # not keep the semaphore busy longer than the fetch itself). A
        # transport error surfaces as status=None and raises the backoff.
        self._limiter.record_response(host, result.status)

        # 5. transport error -> errors; otherwise visited + record page metadata.
        if result.error is not None:
            self.stats.record_error(host)
            return
        self.stats.record_visited(host)
        self.pages.append(
            {
                "url": url,
                "status": result.status,
                "content_type": result.content_type,
                "title": extract_title(result.body),
            }
        )

        # 6. extract links: record EVERY outgoing edge (this is the link graph,
        # not the crawl graph — edges are kept even when the target is beyond
        # max_depth or will later be dropped by dedup/robots), then enqueue
        # targets only if depth allows. Duplicates are enqueued unfiltered:
        # step 1 of the worker that picks them up is the single atomic dedup
        # point.
        links = extract_links(url, result.body)
        for link in links:
            self.edges.append((url, link))
        if depth + 1 <= self._max_depth:
            for link in links:
                self._queue.put_nowait((link, depth + 1))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="politecrawl",
        description="Polite structural async web crawler.",
    )
    parser.add_argument("seeds", nargs="+", help="one or more seed URLs")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="crawl depth from each seed (seed = depth 0)",
    )
    parser.add_argument(
        "--per-domain-concurrency",
        type=int,
        default=2,
        help="max concurrent requests to a single host",
    )
    parser.add_argument(
        "--total-workers",
        type=int,
        default=8,
        help="size of the worker pool",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default="politecrawl/0.0",
        help="User-Agent for requests and robots.can_fetch",
    )
    parser.add_argument(
        "--export-edges",
        type=str,
        default=None,
        metavar="PATH",
        help="write the link graph (source->target edges) to PATH (.jsonl or .csv)",
    )
    parser.add_argument(
        "--export-sitemap",
        type=str,
        default=None,
        metavar="PATH",
        help="write a sitemap-like XML of visited URLs to PATH",
    )
    parser.add_argument(
        "--export-pages",
        type=str,
        default=None,
        metavar="PATH",
        help="write page metadata (url, status, content_type, title) to PATH (.jsonl or .csv)",
    )
    return parser


async def _run(
    seeds: list[str],
    *,
    max_depth: int,
    per_domain_concurrency: int,
    total_workers: int,
    user_agent: str,
) -> tuple[Crawler, float]:
    """Build the client and dependencies inside the event loop and crawl."""
    start = time.perf_counter()
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"user-agent": user_agent},
    ) as client:
        crawler = Crawler(
            client=client,
            robots=RobotsCache(client),
            limiter=PerDomainLimiter(per_domain_concurrency),
            dedup=UrlDedup(),
            max_depth=max_depth,
            user_agent=user_agent,
        )
        await crawler.run(seeds, total_workers)
    elapsed = time.perf_counter() - start
    return crawler, elapsed


def _format_report(stats: CrawlStats, elapsed: float) -> str:
    lines = [
        "politecrawl report",
        f"  visited:        {stats.visited}",
        f"  skipped_dedup:  {stats.skipped_dedup}",
        f"  skipped_robots: {stats.skipped_robots}",
        f"  errors:         {stats.errors}",
        f"  elapsed:        {elapsed:.3f}s",
        "",
        "per host:",
    ]
    for host in sorted(stats.per_host):
        c = stats.per_host[host]
        lines.append(
            f"  {host}  visited={c['visited']} "
            f"skipped_dedup={c['skipped_dedup']} "
            f"skipped_robots={c['skipped_robots']} errors={c['errors']}"
        )
    return "\n".join(lines)


def _validate_export_paths(args: argparse.Namespace) -> None:
    """Validate tabular export extensions up front, before the crawl runs.

    Raises ExportFormatError so a typo in --export-edges/--export-pages fails
    fast instead of after a full (wasted) crawl. Sitemap paths are not checked:
    write_sitemap ignores the extension.
    """
    for path in (args.export_edges, args.export_pages):
        if path is not None:
            validate_extension(path)


def _run_exports(crawler: Crawler, args: argparse.Namespace) -> None:
    """Write requested exports. Raises ExportFormatError on a bad extension."""
    if args.export_edges is not None:
        write_edges(crawler.edges, args.export_edges)
    if args.export_pages is not None:
        write_pages(crawler.pages, args.export_pages)
    if args.export_sitemap is not None:
        write_sitemap([str(p["url"]) for p in crawler.pages], args.export_sitemap)


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: разобрать аргументы, обойти граф, отчёт + экспорт."""
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        _validate_export_paths(args)
    except ExportFormatError as exc:
        print(f"politecrawl: {exc}", file=sys.stderr)
        return 2
    crawler, elapsed = asyncio.run(
        _run(
            args.seeds,
            max_depth=args.max_depth,
            per_domain_concurrency=args.per_domain_concurrency,
            total_workers=args.total_workers,
            user_agent=args.user_agent,
        )
    )
    print(_format_report(crawler.stats, elapsed))
    try:
        _run_exports(crawler, args)
    except ExportFormatError as exc:  # pragma: no cover - guarded by validation
        print(f"politecrawl: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
