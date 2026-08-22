"""Тесты Этапа 5: CLI — сборка Crawler, ограничение глубины, отчёт."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

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
    for token in ("visited", "skipped_dedup", "skipped_robots", "errors", "elapsed"):
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
def test_no_export_flags_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_robots_404()
    _mock_page("/", "<a href='/a'>a</a>")
    _mock_page("/a")
    rc = main([f"{BASE}/", "--max-depth", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert list(tmp_path.iterdir()) == []  # no export files created
    assert "visited:        2" in out  # crawl behaviour unchanged


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
