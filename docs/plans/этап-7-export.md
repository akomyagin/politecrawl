# Этап 7 — Экспорт результатов обхода: план

## Цель

Добавить структурированный экспорт результатов обхода поверх текстового отчёта
Этапа 5. Три опциональных экспорта, включаемых CLI-флагами:

- **граф ссылок** (рёбра «страница → исходящая ссылка») в JSONL или CSV;
- **sitemap-подобный XML** со всеми успешно посещёнными URL;
- **дамп метаданных страниц** (`url`, `status`, `content_type`, `title`) в JSONL
  или CSV.

Экспорт — чистая фаза сериализации ПОСЛЕ обхода: `Crawler` копит данные по ходу
конвейера, файлы пишутся один раз в конце из уже собранных списков. Форматирование
(`export.py`) не имеет сетевых/async зависимостей и тестируется без `respx`.
Спецификация — в `docs/TECHNICAL_PLAN.md` §«Этап 7». Этот план — прямая трансляция
ТЗ в код.

## Границы / что НЕ трогать

- **Не менять** публичные контракты `robots`/`ratelimit`/`dedup` — они готовы.
- **Не менять** `FetchResult` (dataclass в `fetcher.py`) и `extract_links` — title
  извлекается отдельной чистой функцией `extract_title` из `result.body` и хранится
  только в `crawler.pages`. Это исключает правку ~20 мест создания `FetchResult` в
  `tests/test_fetcher.py` и `tests/test_cli.py`.
- **Не менять** `pyproject.toml`: экспорт использует только stdlib (`json`, `csv`,
  `xml.sax.saxutils`), новых зависимостей нет.
- Реальной сети в тестах нет. Тесты `export.py` — чистые (без `respx`, через
  `tmp_path`). Тесты `cli.py` — под `@respx.mock`. `asyncio_mode = "auto"`, поэтому
  `@pytest.mark.asyncio` на async-тестах не нужен (но `@respx.mock` — нужен).
- Весь код (идентификаторы, docstrings, комментарии) — на английском; docstring
  модуля/subject коммита — на русском (конвенция проекта).
- `ruff check .`, `mypy` (strict), `pytest -q` должны быть зелёными.

## Файлы

- **`src/politecrawl/export.py`** — новый модуль: чистые функции сериализации +
  `ExportFormatError`.
- **`src/politecrawl/fetcher.py`** — добавить `extract_title` и `_TitleParser`
  (после `extract_links`; `FetchResult`/`extract_links` не трогать).
- **`src/politecrawl/cli.py`** — сбор `edges`/`pages` в `Crawler`, три новых флага,
  вызов экспорта после обхода. `_run` возвращает `Crawler` вместо `CrawlStats`.
- **`tests/test_export.py`** — новый, чистые функции (без сети).
- **`tests/test_fetcher.py`** — дополнить тестами `extract_title`.
- **`tests/test_cli.py`** — дополнить тестами сбора данных и флагов экспорта.

---

## 1. `fetcher.py` — `extract_title`

Добавить ПОСЛЕ `extract_links` (в конце файла). Не менять существующие импорты,
кроме, возможно, ничего — `HTMLParser` уже импортирован.

```python
class _TitleParser(HTMLParser):
    """Captures the text of the first <title>…</title> element."""

    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self._done = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self._done:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self._done:
            self._chunks.append(data)

    @property
    def title(self) -> str | None:
        text = " ".join("".join(self._chunks).split())
        return text or None


def extract_title(html: str) -> str | None:
    """Return the text of the first <title>…</title>, or None if absent.

    Whitespace is collapsed to single spaces and trimmed; an empty or
    whitespace-only title yields None. Only the first <title> is used.
    """
    parser = _TitleParser()
    parser.feed(html)
    return parser.title
```

- `" ".join("".join(chunks).split())` сворачивает любые пробелы/переводы строк в
  одиночные пробелы и триммит края → пустая строка становится `None`.
- `_done` фиксирует ПЕРВЫЙ title: последующие `<title>` игнорируются.
- Парсинг не прерывается досрочно (HTMLParser не бросает; ранний выход не нужен —
  тела страниц малы в тестах, а корректность важнее микрооптимизации).

---

## 2. `export.py` — новый модуль

Порядок объявлений: docstring модуля → импорты → типы-алиасы → `ExportFormatError`
→ `_format_from_path` → `write_edges` → `write_pages` → `write_sitemap`.

