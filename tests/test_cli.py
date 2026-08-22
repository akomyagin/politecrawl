"""Тесты Этапа 5: CLI — сборка Crawler, ограничение глубины, отчёт."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
import respx

from politecrawl.checkpoint import CrawlSnapshot, load_checkpoint, save_checkpoint
from politecrawl.cli import Crawler, CrawlStats, _build_parser, _format_report, _run, main
from politecrawl.ratelimit import PerDomainLimiter

UA = "politecrawl/0.0"
HOST = "site.test"
BASE = f"https://{HOST}"


def _mock_robots_404() -> respx.Route:
    return respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))


def _mock_page(path: str, html: str = "<html></html>") -> respx.Route:
    return respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(200, html=html))


def _mock_site_graph() -> dict[str, respx.Route]:
    """Mock the linked page graph on one host (see plan, §Тесты).

    / (0) -> /a, /b; /a (1) -> /deep, / (cycle); /b (1) -> /a (dup);
    /deep (2) -> /toodeep; /toodeep (3) must not be fetched at max_depth=2.
    """
    _mock_robots_404()
    return {
        "/": _mock_page("/", "<a href='/a'>a</a><a href='/b'>b</a>"),
        "/a": _mock_page("/a", "<a href='/deep'>d</a><a href='/'>root</a>"),
        "/b": _mock_page("/b", "<a href='/a'>a</a>"),
        "/deep": _mock_page("/deep", "<a href='/toodeep'>t</a>"),
        "/toodeep": _mock_page("/toodeep"),
    }


async def _run_crawler(max_depth: int = 2) -> Crawler:
    crawler, _elapsed = await _run(
        [f"{BASE}/"],
        max_depth=max_depth,
        per_domain_concurrency=2,
        total_workers=4,
        user_agent=UA,
    )
    return crawler


async def _run_default(max_depth: int = 2) -> CrawlStats:
    return (await _run_crawler(max_depth)).stats


# --- argparse ---------------------------------------------------------------


def test_argparse_defaults() -> None:
    args = _build_parser().parse_args(["http://x"])
    assert args.seeds == ["http://x"]
    assert args.max_depth == 2
    assert args.per_domain_concurrency == 2
    assert args.total_workers == 8
    assert args.user_agent == "politecrawl/0.0"


def test_argparse_overrides() -> None:
    args = _build_parser().parse_args(
        [
            "http://x",
            "http://y",
            "--max-depth",
            "5",
            "--per-domain-concurrency",
            "3",
            "--total-workers",
            "4",
            "--user-agent",
            "bot/1",
        ]
    )
    assert args.seeds == ["http://x", "http://y"]
    assert args.max_depth == 5
    assert args.per_domain_concurrency == 3
    assert args.total_workers == 4
    assert args.user_agent == "bot/1"


# --- crawl behaviour --------------------------------------------------------


@respx.mock
async def test_crawl_visits_reachable_graph() -> None:
    _mock_site_graph()
    stats = await _run_default(max_depth=2)
    # Unique pages reachable within depth 2: /, /a, /b, /deep.
    assert stats.visited == 4
    assert stats.errors == 0


@respx.mock
async def test_max_depth_cuts_frontier() -> None:
    routes = _mock_site_graph()
    await _run_default(max_depth=2)
    assert routes["/toodeep"].call_count == 0  # beyond the depth limit
    assert routes["/deep"].call_count == 1  # exactly at the limit


@respx.mock
async def test_max_depth_zero_only_seeds() -> None:
    routes = _mock_site_graph()
    stats = await _run_default(max_depth=0)
    assert stats.visited == 1  # only the seed
    assert routes["/a"].call_count == 0
    assert routes["/b"].call_count == 0


@respx.mock
async def test_dedup_counted_not_visited() -> None:
    routes = _mock_site_graph()
    stats = await _run_default(max_depth=2)
    for path in ("/", "/a", "/b", "/deep"):
        assert routes[path].call_count == 1, path
    assert stats.skipped_dedup > 0
    assert stats.visited == 4  # duplicates counted in skipped_dedup, not here


@respx.mock
async def test_robots_disallow_counted() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    _mock_page("/", "<a href='/private'>p</a><a href='/public'>ok</a>")
    private_route = _mock_page("/private")
    _mock_page("/public")
    stats = await _run_default(max_depth=1)
    assert stats.skipped_robots >= 1
    assert stats.visited == 2  # / and /public; /private is not visited
    assert private_route.call_count == 0  # robots cut BEFORE the fetch


@respx.mock
async def test_network_error_counted_as_error() -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/broken'>b</a>")
    respx.get(f"{BASE}/broken").mock(side_effect=httpx.ConnectError("refused"))
    stats = await _run_default(max_depth=1)
    assert stats.errors >= 1
    assert stats.visited == 1  # only /; /broken is not visited


@respx.mock
async def test_malformed_href_does_not_crash_worker() -> None:
    # extract_links() raises ValueError on some malformed hrefs (e.g. a bad
    # IPv6 literal) found on real crawled pages. A worker must survive that:
    # the crawl must count it as an error, not silently lose the worker task
    # (which, with several queued items still pending, could leave queue.join()
    # waiting forever on a task_done() that never comes).
    _mock_robots_404()
    _mock_page("/", "<a href='http://[bad'>broken</a><a href='/ok'>ok</a>")
    _mock_page("/ok")
    crawler, _elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/"],
            max_depth=1,
            per_domain_concurrency=2,
            total_workers=1,
            user_agent=UA,
        ),
        timeout=5.0,
    )
    stats = crawler.stats
    assert stats.visited == 1  # only the seed; the malformed href never enqueues /ok
    assert stats.errors >= 1


@respx.mock
async def test_join_terminates_no_hang() -> None:
    _mock_site_graph()
    crawler, _elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/"],
            max_depth=2,
            per_domain_concurrency=2,
            total_workers=4,
            user_agent=UA,
        ),
        timeout=5.0,
    )
    assert crawler.stats.visited == 4


@respx.mock
async def test_per_host_breakdown_in_stats() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    # / links to /a twice (dup), /private (disallowed) and itself (cycle).
    _mock_page(
        "/",
        "<a href='/a'>1</a><a href='/a'>2</a><a href='/private'>p</a><a href='/'>me</a>",
    )
    _mock_page("/a")
    _mock_page("/private")
    stats = await _run_default(max_depth=1)
    breakdown = stats.per_host[HOST]
    assert breakdown["visited"] == stats.visited
    assert breakdown["skipped_dedup"] == stats.skipped_dedup
    assert breakdown["skipped_robots"] == stats.skipped_robots
    assert breakdown["errors"] == stats.errors
    assert stats.visited == 2  # / and /a
    assert stats.skipped_dedup >= 2  # second /a link + self-link
    assert stats.skipped_robots == 1


@respx.mock
async def test_seed_disallowed_by_robots() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    seed_route = _mock_page("/")
    stats = await asyncio.wait_for(_run_default(max_depth=2), timeout=5.0)
    assert stats.visited == 0
    assert stats.skipped_robots == 1
    assert seed_route.call_count == 0


@respx.mock
def test_report_contains_counters(capsys: pytest.CaptureFixture[str]) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    rc = main([f"{BASE}/", "--max-depth", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    for token in (
        "visited",
        "skipped_dedup",
        "skipped_robots",
        "errors",
        "sitemap_urls",
        "elapsed",
    ):
        assert token in out
    assert HOST in out


@respx.mock
async def test_empty_links_page() -> None:
    _mock_robots_404()
    _mock_page("/", "<html></html>")
    stats = await asyncio.wait_for(_run_default(max_depth=2), timeout=5.0)
    assert stats.visited == 1
    assert stats.skipped_dedup == 0
    assert stats.errors == 0


# --- Этап 6: crawl-delay из robots.txt + backoff ----------------------------


@respx.mock
async def test_crawl_delay_from_robots_applied() -> None:
    # stdlib RobotFileParser парсит только ЦЕЛЫЕ Crawl-delay (дробные -> None),
    # поэтому минимальный выразимый через robots.txt интервал — 1 секунда
    # (в плане предлагалось 0.02-0.05, но stdlib это отбрасывает). Граф из
    # двух страниц: тест платит ровно за один интервал, ~1s.
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 1\n")
    )
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    crawler, elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/"],
            max_depth=1,
            per_domain_concurrency=2,
            total_workers=4,
            user_agent=UA,
        ),
        timeout=5.0,
    )
    assert crawler.stats.visited == 2
    assert crawler.stats.errors == 0
    # первый запрос стартует сразу, второй выжидает crawl-delay целиком
    # (допуск на дрожание планировщика event loop)
    assert elapsed >= 0.9


@respx.mock
async def test_5xx_response_triggers_backoff_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/boom'>b</a>")
    respx.get(f"{BASE}/boom").mock(return_value=httpx.Response(503))

    calls: list[tuple[str, int | None]] = []
    original = PerDomainLimiter.record_response

    def spy(self: PerDomainLimiter, domain: str, status: int | None) -> None:
        calls.append((domain, status))
        original(self, domain, status)

    monkeypatch.setattr(PerDomainLimiter, "record_response", spy)
    stats = await _run_default(max_depth=1)

    # record_response вызван на каждый fetch с фактическим статусом
    assert (HOST, 200) in calls
    assert (HOST, 503) in calls
    # 5xx — не транспортная ошибка: страница скачана и посчитана как visited
    assert stats.errors == 0
    assert stats.visited == 2


# --- Этап 7: сбор edges/pages и флаги экспорта ------------------------------


@respx.mock
async def test_crawler_collects_edges() -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a><a href='/b'>b</a>")
    _mock_page("/a", "<a href='/deep'>d</a>")
    _mock_page("/b")
    deep_route = _mock_page("/deep")
    crawler = await _run_crawler(max_depth=1)
    assert (f"{BASE}/", f"{BASE}/a") in crawler.edges
    assert (f"{BASE}/", f"{BASE}/b") in crawler.edges
    # The edge to /deep is recorded even though /deep is beyond max_depth=1:
    # edges form the LINK graph, not the crawl graph.
    assert (f"{BASE}/a", f"{BASE}/deep") in crawler.edges
    assert deep_route.call_count == 0  # ...but the target was never fetched


@respx.mock
async def test_crawler_collects_pages_with_title() -> None:
    _mock_robots_404()
    _mock_page("/", "<html><head><title>Root</title></head><a href='/a'>a</a></html>")
    _mock_page("/a", "<html><head><title>Page A</title></head></html>")
    crawler = await _run_crawler(max_depth=1)
    assert len(crawler.pages) == crawler.stats.visited == 2
    by_url = {p["url"]: p for p in crawler.pages}
    root = by_url[f"{BASE}/"]
    assert root["status"] == 200
    assert isinstance(root["content_type"], str)
    assert root["content_type"].startswith("text/html")
    assert root["title"] == "Root"
    assert by_url[f"{BASE}/a"]["title"] == "Page A"


@respx.mock
async def test_edges_recorded_regardless_of_dedup() -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a", "<a href='/'>back</a>")  # cycle back to the seed
    crawler = await _run_crawler(max_depth=2)
    assert crawler.stats.visited == 2  # / fetched once (dedup)
    # The edge to the already-seen URL is still in the link graph.
    assert (f"{BASE}/a", f"{BASE}/") in crawler.edges


@respx.mock
async def test_edges_recorded_to_robots_disallowed_target() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    )
    _mock_page("/", "<a href='/private'>p</a>")
    private_route = _mock_page("/private")
    crawler = await _run_crawler(max_depth=1)
    # The link graph records the edge even though the target is disallowed
    # by robots and never fetched.
    assert (f"{BASE}/", f"{BASE}/private") in crawler.edges
    assert crawler.stats.skipped_robots == 1
    assert private_route.call_count == 0


@respx.mock
def test_export_flags_write_files(tmp_path: Path) -> None:
    _mock_robots_404()
    _mock_page("/", "<html><head><title>Root</title></head><a href='/a'>a</a></html>")
    _mock_page("/a")
    edges_path = tmp_path / "e.jsonl"
    pages_path = tmp_path / "p.csv"
    sitemap_path = tmp_path / "s.xml"
    rc = main(
        [
            f"{BASE}/",
            "--max-depth",
            "1",
            "--export-edges",
            str(edges_path),
            "--export-pages",
            str(pages_path),
            "--export-sitemap",
            str(sitemap_path),
        ]
    )
    assert rc == 0
    edges = [json.loads(line) for line in edges_path.read_text(encoding="utf-8").splitlines()]
    assert {"source": f"{BASE}/", "target": f"{BASE}/a"} in edges
    pages_text = pages_path.read_text(encoding="utf-8")
    assert pages_text.startswith("url,status,content_type,title")
    assert f"{BASE}/a" in pages_text
    sitemap_text = sitemap_path.read_text(encoding="utf-8")
    assert "<urlset" in sitemap_text
    assert f"<loc>{BASE}/</loc>" in sitemap_text


@respx.mock
def test_export_bad_extension_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_robots_404()
    _mock_page("/")
    rc = main([f"{BASE}/", "--export-edges", str(tmp_path / "e.txt")])
    captured = capsys.readouterr()
    assert rc == 2
    assert "e.txt" in captured.err
    assert "unsupported" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


@respx.mock
def test_export_bad_extension_before_crawl(tmp_path: Path) -> None:
    # Extension validation runs BEFORE the crawl: no network traffic at all.
    _mock_robots_404()
    seed_route = _mock_page("/")
    rc = main([f"{BASE}/", "--export-pages", str(tmp_path / "p.bogus")])
    assert rc == 2
    assert seed_route.call_count == 0


@respx.mock
def test_no_export_flags_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    rc = main([f"{BASE}/", "--max-depth", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert list(tmp_path.iterdir()) == []  # no export files created
    assert "visited:        2" in out  # crawl behaviour unchanged


# --- Этап 8: Sitemap: из robots.txt -----------------------------------------

SITEMAP_XMLNS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _mock_robots_with_sitemap(sitemap_path: str = "/sitemap.xml", rules: str = "") -> respx.Route:
    return respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text=f"User-agent: *\n{rules}Sitemap: {BASE}{sitemap_path}\n",
        )
    )


def _urlset(*urls: str) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f"<urlset {SITEMAP_XMLNS}>{locs}</urlset>"


def _sitemapindex(*urls: str) -> str:
    locs = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f"<sitemapindex {SITEMAP_XMLNS}>{locs}</sitemapindex>"


def _mock_sitemap(path: str, xml: str) -> respx.Route:
    return respx.get(f"{BASE}{path}").mock(
        return_value=httpx.Response(200, text=xml, headers={"content-type": "application/xml"})
    )


@respx.mock
async def test_sitemap_urls_discovered_and_enqueued() -> None:
    # /orphan1 and /orphan2 are declared only in the sitemap: no <a href>
    # anywhere points at them.
    _mock_robots_with_sitemap()
    _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/orphan1", f"{BASE}/orphan2"))
    _mock_page("/", "<html></html>")
    orphan1 = _mock_page("/orphan1")
    orphan2 = _mock_page("/orphan2")
    stats = await _run_default(max_depth=1)
    assert stats.visited == 3  # seed + both sitemap-only pages
    assert stats.sitemap_urls == 2
    assert orphan1.call_count == 1
    assert orphan2.call_count == 1


@respx.mock
async def test_sitemap_fetched_once_per_host() -> None:
    robots_route = _mock_robots_with_sitemap()
    sitemap_route = _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/p"))
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    _mock_page("/p")
    crawler, _elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/", f"{BASE}/a"],  # two depth-0 seeds on the SAME host
            max_depth=1,
            per_domain_concurrency=2,
            total_workers=4,
            user_agent=UA,
        ),
        timeout=5.0,
    )
    assert crawler.stats.visited == 3  # /, /a, /p
    assert robots_route.call_count == 1  # RobotsCache: one download per host
    assert sitemap_route.call_count == 1  # discovery ran once per host


@respx.mock
async def test_sitemap_index_one_level() -> None:
    _mock_robots_with_sitemap("/sitemap-index.xml")
    _mock_sitemap(
        "/sitemap-index.xml",
        _sitemapindex(f"{BASE}/sm-a.xml", f"{BASE}/sm-b.xml"),
    )
    _mock_sitemap("/sm-a.xml", _urlset(f"{BASE}/from-a"))
    _mock_sitemap("/sm-b.xml", _urlset(f"{BASE}/from-b"))
    _mock_page("/", "<html></html>")
    from_a = _mock_page("/from-a")
    from_b = _mock_page("/from-b")
    stats = await _run_default(max_depth=1)
    assert from_a.call_count == 1  # pages from child sitemaps are crawled
    assert from_b.call_count == 1
    assert stats.visited == 3
    assert stats.sitemap_urls == 2


@respx.mock
async def test_sitemap_index_two_levels_not_expanded() -> None:
    # index -> child index -> grandchild sitemap: only ONE level of nesting is
    # followed, so the grandchild sitemap is never fetched and its page is
    # never enqueued.
    _mock_robots_with_sitemap("/sitemap-index.xml")
    _mock_sitemap(
        "/sitemap-index.xml",
        _sitemapindex(f"{BASE}/child-index.xml"),
    )
    grandchild_route = _mock_sitemap(
        "/child-index.xml",
        _sitemapindex(f"{BASE}/grandchild.xml"),
    )
    grandchild_sitemap_route = _mock_sitemap("/grandchild.xml", _urlset(f"{BASE}/from-grandchild"))
    _mock_page("/", "<html></html>")
    from_grandchild = _mock_page("/from-grandchild")
    stats = await _run_default(max_depth=1)
    assert grandchild_route.call_count == 1  # the child index itself IS fetched
    assert grandchild_sitemap_route.call_count == 0  # ...but not expanded further
    assert from_grandchild.call_count == 0
    assert stats.visited == 1  # only the seed
    assert stats.sitemap_urls == 0


@respx.mock
async def test_sitemap_urls_go_through_robots() -> None:
    _mock_robots_with_sitemap(rules="Disallow: /private\n")
    _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/private/page"))
    _mock_page("/", "<html></html>")
    private_route = _mock_page("/private/page")
    stats = await _run_default(max_depth=1)
    # Discovered in the sitemap and enqueued...
    assert stats.sitemap_urls == 1
    # ...but dropped at the fence by the NORMAL robots check, never fetched.
    assert stats.skipped_robots == 1
    assert private_route.call_count == 0
    assert stats.visited == 1  # only the seed


@respx.mock
async def test_sitemap_absent_no_effect() -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    sitemap_route = _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/never"))
    stats = await _run_default(max_depth=1)
    assert stats.visited == 2  # crawl unchanged: / and /a
    assert stats.sitemap_urls == 0
    assert sitemap_route.call_count == 0  # no sitemap request at all


@respx.mock
async def test_malformed_sitemap_does_not_crash() -> None:
    _mock_robots_with_sitemap()
    _mock_sitemap("/sitemap.xml", "this is not xml <<<")
    _mock_page("/", "<html></html>")
    stats = await asyncio.wait_for(_run_default(max_depth=1), timeout=5.0)
    assert stats.visited == 1  # the seed is still crawled
    assert stats.sitemap_urls == 0
    assert stats.errors == 0  # a bad sitemap is not a crawl error


# --- report formatting ------------------------------------------------------


def test_format_report_lists_hosts_sorted() -> None:
    stats = CrawlStats()
    stats.record_visited("b.test")
    stats.record_visited("a.test")
    stats.record_error("b.test")
    report = _format_report(stats, 0.5)
    assert "visited:        2" in report
    assert "errors:         1" in report
    assert "elapsed:        0.500s" in report
    assert report.index("a.test") < report.index("b.test")


# --- Этап 9: персистентность фронтира (чекпоинт + resume) -------------------


def _totals(**over: int) -> dict[str, int]:
    totals = {
        "visited": 0,
        "skipped_dedup": 0,
        "skipped_robots": 0,
        "errors": 0,
        "sitemap_urls": 0,
    }
    totals.update(over)
    return totals


def _artificial_snapshot(
    *,
    frontier: list[tuple[str, int]],
    seen: set[str],
    seeds: list[str] | None = None,
    max_depth: int = 2,
    per_host: dict[str, Counter[str]] | None = None,
    sitemap_hosts: set[str] | None = None,
    fetched_sitemaps: set[str] | None = None,
    **totals_over: int,
) -> CrawlSnapshot:
    """Искусственный снимок частично обойдённого графа (план §Тесты)."""
    return CrawlSnapshot(
        seeds=seeds if seeds is not None else [f"{BASE}/"],
        max_depth=max_depth,
        user_agent=UA,
        frontier=frontier,
        seen=seen,
        stats_totals=_totals(**totals_over),
        per_host=per_host if per_host is not None else {},
        edges=[],
        pages=[],
        sitemap_hosts=sitemap_hosts if sitemap_hosts is not None else set(),
        fetched_sitemaps=fetched_sitemaps if fetched_sitemaps is not None else set(),
    )


async def _run_with_checkpoint(
    checkpoint_path: str,
    *,
    max_depth: int = 1,
    snapshot: CrawlSnapshot | None = None,
) -> Crawler:
    crawler, _elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/"],
            max_depth=max_depth,
            per_domain_concurrency=2,
            total_workers=2,
            user_agent=UA,
            checkpoint_path=checkpoint_path,
            snapshot=snapshot,
        ),
        timeout=5.0,
    )
    return crawler


async def _interrupt_crawl(checkpoint_path: str, started: asyncio.Event, max_depth: int) -> None:
    """Запустить обход и детерминированно отменить его, когда started взведён.

    Модель Ctrl-C/SIGTERM: отмена доходит до Crawler.run() как CancelledError,
    финальный чекпоинт пишется в его finally до гашения воркеров.
    """
    task = asyncio.ensure_future(
        _run(
            [f"{BASE}/"],
            max_depth=max_depth,
            per_domain_concurrency=2,
            total_workers=2,
            user_agent=UA,
            checkpoint_path=checkpoint_path,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _hang_then_serve(started: asyncio.Event, html: str = "<html></html>") -> respx.Route:
    """Мок /a: первый запрос виснет навсегда (взведя started), последующие — 200.

    Даёт детерминированную точку прерывания: пока первый fetch висит, /a
    гарантированно in-flight; после resume повторный запрос уже отвечает.
    """
    calls = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()  # блокируется до отмены воркера
        return httpx.Response(200, html=html)

    return respx.get(f"{BASE}/a").mock(side_effect=side_effect)


def test_argparse_checkpoint_flags() -> None:
    args = _build_parser().parse_args(["http://x"])
    assert args.checkpoint is None
    assert args.resume is False
    assert args.checkpoint_every == 100
    args = _build_parser().parse_args(
        ["http://x", "--checkpoint", "state.json", "--resume", "--checkpoint-every", "7"]
    )
    assert args.checkpoint == "state.json"
    assert args.resume is True
    assert args.checkpoint_every == 7


def test_resume_requires_checkpoint(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["http://x", "--resume"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--resume" in err
    assert "--checkpoint" in err
    assert "Traceback" not in err


@respx.mock
def test_resume_missing_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mock_robots_404()
    seed_route = _mock_page("/")
    rc = main([f"{BASE}/", "--checkpoint", str(tmp_path / "missing.json"), "--resume"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "does not exist" in err
    assert seed_route.call_count == 0  # обход не запускается


@respx.mock
def test_resume_corrupt_file_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cp = tmp_path / "state.json"
    cp.write_text("{broken json", encoding="utf-8")
    _mock_robots_404()
    seed_route = _mock_page("/")
    rc = main([f"{BASE}/", "--checkpoint", str(cp), "--resume"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "JSON" in err
    assert cp.read_text(encoding="utf-8") == "{broken json"  # файл не перезаписан
    assert seed_route.call_count == 0


@respx.mock
def test_checkpoint_written_periodically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a><a href='/b'>b</a>")
    _mock_page("/a")
    _mock_page("/b")
    cp = tmp_path / "state.json"
    save_paths: list[str] = []

    def spy(snapshot: CrawlSnapshot, path: str) -> None:
        save_paths.append(path)
        save_checkpoint(snapshot, path)

    monkeypatch.setattr("politecrawl.cli.save_checkpoint", spy)
    rc = main([f"{BASE}/", "--max-depth", "1", "--checkpoint", str(cp), "--checkpoint-every", "1"])
    assert rc == 0
    # --checkpoint-every 1 на графе из 3 страниц: были и периодические записи,
    # а не только финальная из run()'s finally.
    assert len(save_paths) >= 2
    assert set(save_paths) == {str(cp)}
    final = load_checkpoint(str(cp))  # файл существует и валиден
    assert final.frontier == []  # финальный снимок: фронтир дренирован
    assert final.stats_totals["visited"] == 3
    assert final.seeds == [f"{BASE}/"]


@respx.mock
async def test_checkpoint_written_on_interrupt(tmp_path: Path) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a><a href='/b'>b</a>")
    _mock_page("/b")
    started = asyncio.Event()
    _hang_then_serve(started)
    cp = tmp_path / "state.json"
    await _interrupt_crawl(str(cp), started, max_depth=1)

    snap = load_checkpoint(str(cp))
    # in-flight /a (вынут из очереди, fetch завис) обязан попасть во фронтир
    assert (f"{BASE}/a", 1) in snap.frontier
    # и исключён из seen: _process уже отметил его в дедупе, но не завершил;
    # останься он в seen — resume срезал бы его на заборе дедупа навсегда
    assert f"{BASE}/a" not in snap.seen
    assert f"{BASE}/" in snap.seen  # сам seed обработан и остаётся виденным


@respx.mock
def test_resume_continues_from_frontier(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mock_robots_404()
    root_route = _mock_page("/", "<a href='/a'>a</a>")
    a_route = _mock_page("/a")
    cp = tmp_path / "state.json"
    snapshot = _artificial_snapshot(
        frontier=[(f"{BASE}/a", 1)],
        seen={f"{BASE}/"},
        per_host={HOST: Counter({"visited": 1})},
        visited=1,
    )
    save_checkpoint(snapshot, str(cp))
    rc = main([f"{BASE}/", "--checkpoint", str(cp), "--resume"])
    out = capsys.readouterr().out
    assert rc == 0
    assert root_route.call_count == 0  # уже виденное не перекачивается
    assert a_route.call_count == 1  # хвост фронтира добран
    # seeds на resume НЕ enqueue-ятся заново: иначе seed прошёл бы через дедуп
    # и в отчёте появился бы skipped_dedup >= 1
    assert "skipped_dedup:  0" in out
    assert "visited:        2" in out  # 1 из снимка + добранный /a


@respx.mock
async def test_resume_final_state_matches_uninterrupted(tmp_path: Path) -> None:
    """Ключевой: прерванный+resume обход == непрерывный по stats/edges/pages."""
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/b")
    started = asyncio.Event()
    # 1-й запрос /a (прерванный прогон) виснет; 2-й (resume) и 3-й
    # (непрерывный прогон) отдают страницу со ссылкой дальше.
    _hang_then_serve(started, html="<a href='/b'>b</a>")
    cp = tmp_path / "state.json"

    # (б) прерваться, пока /a in-flight, затем возобновиться из чекпоинта
    await _interrupt_crawl(str(cp), started, max_depth=2)
    resumed = await _run_with_checkpoint(str(cp), max_depth=2, snapshot=load_checkpoint(str(cp)))

    # (а) непрерывный прогон того же графа
    uninterrupted, _elapsed = await asyncio.wait_for(
        _run(
            [f"{BASE}/"],
            max_depth=2,
            per_domain_concurrency=2,
            total_workers=2,
            user_agent=UA,
        ),
        timeout=5.0,
    )

    assert resumed.stats.totals() == uninterrupted.stats.totals()
    assert resumed.stats.per_host == uninterrupted.stats.per_host
    assert set(resumed.edges) == set(uninterrupted.edges)
    key = "url"
    assert sorted(resumed.pages, key=lambda p: str(p[key])) == sorted(
        uninterrupted.pages, key=lambda p: str(p[key])
    )
    assert uninterrupted.stats.visited == 3  # /, /a, /b — прогресс не потерян и не удвоен


@respx.mock
def test_resume_seed_mismatch_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cp = tmp_path / "state.json"
    save_checkpoint(_artificial_snapshot(frontier=[], seen=set()), str(cp))
    respx.get("https://other.test/robots.txt").mock(return_value=httpx.Response(404))
    other_route = respx.get("https://other.test/").mock(return_value=httpx.Response(200))
    rc = main(["https://other.test/", "--checkpoint", str(cp), "--resume"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "seeds" in err
    assert other_route.call_count == 0  # обход не идёт


@respx.mock
def test_resume_max_depth_mismatch_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cp = tmp_path / "state.json"
    save_checkpoint(_artificial_snapshot(frontier=[], seen=set(), max_depth=2), str(cp))
    _mock_robots_404()
    seed_route = _mock_page("/")
    rc = main([f"{BASE}/", "--max-depth", "3", "--checkpoint", str(cp), "--resume"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "max-depth" in err
    assert seed_route.call_count == 0


@respx.mock
def test_resume_preserves_sitemap_state(tmp_path: Path) -> None:
    # Хост site.test в первом прогоне полностью обойдён, его sitemap-обнаружение
    # зафиксировано в снимке; во фронтире остался только другой хост. На resume
    # ни robots.txt, ни sitemap хоста site.test не запрашиваются повторно.
    robots_route = _mock_robots_with_sitemap()
    sitemap_route = _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/from-sitemap"))
    respx.get("https://other.test/robots.txt").mock(return_value=httpx.Response(404))
    tail_route = respx.get("https://other.test/tail").mock(
        return_value=httpx.Response(200, html="<html></html>")
    )
    cp = tmp_path / "state.json"
    snapshot = _artificial_snapshot(
        frontier=[("https://other.test/tail", 1)],
        seen={f"{BASE}/", f"{BASE}/from-sitemap"},
        sitemap_hosts={HOST},
        fetched_sitemaps={f"{BASE}/sitemap.xml"},
        visited=2,
        sitemap_urls=1,
    )
    save_checkpoint(snapshot, str(cp))
    rc = main([f"{BASE}/", "--checkpoint", str(cp), "--resume"])
    assert rc == 0
    assert robots_route.call_count == 0  # robots хоста не перекачивается
    assert sitemap_route.call_count == 0  # обнаружение не переигрывается
    assert tail_route.call_count == 1  # хвост фронтира добран
    final = load_checkpoint(str(cp))
    assert final.stats_totals["sitemap_urls"] == 1  # не удвоился


@respx.mock
def test_resume_skips_sitemap_discovery_for_seen_host(tmp_path: Path) -> None:
    # Во фронтире остался depth-0 URL хоста, чьё обнаружение уже состоялось:
    # robots.txt перечитывается ради allowed() (RobotsCache намеренно не
    # персистится), но sitemap повторно НЕ скачивается и sitemap_urls не растёт.
    robots_route = _mock_robots_with_sitemap()
    sitemap_route = _mock_sitemap("/sitemap.xml", _urlset(f"{BASE}/from-sitemap"))
    from_sitemap_route = _mock_page("/from-sitemap")
    cp = tmp_path / "state.json"
    snapshot = _artificial_snapshot(
        frontier=[(f"{BASE}/from-sitemap", 0)],
        seen={f"{BASE}/"},
        sitemap_hosts={HOST},
        fetched_sitemaps={f"{BASE}/sitemap.xml"},
        visited=1,
        sitemap_urls=1,
    )
    save_checkpoint(snapshot, str(cp))
    rc = main([f"{BASE}/", "--checkpoint", str(cp), "--resume"])
    assert rc == 0
    assert sitemap_route.call_count == 0  # discovery не переигрывается
    assert from_sitemap_route.call_count == 1
    assert robots_route.call_count == 1  # только для allowed(), не для discovery
    final = load_checkpoint(str(cp))
    assert final.stats_totals["sitemap_urls"] == 1


@respx.mock
def test_no_checkpoint_flag_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    save_calls: list[str] = []
    monkeypatch.setattr(
        "politecrawl.cli.save_checkpoint",
        lambda snapshot, path: save_calls.append(str(path)),
    )
    rc = main([f"{BASE}/", "--max-depth", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert save_calls == []  # save_checkpoint не вызывался вовсе
    assert list(tmp_path.iterdir()) == []  # и файлов не появилось
    assert "visited:        2" in out  # поведение обхода как на Этапе 8


@respx.mock
def test_resume_exports_include_first_run(tmp_path: Path) -> None:
    _mock_robots_404()
    _mock_page("/", "<html><head><title>Root</title></head><a href='/a'>a</a></html>")
    started = asyncio.Event()
    _hang_then_serve(started, html="<html><head><title>Page A</title></head></html>")
    cp = tmp_path / "state.json"

    # Прерваться, пока /a in-flight: в чекпоинте pages/edges только 1-го прогона.
    asyncio.run(_interrupt_crawl(str(cp), started, 1))

    edges_path = tmp_path / "edges.jsonl"
    pages_path = tmp_path / "pages.jsonl"
    rc = main(
        [
            f"{BASE}/",
            "--max-depth",
            "1",
            "--checkpoint",
            str(cp),
            "--resume",
            "--export-edges",
            str(edges_path),
            "--export-pages",
            str(pages_path),
        ]
    )
    assert rc == 0
    pages = [json.loads(line) for line in pages_path.read_text(encoding="utf-8").splitlines()]
    assert {p["url"] for p in pages} == {f"{BASE}/", f"{BASE}/a"}  # оба прогона
    assert {p["title"] for p in pages} == {"Root", "Page A"}
    edges = [json.loads(line) for line in edges_path.read_text(encoding="utf-8").splitlines()]
    # ребро найдено ПЕРВЫМ прогоном и пережило чекпоинт
    assert {"source": f"{BASE}/", "target": f"{BASE}/a"} in edges
