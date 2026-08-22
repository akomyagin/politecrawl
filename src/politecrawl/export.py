"""Сериализация результатов обхода в файлы разных форматов.

Этап 7: чистые функции экспорта — принимают уже собранные данные (списки/словари)
и путь, пишут файл. Без сетевых и async-зависимостей: только stdlib. Формат
edges/pages выбирается по расширению пути (.jsonl / .csv); sitemap всегда XML.
"""

from __future__ import annotations

import csv
import json
from xml.sax.saxutils import escape

Edge = tuple[str, str]  # (source_url, target_url)
PageMeta = dict[str, str | int | None]  # url, status, content_type, title


class ExportFormatError(ValueError):
    """Raised when an export path has an unrecognized file extension."""


_TABULAR_FORMATS = {"jsonl", "csv"}


def _format_from_path(path: str) -> str:
    """Return 'jsonl' or 'csv' from the path's extension (case-insensitive).

    Raises ExportFormatError with a user-facing message on any other suffix.
    """
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix not in _TABULAR_FORMATS:
        raise ExportFormatError(
            f"unsupported export extension for {path!r}: "
            f"expected one of {sorted(_TABULAR_FORMATS)} (.jsonl or .csv)"
        )
    return suffix


def validate_extension(path: str) -> None:
    """Raise ExportFormatError if path's extension is not .jsonl or .csv.

    Lets callers (the CLI) fail fast on a bad --export-* path before doing
    any work, without reaching into write_edges/write_pages internals.
    """
    _format_from_path(path)


def write_edges(edges: list[Edge], path: str) -> None:
    """Write link-graph edges to path as JSONL or CSV (by extension).

    JSONL: one {"source": ..., "target": ...} object per line.
    CSV: header 'source,target' then one row per edge.
    """
    fmt = _format_from_path(path)
    if fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for source, target in edges:
                f.write(json.dumps({"source": source, "target": target}, ensure_ascii=False))
                f.write("\n")
    else:  # csv
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["source", "target"])
            writer.writerows(edges)


_PAGE_FIELDS = ["url", "status", "content_type", "title"]


def write_pages(pages: list[PageMeta], path: str) -> None:
    """Write page metadata to path as JSONL or CSV (by extension).

    Columns/keys: url, status, content_type, title. None serializes as an
    empty cell in CSV and as null in JSON.
    """
    fmt = _format_from_path(path)
    if fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for page in pages:
                f.write(json.dumps(page, ensure_ascii=False))
                f.write("\n")
    else:  # csv
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_PAGE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for page in pages:
                row = {k: ("" if page.get(k) is None else page.get(k)) for k in _PAGE_FIELDS}
                writer.writerow(row)


def write_sitemap(urls: list[str], path: str) -> None:
    """Write a sitemap-like XML listing all URLs (extension is ignored).

    Root <urlset> in the sitemaps.org 0.9 namespace; one <url><loc>…</loc></url>
    per URL. URLs are XML-escaped.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in urls:
        lines.append(f"  <url><loc>{escape(url)}</loc></url>")
    lines.append("</urlset>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")