### Docstring модуля (русский) + импорты

```python
"""Сериализация результатов обхода в файлы разных форматов.

Этап 7: чистые функции экспорта — принимают уже собранные данные (списки/словари)
и путь, пишут файл. Без сетевых и async-зависимостей: только stdlib. Формат
edges/pages выбирается по расширению пути (.jsonl / .csv); sitemap всегда XML.
"""

from __future__ import annotations

import csv
import json
from typing import Union
from xml.sax.saxutils import escape

Edge = tuple[str, str]  # (source_url, target_url)
PageMeta = dict[str, Union[str, int, None]]  # url, status, content_type, title
```

- `Union[str, int, None]` (а не `str | int | None`) в алиасе — безопаснее для
  strict-mypy в аннотации значения dict; можно и `str | int | None`, оба проходят
  на py310. Использовать `str | int | None` для единообразия со стилем проекта,
  если mypy не ругается; при сомнении — `Union`.

### `ExportFormatError` + `_format_from_path`

```python
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
```

- Регистр расширения нормализуется (`.JSONL` → `jsonl`).
- Сообщение самодостаточное — печатается пользователю в stderr без traceback.

### `write_edges`

```python
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
```

### `write_pages`

```python
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
                writer.writerow({k: ("" if page.get(k) is None else page.get(k)) for k in _PAGE_FIELDS})
```

- `extrasaction="ignore"` — на случай лишних ключей в dict (страховка).
- `None` → `""` для CSV явно (иначе `DictWriter` пишет пустую строку и так, но
  явность делает контракт очевидным и тест стабильным).

### `write_sitemap`

```python
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
```

- `write_sitemap` НЕ вызывает `_format_from_path` — формат всегда XML вне
  зависимости от расширения (пользователь может назвать файл `.xml` или иначе).
- `escape` экранирует `&`, `<`, `>` в URL (query-параметры с `&` — частый случай).

---

## 3. `cli.py` — сбор данных и проводка

### Импорты — добавить

```python
from politecrawl.export import (
    Edge,
    ExportFormatError,
    PageMeta,
    write_edges,
    write_pages,
    write_sitemap,
)
```

и к существующему `from politecrawl.fetcher import extract_links, fetch` добавить
`extract_title`:

```python
from politecrawl.fetcher import extract_links, extract_title, fetch
```

### 3a. `Crawler.__init__` — новые аккумуляторы

Добавить в конец `__init__` (после `self.stats = CrawlStats()`):

```python
self.edges: list[Edge] = []
self.pages: list[PageMeta] = []
```

Мутируются только из воркеров одного event loop, без `await` между чтением и
записью (`list.append`) — атомарны в рамках шага loop, лок не нужен (тот же
прецедент, что `CrawlStats`/`UrlDedup`, Этапы 4-5).

### 3b. `Crawler._process` — сбор pages (шаг 5) и edges (шаг 6)

Изменить хвост `_process`. Текущий код (строки 154-164):

```python
        # 5. transport error -> errors; otherwise visited.
        if result.error is not None:
            self.stats.record_error(host)
            return
        self.stats.record_visited(host)

        # 6. extract links and enqueue them if depth allows. ...
        if depth + 1 <= self._max_depth:
            for link in extract_links(url, result.body):
                self._queue.put_nowait((link, depth + 1))
```

Заменить на:

```python
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
        # not the crawl graph), then enqueue targets only if depth allows.
        links = extract_links(url, result.body)
        for link in links:
            self.edges.append((url, link))
        if depth + 1 <= self._max_depth:
            for link in links:
                self._queue.put_nowait((link, depth + 1))
```

**Ключевые инварианты правки:**
- `extract_links` теперь вызывается ВСЕГДА при успешном fetch (раньше — только
  внутри `if depth+1<=max_depth`). Рёбра пишутся независимо от глубины: даже если
  target не ставится в очередь (за пределом глубины) или потом отсеётся
  дедупом/robots при заборе — сам факт «страница ссылается на» фиксируется. Это
  граф ссылок, а не граф обхода.
- `pages` пишется на шаге 5, ровно когда `record_visited` (только успешные
  fetch, `result.error is None`).
- Дубли рёбер НЕ дедупятся: `extract_links` сохраняет дубли, граф отражает
  страницу как есть.

### 3c. `_build_parser` — три новых флага

