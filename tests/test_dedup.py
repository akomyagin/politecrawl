"""Тесты Этапа 4: normalize() и UrlDedup — дедупликация URL."""

from __future__ import annotations

import pytest

from politecrawl.dedup import UrlDedup, normalize

# --- normalize() -------------------------------------------------------------

NORMALIZE_CASES = [
    pytest.param(
        "http://example.com/x#frag",
        "http://example.com/x",
        id="fragment-dropped",
    ),
    pytest.param(
        "http://example.com:80/x",
        "http://example.com/x",
        id="default-port-http-stripped",
    ),
    pytest.param(
        "https://example.com:443/x",
        "https://example.com/x",
        id="default-port-https-stripped",
    ),
    pytest.param(
        "http://example.com:8080/x",
        "http://example.com:8080/x",
        id="non-default-port-kept",
    ),
    pytest.param(
        "HTTP://Example.COM/x",
        "http://example.com/x",
        id="scheme-and-host-lowercased",
    ),
    pytest.param(
        "http://example.com/x?b=2&a=1",
        "http://example.com/x?a=1&b=2",
        id="query-params-sorted",
    ),
    pytest.param(
        "http://example.com",
        "http://example.com/",
        id="empty-path-becomes-slash",
    ),
    pytest.param(
        "http://example.com/x?a=&b=1",
        "http://example.com/x?a=&b=1",
        id="blank-query-value-kept",
    ),
    pytest.param(
        "http://example.com:443/x",
        "http://example.com:443/x",
        id="non-default-port-for-scheme-kept-https-port-on-http",
    ),
    pytest.param(
        "https://example.com:80/x",
        "https://example.com:80/x",
        id="non-default-port-for-scheme-kept-http-port-on-https",
    ),
    pytest.param(
        "http://example.com/x?a=2&a=1",
        "http://example.com/x?a=1&a=2",
        id="duplicate-query-keys-sorted-by-value",
    ),
]


@pytest.mark.parametrize("raw, expected", NORMALIZE_CASES)
def test_normalize_cases(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_combines_all_rules_at_once() -> None:
    raw = "HTTP://Example.com:80/x?b=2&a=1#frag"
    assert normalize(raw) == "http://example.com/x?a=1&b=2"


# --- UrlDedup ------------------------------------------------------------


def test_add_returns_true_then_false_for_same_url() -> None:
    dedup = UrlDedup()
    assert dedup.add("http://example.com/x") is True
    assert dedup.add("http://example.com/x") is False


def test_seen_without_add_is_false() -> None:
    dedup = UrlDedup()
    assert dedup.seen("http://example.com/x") is False


def test_seen_true_after_add() -> None:
    dedup = UrlDedup()
    dedup.add("http://example.com/x")
    assert dedup.seen("http://example.com/x") is True


def test_equivalent_urls_collapse_on_add() -> None:
    dedup = UrlDedup()
    first = "HTTP://Example.com:80/x?b=2&a=1#frag"
    second = "http://example.com/x?a=1&b=2"
    assert dedup.add(first) is True
    assert dedup.add(second) is False  # same normalized URL, already seen


def test_equivalent_urls_collapse_on_seen() -> None:
    dedup = UrlDedup()
    dedup.add("HTTP://Example.com:80/x?b=2&a=1#frag")
    assert dedup.seen("http://example.com/x?a=1&b=2") is True


def test_seen_does_not_mutate_state() -> None:
    dedup = UrlDedup()
    url = "http://example.com/x"
    assert dedup.seen(url) is False
    assert dedup.seen(url) is False  # calling seen() again must not have added it
    assert dedup.add(url) is True  # still unseen: seen() above did not insert


# --- Этап 9: снимок/загрузка _seen для чекпоинта -----------------------------


def test_dedup_snapshot_roundtrip() -> None:
    first = UrlDedup()
    first.add("HTTP://Example.com:80/x?b=2&a=1#frag")  # нормализуется при add()
    first.add("http://example.com/y")
    snapshot = first.snapshot_seen()

    second = UrlDedup()
    second.load_seen(snapshot)
    # ранее виденные (в т.ч. в эквивалентной записи) -> True, новый -> False
    assert second.seen("http://example.com/x?a=1&b=2") is True
    assert second.seen("http://example.com/y") is True
    assert second.seen("http://example.com/z") is False
    # снимок — копия: дальнейшие add() первого экземпляра его не меняют
    first.add("http://example.com/later")
    assert "http://example.com/later" not in snapshot


def test_dedup_load_seen_does_not_renormalize() -> None:
    dedup = UrlDedup()
    # Намеренно НЕнормализованная запись: load_seen() обязан положить её как
    # есть (снимки пишет сам add() уже нормализованными, повторная
    # нормализация не выполняется) — поэтому нормализованный эквивалент
    # НЕ считается виденным.
    dedup.load_seen({"HTTP://Example.com/x"})
    assert dedup.seen("http://example.com/x") is False
