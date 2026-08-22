"""Тесты Этапа 7: чистые функции экспорта (без сети, без respx, без async)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from politecrawl.export import (
    ExportFormatError,
    PageMeta,
    write_edges,
    write_pages,
    write_sitemap,
)

# --- write_edges ------------------------------------------------------------


def test_write_edges_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    write_edges([("a", "b"), ("a", "c")], str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"source": "a", "target": "b"},
        {"source": "a", "target": "c"},
    ]


def test_write_edges_csv(tmp_path: Path) -> None:
    path = tmp_path / "e.csv"
    write_edges([("a", "b"), ("a", "c")], str(path))
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["source", "target"], ["a", "b"], ["a", "c"]]


def test_write_edges_empty(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "empty.jsonl"
    write_edges([], str(jsonl_path))
    assert jsonl_path.read_text(encoding="utf-8") == ""  # zero lines

    csv_path = tmp_path / "empty.csv"
    write_edges([], str(csv_path))
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["source", "target"]]  # header only


# --- write_pages ------------------------------------------------------------

_PAGES: list[PageMeta] = [
    {"url": "u", "status": 200, "content_type": "text/html", "title": "T"},
    {"url": "u2", "status": 404, "content_type": None, "title": None},
]


def test_write_pages_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"
    write_pages([dict(p) for p in _PAGES], str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    objects = [json.loads(line) for line in lines]
    assert objects[0] == {"url": "u", "status": 200, "content_type": "text/html", "title": "T"}
    assert objects[1]["url"] == "u2"
    assert objects[1]["status"] == 404
    assert objects[1]["content_type"] is None  # None -> JSON null
    assert objects[1]["title"] is None


def test_write_pages_csv(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    write_pages([dict(p) for p in _PAGES], str(path))
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["url", "status", "content_type", "title"]
    assert rows[1] == ["u", "200", "text/html", "T"]
    assert rows[2] == ["u2", "404", "", ""]  # None -> empty cell


# --- write_sitemap ----------------------------------------------------------


def test_write_sitemap_xml(tmp_path: Path) -> None:
    path = tmp_path / "s.xml"
    write_sitemap(
        ["https://x/a", "https://x/b?q=1&r=2", "https://x/c?q=<script>&r=1>2"],
        str(path),
    )
    text = path.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0"')
    assert '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' in text
    assert "<loc>https://x/a</loc>" in text
    assert "&amp;r=2" in text  # & in query is XML-escaped
    assert "&r=" not in text.replace("&amp;r=", "")  # no raw ampersand left
    assert "&lt;script&gt;" in text  # < and > are XML-escaped too
    assert "<script>" not in text
    assert text.rstrip().endswith("</urlset>")


def test_sitemap_ignores_extension(tmp_path: Path) -> None:
    path = tmp_path / "s.txt"
    write_sitemap(["u"], str(path))  # must not raise on .txt
    text = path.read_text(encoding="utf-8")
    assert "<urlset" in text
    assert "<loc>u</loc>" in text


# --- extension handling -----------------------------------------------------


def test_unrecognized_extension_raises(tmp_path: Path) -> None:
    with pytest.raises(ExportFormatError):
        write_edges([], str(tmp_path / "out.txt"))
    with pytest.raises(ExportFormatError):
        write_pages([], str(tmp_path / "out.xml"))
    with pytest.raises(ExportFormatError):
        write_edges([], str(tmp_path / "no_extension"))


def test_extension_case_insensitive(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "e.JSONL"
    write_edges([("a", "b")], str(jsonl_path))
    assert json.loads(jsonl_path.read_text(encoding="utf-8")) == {"source": "a", "target": "b"}

    csv_path = tmp_path / "e.CSV"
    write_edges([("a", "b")], str(csv_path))
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["source", "target"], ["a", "b"]]