Добавить перед `return parser`:

```python
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
```

- Атрибуты после парсинга: `args.export_edges`, `args.export_sitemap`,
  `args.export_pages` — `str | None`, дефолт `None` = экспорт выключен.

### 3d. `_run` — вернуть `Crawler`

Изменить сигнатуру и `return` `_run`. Было:

```python
) -> tuple[CrawlStats, float]:
    ...
        await crawler.run(seeds, total_workers)
    elapsed = time.perf_counter() - start
    return crawler.stats, elapsed
```

Стало:

```python
) -> tuple[Crawler, float]:
    ...
        await crawler.run(seeds, total_workers)
    elapsed = time.perf_counter() - start
    return crawler, elapsed
```

- Возврат `Crawler` даёт `main` доступ к `crawler.stats`, `crawler.edges`,
  `crawler.pages`. Существующие тесты `test_cli.py`, распаковывающие
  `stats, elapsed = await _run(...)`, нужно обновить на
  `crawler, elapsed = await _run(...)` + `crawler.stats`. **Проверить и поправить
  все вызовы `_run` в `tests/test_cli.py`** — это часть Этапа 7.

### 3e. `_run_exports` — запись файлов после обхода

Новая функция (после `_format_report`, перед `main`):

```python
def _run_exports(crawler: Crawler, args: argparse.Namespace) -> None:
    """Write requested exports. Raises ExportFormatError on a bad extension."""
    if args.export_edges is not None:
        write_edges(crawler.edges, args.export_edges)
    if args.export_pages is not None:
        write_pages(crawler.pages, args.export_pages)
    if args.export_sitemap is not None:
        write_sitemap([str(p["url"]) for p in crawler.pages], args.export_sitemap)
```

- Sitemap берёт URL только посещённых страниц (`crawler.pages`), не все виденные
  ссылки. `str(p["url"])` — потому что `PageMeta` значения `str | int | None`;
  `url` всегда `str`, но `str(...)` успокаивает mypy (значение по ключу — union).

### 3f. `main` — вызов экспорта + обработка ошибки формата

Изменить `main`. Было (строки 248-261):

```python
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    stats, elapsed = asyncio.run(
        _run(
            args.seeds,
            max_depth=args.max_depth,
            per_domain_concurrency=args.per_domain_concurrency,
            total_workers=args.total_workers,
            user_agent=args.user_agent,
        )
    )
    print(_format_report(stats, elapsed))
    return 0
```

Стало:

```python
def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: разобрать аргументы, обойти граф, отчёт + экспорт."""
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
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
    except ExportFormatError as exc:
        print(f"politecrawl: {exc}", file=sys.stderr)
        return 2
    return 0
```

- Отчёт печатается ДО экспорта (обход состоялся; экспорт — постобработка). Плохое
  расширение флага не отменяет уже показанный отчёт, но даёт код выхода `2` и
  сообщение в stderr — **без traceback**.
- Код `2` согласован с `argparse` (тот тоже возвращает 2 на неверные аргументы).
- **Тонкость:** ошибку расширения формально можно ловить и ДО обхода (валидировать
  расширения сразу после парсинга, чтобы не гонять обход зря). Это допустимое
  улучшение: в начале `main`, после `parse_args`, вызвать `_validate_export_paths(args)`,
  которая прогоняет `_format_from_path` для `export_edges`/`export_pages` (если не
  `None`) и ловит `ExportFormatError` → stderr + `return 2` до `asyncio.run`.
  Рекомендуется — экономит бессмысленный обход при опечатке в расширении. Если
  внедряешь, добавь тест `test_export_bad_extension_before_crawl` (проверяет, что
  сеть не трогается — например, respx-route на seed имеет `call_count == 0`).

---

## 4. Тесты

### 4a. `tests/test_export.py` (новый, БЕЗ сети/respx)

Стиль: `from __future__ import annotations`, `import csv/json`, чтение файла через
`tmp_path` (pytest-фикстура). Импорт из `politecrawl.export`.

Тест-кейсы:

1. **`test_write_edges_jsonl`** — `write_edges([("a", "b"), ("a", "c")],
   str(tmp_path / "e.jsonl"))`; прочитать файл построчно, распарсить `json.loads`,
   проверить два объекта `{"source": "a", "target": "b"}` и `{..."c"}`, порядок
   сохранён.
