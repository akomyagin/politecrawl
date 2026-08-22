"""CLI-точка входа politecrawl.

Этап 5: разбор аргументов (seed-URL, max-depth, per-domain concurrency),
запуск конкурентного обхода с ограничением глубины и печать итогового отчёта.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx

from politecrawl.checkpoint import (
    CheckpointFormatError,
    CrawlSnapshot,
    load_checkpoint,
    save_checkpoint,
)
from politecrawl.dedup import UrlDedup, normalize
from politecrawl.export import (
    Edge,
    ExportFormatError,
    PageMeta,
    validate_extension,
    write_edges,
    write_pages,
    write_sitemap,
)
from politecrawl.fetcher import extract_links, extract_sitemap_urls, extract_title, fetch
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
        self.sitemap_urls = 0
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

    def record_sitemap_url(self, host: str) -> None:
        """Count a page URL discovered in a sitemap and enqueued (Stage 8).

        Counts discovery, not visits: some of these URLs are later dropped at
        the fence by dedup/robots. That is intentional — the counter reflects
        the sitemap's contribution as a link source.
        """
        self.sitemap_urls += 1
        self._bump(host, "sitemap_urls")

    def totals(self) -> dict[str, int]:
        """Return the aggregate counters as a plain dict (Stage 9 checkpoint)."""
        return {
            "visited": self.visited,
            "skipped_dedup": self.skipped_dedup,
            "skipped_robots": self.skipped_robots,
            "errors": self.errors,
            "sitemap_urls": self.sitemap_urls,
        }

    def restore(self, totals: dict[str, int], per_host: dict[str, Counter[str]]) -> None:
        """Restore counters from a checkpoint snapshot, symmetric to totals().

        Stage 9: called once before a resumed crawl starts, so the report and
        the per-host breakdown include the first run's contribution.
        """
        self.visited = totals["visited"]
        self.skipped_dedup = totals["skipped_dedup"]
        self.skipped_robots = totals["skipped_robots"]
        self.errors = totals["errors"]
        self.sitemap_urls = totals["sitemap_urls"]
        self.per_host = {host: Counter(counts) for host, counts in per_host.items()}


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
        checkpoint_path: str | None = None,
        checkpoint_every: int = 100,
    ) -> None:
        self._client = client
        self._robots = robots
        self._limiter = limiter
        self._dedup = dedup
        self._max_depth = max_depth
        self._user_agent = user_agent
        # Checkpointing (Stage 9). checkpoint_path=None keeps Stage 8
        # behaviour bit-for-bit: no file I/O, no snapshot work at all.
        self._checkpoint_path = checkpoint_path
        self._checkpoint_every = checkpoint_every
        self._checkpoint_stopped = False  # final snapshot written; block periodic ones
        self._processed_count = 0  # completed frontier items (periodic trigger)
        self._seeds: list[str] = []  # remembered by run() for the snapshot
        # In-flight frontier items: taken from the queue but not yet
        # task_done(). A snapshot of the frontier is queue contents + this
        # registry — without it, items being processed at snapshot time would
        # be lost on a crash between get() and the end of _process. A Counter
        # (not a set), so short-lived duplicates of one item held by two
        # workers cannot cancel each other out.
        self._in_flight: Counter[tuple[str, int]] = Counter()
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.stats = CrawlStats()
        # Export accumulators (Stage 7). Mutated only from crawl workers in ONE
        # event loop with no await between read and write (list.append), so the
        # mutation is atomic within an event-loop step — no lock is needed
        # (same precedent as CrawlStats/UrlDedup, Stages 4-5).
        self.edges: list[Edge] = []
        self.pages: list[PageMeta] = []
        # Sitemap discovery state (Stage 8). Both sets are mutated with plain
        # check-and-insert (no await between read and write), so mutation is
        # atomic within an event-loop step — same lock-free precedent as
        # UrlDedup.add and the edges/pages accumulators above.
        self._sitemap_hosts: set[str] = set()  # hosts whose discovery already ran
        self._fetched_sitemaps: set[str] = set()  # sitemap URLs already fetched

    async def run(self, seeds: list[str], total_workers: int, *, resume: bool = False) -> None:
        """Crawl from seeds with a pool of workers until the frontier drains.

        With resume=True seeds are NOT enqueued: apply_snapshot() already
        filled the queue with the saved frontier, and the seeds themselves are
        either in the seen-set or in that frontier — re-enqueueing them would
        only add a spurious dedup pass. They are still remembered for the
        compatibility check stored in every snapshot.
        """
        self._seeds = list(seeds)
        if not resume:
            # Enqueue seeds BEFORE starting workers so queue.join() cannot
            # return on a momentarily empty queue.
            for seed in seeds:
                self._queue.put_nowait((seed, 0))
        workers = [asyncio.create_task(self._worker()) for _ in range(total_workers)]
        try:
            await self._queue.join()  # wait until the frontier is drained
        finally:
            # Final checkpoint FIRST, synchronously, BEFORE cancelling the
            # workers: at this point every worker is parked at an await, so
            # queue + in-flight registry are mutually consistent and still
            # include the tasks that cancellation would otherwise strip from
            # the registry (their finally blocks run task_done()). The flag
            # also stops those finally blocks from overwriting this snapshot
            # with a periodic one that has already lost the in-flight items.
            if self._checkpoint_path is not None:
                self._checkpoint_stopped = True
                self._write_checkpoint()
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
            item = await self._queue.get()
            # Stage 9: register the item as in-flight BEFORE processing, so a
            # snapshot taken while this task awaits inside _process() still
            # sees it in the frontier (queue contents alone would lose it).
            self._in_flight[item] += 1
            url, depth = item
            try:
                await self._process(url, depth)
            except Exception:
                self.stats.record_error(self._safe_host(url))
            finally:
                self._in_flight[item] -= 1
                if not self._in_flight[item]:
                    del self._in_flight[item]
                self._queue.task_done()
                self._checkpoint_after_task()

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

        # 2b. sitemap discovery (Stage 8): once per host, only for depth-0 seeds
        # that passed robots. Enqueues sitemap-declared page URLs as additional
        # depth-0 entry points. The check-and-add guard has no await between
        # read and write, so it runs once per host across all workers.
        if depth == 0 and host not in self._sitemap_hosts:
            self._sitemap_hosts.add(host)
            await self._discover_sitemaps(url)

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

    async def _discover_sitemaps(self, seed_url: str) -> None:
        """Fetch sitemaps declared in the seed host's robots.txt (Stage 8).

        Sitemap URLs come from the already-warmed RobotsCache (the same
        robots.txt loaded for allowed() on step 2 — no extra network
        round-trip). Page URLs found in sitemaps are enqueued at depth 0: a
        sitemap is the site's declared list of canonical entry points, so they
        crawl their own outgoing links to the full max_depth. They still pass
        the normal dedup -> robots -> rate-limited fetch pipeline in _process.

        <sitemapindex> is expanded exactly ONE level: child sitemaps of an
        index are fetched, but an index nested inside another index is not
        recursed into (POST_MVP). Fetch/parse failures are absorbed by
        fetch()/extract_sitemap_urls, so a bad sitemap never crashes the seed's
        own processing.
        """
        for sitemap_url in await self._robots.sitemaps(seed_url, self._user_agent):
            for child_url in await self._ingest_sitemap(sitemap_url):
                # One level only: nested sitemaps declared by a child index
                # are ignored (returned list is dropped).
                await self._ingest_sitemap(child_url)

    async def _ingest_sitemap(self, sitemap_url: str) -> list[str]:
        """Fetch one sitemap politely and enqueue its page URLs at depth 0.

        Returns nested sitemap URLs when the document is a <sitemapindex>
        (empty for a plain <urlset>, an already-fetched sitemap, or any
        fetch/parse failure). The fetch goes through the same per-domain
        limiter slot (concurrency cap + crawl-delay + backoff) as page
        fetches: a sitemap download is a request to the domain like any other.
        """
        # Atomic check-and-insert (no await in between): each sitemap URL is
        # fetched at most once per crawl, even when several indexes share
        # child sitemaps or hosts declare the same sitemap.
        if sitemap_url in self._fetched_sitemaps:
            return []
        self._fetched_sitemaps.add(sitemap_url)
        host = self._safe_host(sitemap_url)
        delay = await self._robots.crawl_delay(sitemap_url, self._user_agent)
        async with self._limiter.slot(host, crawl_delay=delay or 0.0):
            result = await fetch(self._client, sitemap_url)
        self._limiter.record_response(host, result.status)
        if result.error is not None:
            return []
        page_urls, nested_sitemap_urls = extract_sitemap_urls(result.body)
        for page_url in page_urls:
            self.stats.record_sitemap_url(self._safe_host(page_url))
            self._queue.put_nowait((page_url, 0))
        return nested_sitemap_urls

    def export_snapshot(self) -> CrawlSnapshot:
        """Collect the crawl state into a serializable CrawlSnapshot (Stage 9).

        Synchronous on purpose: no await between reads of related structures,
        so the snapshot is internally consistent (every other task is parked
        at an await while this runs). The frontier is queue contents plus the
        in-flight registry.

        In-flight URLs are REMOVED from the seen-set copy: _process() marks a
        URL seen synchronously before its first await, so an observable
        in-flight item always OWNS its _seen entry while none of its effects
        (stats/pages/edges) are recorded yet. Keeping it in seen would make
        resume drop the URL at the dedup fence and lose it forever; removing
        it re-processes it cleanly. (A dedup-rejected duplicate is never
        observable in-flight: from add() returning False to task_done() there
        is no await, so no snapshot can run in between.)
        """
        # Peeking at Queue._queue (a deque) is an internal-attribute read, but
        # it is the only way to observe queue contents without consuming them:
        # a get/put drain-and-refill would corrupt join() accounting. The read
        # is synchronous and mutation-free, hence safe on one event loop.
        frontier: list[tuple[str, int]] = list(self._queue._queue)  # type: ignore[attr-defined]
        seen = self._dedup.snapshot_seen()
        for item, count in self._in_flight.items():
            frontier.extend([item] * count)
            seen.discard(normalize(item[0]))
        return CrawlSnapshot(
            seeds=list(self._seeds),
            max_depth=self._max_depth,
            user_agent=self._user_agent,
            frontier=frontier,
            seen=seen,
            stats_totals=self.stats.totals(),
            per_host={host: Counter(counts) for host, counts in self.stats.per_host.items()},
            edges=list(self.edges),
            pages=[dict(page) for page in self.pages],
            sitemap_hosts=set(self._sitemap_hosts),
            fetched_sitemaps=set(self._fetched_sitemaps),
        )

    def apply_snapshot(self, snapshot: CrawlSnapshot) -> None:
        """Initialize crawler state from a checkpoint BEFORE run(resume=True).

        Fills the queue with the saved frontier ONLY — seeds are not
        re-enqueued (they are already in the seen-set or in this frontier).
        PerDomainLimiter state is deliberately not restored: see the
        CrawlSnapshot docstring in checkpoint.py.
        """
        for frontier_item in snapshot.frontier:
            self._queue.put_nowait(frontier_item)
        self._dedup.load_seen(snapshot.seen)
        self.stats.restore(snapshot.stats_totals, snapshot.per_host)
        self.edges = list(snapshot.edges)
        self.pages = [dict(page) for page in snapshot.pages]
        self._sitemap_hosts = set(snapshot.sitemap_hosts)
        self._fetched_sitemaps = set(snapshot.fetched_sitemaps)

    def _checkpoint_after_task(self) -> None:
        """Periodic checkpoint trigger: every N completed frontier items.

        Called from the worker's finally right after task_done() — a point
        with no half-applied mutations (plan §Когда сохранять). No-op — not
        even counter work — when checkpointing is off or already finalized by
        run()'s final snapshot.
        """
        if self._checkpoint_path is None or self._checkpoint_stopped:
            return
        self._processed_count += 1
        if self._processed_count % self._checkpoint_every == 0:
            self._write_checkpoint()

    def _write_checkpoint(self) -> None:
        """Write a checkpoint, never letting an I/O failure kill the crawl.

        save_checkpoint() is atomic (tmp + os.replace), so a failed write
        leaves the previous valid file untouched. The failure itself is
        reported to stderr and swallowed: losing one checkpoint beats losing
        the crawl — and beats masking a CancelledError in run()'s finally.
        """
        if self._checkpoint_path is None:  # pragma: no cover - guarded by callers
            return
        try:
            save_checkpoint(self.export_snapshot(), self._checkpoint_path)
        except OSError as exc:
            print(f"politecrawl: checkpoint write failed: {exc}", file=sys.stderr)


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
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        metavar="PATH",
        help="write crawl checkpoints (periodic + final) to PATH; with --resume, read it too",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a crawl from the checkpoint file given by --checkpoint",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=100,
        metavar="N",
        help="write a periodic checkpoint every N processed URLs (default: 100)",
    )
    return parser


async def _run(
    seeds: list[str],
    *,
    max_depth: int,
    per_domain_concurrency: int,
    total_workers: int,
    user_agent: str,
    checkpoint_path: str | None = None,
    checkpoint_every: int = 100,
    snapshot: CrawlSnapshot | None = None,
) -> tuple[Crawler, float]:
    """Build the client and dependencies inside the event loop and crawl.

    With snapshot != None the crawler state is restored from it and run()
    starts in resume mode (the frontier comes from the snapshot, not from the
    seeds). When checkpointing is on, SIGTERM is wired to cancel this task so
    a supervisor kill leaves a final checkpoint via the same graceful path as
    Ctrl-C; on event loops without add_signal_handler (non-Unix) the wiring
    is silently skipped and only the KeyboardInterrupt path remains.
    """
    start = time.perf_counter()
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    sigterm_wired = False
    if checkpoint_path is not None and task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            sigterm_wired = True
        except (NotImplementedError, RuntimeError):
            pass
    try:
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
                checkpoint_path=checkpoint_path,
                checkpoint_every=checkpoint_every,
            )
            if snapshot is not None:
                crawler.apply_snapshot(snapshot)
            await crawler.run(seeds, total_workers, resume=snapshot is not None)
    finally:
        if sigterm_wired:
            loop.remove_signal_handler(signal.SIGTERM)
    elapsed = time.perf_counter() - start
    return crawler, elapsed


def _format_report(stats: CrawlStats, elapsed: float) -> str:
    lines = [
        "politecrawl report",
        f"  visited:        {stats.visited}",
        f"  skipped_dedup:  {stats.skipped_dedup}",
        f"  skipped_robots: {stats.skipped_robots}",
        f"  errors:         {stats.errors}",
        f"  sitemap_urls:   {stats.sitemap_urls}",
        f"  elapsed:        {elapsed:.3f}s",
        "",
        "per host:",
    ]
    for host in sorted(stats.per_host):
        c = stats.per_host[host]
        lines.append(
            f"  {host}  visited={c['visited']} "
            f"skipped_dedup={c['skipped_dedup']} "
            f"skipped_robots={c['skipped_robots']} errors={c['errors']} "
            f"sitemap_urls={c['sitemap_urls']}"
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


def _validate_checkpoint_args(args: argparse.Namespace) -> str | None:
    """Return a user-facing error message for invalid checkpoint flags, or None.

    Fail-fast before the crawl, like _validate_export_paths: --resume without
    --checkpoint has no file to resume from, and a non-positive period would
    make the periodic-trigger modulo meaningless.
    """
    if args.resume and args.checkpoint is None:
        return "--resume requires --checkpoint PATH (the checkpoint file to resume from)"
    if args.checkpoint_every < 1:
        return "--checkpoint-every must be a positive number of processed URLs"
    return None


def _load_resume_snapshot(args: argparse.Namespace) -> CrawlSnapshot:
    """Load and cross-check the checkpoint for --resume, before the crawl.

    Raises CheckpointFormatError for every failure mode — missing file,
    corrupt/incompatible content, seeds/max-depth mismatch — so main() has a
    single fail-fast error path. A mismatch is an error by design: another
    max-depth would yield inconsistent depths, other seeds a different graph;
    silently continuing would be worse than refusing (plan §CLI-интерфейс).
    """
    try:
        snapshot = load_checkpoint(args.checkpoint)
    except FileNotFoundError:
        raise CheckpointFormatError(
            f"cannot resume: checkpoint file {args.checkpoint!r} does not exist"
        ) from None
    if snapshot.seeds != args.seeds:
        raise CheckpointFormatError(
            "cannot resume: seeds differ from the checkpointed crawl "
            f"(checkpoint: {snapshot.seeds}, current: {args.seeds}); "
            "resuming with different seeds would crawl a different graph"
        )
    if snapshot.max_depth != args.max_depth:
        raise CheckpointFormatError(
            "cannot resume: --max-depth differs from the checkpointed crawl "
            f"(checkpoint: {snapshot.max_depth}, current: {args.max_depth}); "
            "resuming with another depth would yield inconsistent results"
        )
    return snapshot


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
    checkpoint_error = _validate_checkpoint_args(args)
    if checkpoint_error is not None:
        print(f"politecrawl: {checkpoint_error}", file=sys.stderr)
        return 2
    snapshot: CrawlSnapshot | None = None
    if args.resume:
        try:
            snapshot = _load_resume_snapshot(args)
        except CheckpointFormatError as exc:
            print(f"politecrawl: {exc}", file=sys.stderr)
            return 2
    try:
        crawler, elapsed = asyncio.run(
            _run(
                args.seeds,
                max_depth=args.max_depth,
                per_domain_concurrency=args.per_domain_concurrency,
                total_workers=args.total_workers,
                user_agent=args.user_agent,
                checkpoint_path=args.checkpoint,
                checkpoint_every=args.checkpoint_every,
                snapshot=snapshot,
            )
        )
    except asyncio.CancelledError:  # pragma: no cover - SIGTERM path (real signal)
        message = "politecrawl: crawl interrupted"
        if args.checkpoint is not None:
            message += f"; checkpoint saved to {args.checkpoint}"
        print(message, file=sys.stderr)
        return 1
    print(_format_report(crawler.stats, elapsed))
    try:
        _run_exports(crawler, args)
    except ExportFormatError as exc:  # pragma: no cover - guarded by validation
        print(f"politecrawl: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
