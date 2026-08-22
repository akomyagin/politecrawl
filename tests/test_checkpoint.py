"""Тесты Этапа 9: checkpoint.py — формат снимка, атомарная запись, валидация."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from politecrawl.checkpoint import (
    SCHEMA_VERSION,
    CheckpointFormatError,
    CrawlSnapshot,
    load_checkpoint,
    save_checkpoint,
)


def _snapshot() -> CrawlSnapshot:
    """Полный снимок со всеми полями плана (§Что персистится)."""
    return CrawlSnapshot(
        seeds=["https://site.test/"],
        max_depth=2,
        user_agent="politecrawl/0.0",
        frontier=[("https://site.test/a", 1), ("https://site.test/b", 2)],
        seen={"https://site.test/", "https://site.test/a"},
        stats_totals={
            "visited": 1,
            "skipped_dedup": 2,
            "skipped_robots": 3,
            "errors": 4,
            "sitemap_urls": 5,
        },
        per_host={"site.test": Counter({"visited": 1, "errors": 4})},
        edges=[("https://site.test/", "https://site.test/a")],
        pages=[
            {
                "url": "https://site.test/",
                "status": 200,
                "content_type": "text/html",
                "title": None,
            }
        ],
        sitemap_hosts={"site.test", "a.test"},
        fetched_sitemaps={"https://site.test/sitemap.xml"},
    )


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = _snapshot()
    save_checkpoint(original, str(path))
    loaded = load_checkpoint(str(path))
    # dataclass-эквивалентность покрывает все поля разом...
    assert loaded == original
    # ...а типы восстановлены именно как множества/Counter/кортежи, не списки.
    assert isinstance(loaded.seen, set)
    assert isinstance(loaded.sitemap_hosts, set)
    assert isinstance(loaded.fetched_sitemaps, set)
    assert isinstance(loaded.per_host["site.test"], Counter)
    assert all(isinstance(item, tuple) for item in loaded.frontier)
    assert all(isinstance(edge, tuple) for edge in loaded.edges)


def test_atomic_write_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    original = _snapshot()
    save_checkpoint(original, str(path))

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash between tmp write and os.replace")

    # «Креш» между записью tmp-файла и os.replace: целевой файл не должен
    # быть ни усечён, ни заменён недописанным содержимым.
    monkeypatch.setattr("politecrawl.checkpoint.os.replace", broken_replace)
    changed = _snapshot()
    changed.stats_totals["visited"] = 99
    with pytest.raises(OSError):
        save_checkpoint(changed, str(path))
    monkeypatch.undo()

    assert load_checkpoint(str(path)) == original  # прежний валидный файл цел
    # и в директории не осталось ни tmp-файла, ни другого мусора
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{this is not json", encoding="utf-8")
    with pytest.raises(CheckpointFormatError, match="not valid JSON"):
        load_checkpoint(str(path))


def test_load_rejects_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_checkpoint(_snapshot(), str(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["frontier"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointFormatError, match="frontier"):
        load_checkpoint(str(path))


def test_load_rejects_bad_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_checkpoint(_snapshot(), str(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointFormatError, match="schema version"):
        load_checkpoint(str(path))


def test_sets_serialized_sorted(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_checkpoint(_snapshot(), str(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("seen", "sitemap_hosts", "fetched_sitemaps"):
        assert raw[key] == sorted(raw[key]), key
    # сортировка наблюдаема: в sitemap_hosts больше одного элемента
    assert len(raw["sitemap_hosts"]) == 2