2. **`test_write_edges_csv`** — `.csv`; прочитать `csv.reader`, проверить заголовок
   `["source", "target"]` и строки.
3. **`test_write_pages_jsonl`** — `write_pages([{"url": "u", "status": 200,
   "content_type": "text/html", "title": "T"}, {"url": "u2", "status": 404,
   "content_type": None, "title": None}], path)`; распарсить, проверить, что
   `None` сериализован как `null` (в объекте `content_type is None`).
4. **`test_write_pages_csv`** — тот же вход в `.csv`; проверить заголовок
   `["url", "status", "content_type", "title"]`, что `None` → пустая строка в
   ячейке.
5. **`test_write_sitemap_xml`** — `write_sitemap(["https://x/a",
   "https://x/b?q=1&r=2"], path)`; прочитать файл, проверить наличие
   `<?xml version="1.0"`, `<urlset`, `<loc>https://x/a</loc>`, и что `&` в query
   экранирован как `&amp;` (нет сырого `&r=`).
6. **`test_unrecognized_extension_raises`** — `pytest.raises(ExportFormatError):
   write_edges([], str(tmp_path / "out.txt"))`. Аналогично для `write_pages`.
7. **`test_extension_case_insensitive`** — `write_edges([("a", "b")],
   str(tmp_path / "e.JSONL"))` не бросает, файл создан и парсится как JSONL.
   Аналогично `.CSV`.
8. **`test_write_edges_empty`** — пустой список рёбер: JSONL → пустой файл (0
   строк) или только `\n`? (JSONL пустой = 0 строк); CSV → только заголовок.
   Обход/экспорт не падает.
9. **`test_sitemap_ignores_extension`** — `write_sitemap(["u"], str(tmp_path /
   "s.txt"))` пишет XML (не бросает на `.txt`) — формат всегда XML.

`ExportFormatError` импортировать из `politecrawl.export`.

### 4b. `tests/test_fetcher.py` (дополнить)

Добавить после блока `extract_links` (импорт: добавить `extract_title` в
`from politecrawl.fetcher import ...`). Чистые функции, без `@respx.mock`:

1. **`test_extract_title_basic`** — `extract_title("<html><head><title>Hi</title>
   </head></html>") == "Hi"`.
2. **`test_extract_title_absent`** — `extract_title("<html><body>x</body></html>")
   is None`.
3. **`test_extract_title_whitespace_collapsed`** — `extract_title(
   "<title>\n  Hello   World\n</title>") == "Hello World"`.
4. **`test_extract_title_empty_is_none`** — `extract_title("<title>   </title>")
   is None`.
5. **`test_extract_title_first_wins`** — `extract_title(
   "<title>First</title><title>Second</title>") == "First"`.

### 4c. `tests/test_cli.py` (дополнить)

Сначала **обновить существующие вызовы `_run`**: распаковка теперь
`crawler, elapsed = await _run(...)`; где нужен `stats` — брать `crawler.stats`.

Новые тесты (стиль как существующие — `@respx.mock`, мини-граф на `https://site.test`,
обязательно мокать `/robots.txt` → 404):

1. **`test_crawler_collects_edges`** (`@respx.mock`) — граф: `/` → `/a`, `/b`;
   `/a` → `/deep`. `_run([seed], max_depth=1, ...)`. Проверить, что
   `crawler.edges` содержит `("https://site.test/", "https://site.test/a")` и
   `(..., "/b")`; и что ребро от `/a` к `/deep` присутствует **несмотря на то, что
   `/deep` за пределом `max_depth=1`** (ребро фиксируется, target не ставится в
   очередь). То есть `crawler.edges` включает `("https://site.test/a",
   "https://site.test/deep")`, при этом `deep_route.call_count == 0`.
2. **`test_crawler_collects_pages_with_title`** (`@respx.mock`) — страницы с
   `<title>`; `_run(...)`. Проверить, что `crawler.pages` содержит по объекту на
   посещённую страницу с ключами `url`, `status == 200`, `content_type`
   (начинается с `text/html`), `title` == текст из `<title>`. Число элементов
   `pages` == `stats.visited`.
3. **`test_edges_recorded_regardless_of_dedup`** (`@respx.mock`) — граф с циклом
   (`/a` → `/`): `/` посещается один раз (дедуп), но ребро `("/a", "/")`
   присутствует в `crawler.edges` (граф ссылок фиксирует даже ссылку на уже
   виденный URL).
4. **`test_export_flags_write_files`** (`@respx.mock`, `tmp_path`, `capsys`) —
   `main([seed, "--max-depth", "1", "--export-edges", str(tmp_path/"e.jsonl"),
   "--export-pages", str(tmp_path/"p.csv"), "--export-sitemap",
   str(tmp_path/"s.xml")])`. Проверить: `main` вернул `0`; три файла существуют и
   непусты; `e.jsonl` парсится как JSONL с рёбрами; `s.xml` содержит `<urlset`.
5. **`test_export_bad_extension_exits_nonzero`** (`@respx.mock`, `capsys`) —
   `main([seed, "--export-edges", str(tmp_path/"e.txt")])`. Проверить: вернул `2`;
   stderr (через `capsys.readouterr().err`) содержит имя файла и `unsupported`
   (или подстроку сообщения); **stdout НЕ содержит traceback** (нет `Traceback`).
6. **`test_no_export_flags_writes_nothing`** (`@respx.mock`, `tmp_path`) — `main`
   без экспорт-флагов; проверить, что в `tmp_path` не создано файлов
   (`list(tmp_path.iterdir()) == []`) и `stats`/поведение обхода не изменилось
   (visited как раньше). Вернул `0`.
7. **(опционально, если внедрён pre-crawl-валидатор)**
   **`test_export_bad_extension_before_crawl`** — плохое расширение → seed-route
   `call_count == 0` (обход не запускался), вернул `2`.

### Замечания по тестам

- **Мокать `robots.txt` для каждого хоста** — иначе `RobotsCache` реально
  запросит его; вернуть 404 (→ allow-all) для детерминизма.
- **respx-роуты по точному URL** + `httpx.Response(200, html=..., headers=
  {"content-type": "text/html; charset=utf-8"})` — чтобы `content_type` в
  `pages` был осмысленным.
- **Относительные ссылки** (`href='/a'`) абсолютизируются `extract_links` через
  `urljoin` — совпадут с точными respx-URL.
- `tmp_path` — стандартная pytest-фикстура, путь-`Path`; передавать в `write_*` и
  флаги как `str(tmp_path / "name")`.
- Тесты `export.py` — БЕЗ `@respx.mock` и БЕЗ `async def` (чистые синхронные
  функции).

---

## Порядок проверок / критерий готовности

Перед завершением этапа (чеклист SKILL.md):

- `.venv/bin/pytest -q` — весь набор зелёный (существующие + `test_export.py` +
  новые в `test_fetcher.py`/`test_cli.py`; обновлённые вызовы `_run`).
- `.venv/bin/ruff check .` — чисто (включая `I` — порядок импортов export в cli).
- `.venv/bin/mypy` — чисто (strict): аннотировать `Edge`, `PageMeta`,
  `list[Edge]`/`list[PageMeta]` на `Crawler`, сигнатуру `_run -> tuple[Crawler,
  float]`, `_run_exports(crawler: Crawler, args: argparse.Namespace) -> None`.
  `write_*` — все с явными типами.
- Ручная проверка (опционально, вне тестов): `politecrawl <url> --export-edges
  out.jsonl --export-pages pages.csv --export-sitemap sitemap.xml` пишет три
  файла; `politecrawl <url> --export-edges out.bogus` печатает ошибку в stderr и
  выходит с кодом 2, без traceback.

## Ключевые инварианты, которые обязаны выполняться

1. **Рёбра — граф ссылок, а не граф обхода:** `extract_links` вызывается при
   КАЖДОМ успешном fetch; ребро `(source, target)` пишется для каждой исходящей
   ссылки независимо от `max_depth`/дедупа/robots-статуса target.
2. **Pages — только успешно посещённые:** запись в `crawler.pages` строго там же,
   где `record_visited` (шаг 5, `result.error is None`).
3. **Экспорт опционален:** без флагов ничего не пишется, поведение обхода не
   меняется.
4. **Нераспознанное расширение → понятная ошибка + код выхода 2**, не traceback.
5. **`export.py` — чистый:** без сети/async; тестируется без respx.
6. **`FetchResult`/`extract_links` не изменены** — title извлекается отдельной
   функцией из `result.body`.
7. **Сбор данных без локов** — `list.append` из одного event loop атомарен по шагу
   (тот же прецедент, что `CrawlStats`/`UrlDedup`).
