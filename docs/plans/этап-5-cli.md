# Этап 5 — Ограничение глубины + CLI + отчёт: план

## Цель

Собрать модули Этапов 1–4 (`fetcher`, `robots`, `ratelimit`, `dedup`) за одним
CLI: разобрать аргументы, запустить конкурентный обход фейк-графа страниц с
ограничением глубины и per-domain politeness, напечатать в stdout итоговый
отчёт (счётчики + разбивка по хостам + время). Спецификация — в
`docs/TECHNICAL_PLAN.md` §«Этап 5» (строки 208–263). Этот план — прямая
трансляция ТЗ в код; архитектуру заново не изобретаем.

## Границы / что НЕ трогать

- Не менять публичные контракты `fetcher`/`robots`/`ratelimit`/`dedup` — они
  готовы и покрыты тестами. `cli.py` их только вызывает.
- Не менять `pyproject.toml`: entry point `politecrawl = "politecrawl.cli:main"`
  уже прописан (строка 32), сигнатура `main()` должна остаться совместимой.
- Реальной сети в тестах нет — только `respx`. `asyncio_mode = "auto"`, поэтому
  `@pytest.mark.asyncio` на тестах не нужен (но `@respx.mock` — нужен).
- Весь код (идентификаторы, docstrings, комментарии) — на английском; docstring
  модуля/subject коммита — на русском (конвенция проекта).

## Файлы

- **`src/politecrawl/cli.py`** — полностью заменить текущую заглушку. Весь код
  Этапа 5 живёт в одном файле (публичный контракт — только `main`; `Crawler`,
  `CrawlStats`, `crawl` — внутренние, отдельный модуль не нужен).
- **`tests/test_cli.py`** — новый файл, стиль как `tests/test_fetcher.py` /
  `tests/test_robots.py`: без классов, простые `async def test_*`, `@respx.mock`.

---

## Реализация `cli.py`

Порядок объявлений в файле: импорты → `CrawlStats` → `Crawler` →
`_build_parser` → `_run` (async сборка+обход) → `_format_report` → `main`.

### Импорты

```python
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx

from politecrawl.dedup import UrlDedup
from politecrawl.fetcher import extract_links, fetch
from politecrawl.ratelimit import PerDomainLimiter
from politecrawl.robots import RobotsCache
```

### 1. Счётчики отчёта — `CrawlStats`

Обычный класс (не dataclass обязателен, но допустим). Держит четыре глобальных
`int` и per-host разбивку. Per-host — `dict[str, Counter[str]]`, где ключ
внешнего dict = host (`urlsplit(url).netloc`), а `Counter` считает по строковым
ключам `"visited" | "skipped_dedup" | "skipped_robots" | "errors"`.

```python
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
```

**Подтверждение рассуждения про синхронизацию (пункт 5 ТЗ):** блокировка НЕ
нужна. asyncio однопоточен; воркеры уступают управление только на `await`.
Каждый `record_*` — цепочка синхронных мутаций `int += 1` и
`Counter[key] += 1` без `await` внутри, поэтому другой воркер не может
вклиниться между чтением и записью — инкремент атомарен в рамках шага event
loop. Ровно тем же свойством пользуется `UrlDedup.add` (см. его docstring).
Осторожность нужна была бы только если бы между чтением и записью счётчика
стоял `await` — здесь его нет по построению.

### 2. Класс `Crawler`

Инкапсулирует зависимости + фронтир + конвейер. Создаётся внутри `_run` уже
внутри активного event loop (важно: `asyncio.Queue` и локи в `RobotsCache`/
`PerDomainLimiter` привязываются к текущему loop, поэтому конструировать их
нужно внутри `asyncio.run`, а не до него).

```python
class Crawler:
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
```

#### 2a. Метод `async def run(self, seeds, total_workers) -> None`

Точка сборки обхода — тело именно как в ТЗ (строки 244–249):

```python
async def run(self, seeds: list[str], total_workers: int) -> None:
    for seed in seeds:
        self._queue.put_nowait((seed, 0))
    workers = [
        asyncio.create_task(self._worker()) for _ in range(total_workers)
    ]
    try:
        await self._queue.join()          # ждём, пока фронтир опустошён
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
```

- Seed-ы кладём с `depth=0` ДО старта воркеров (иначе `queue.join()` мог бы
  вернуться на пустой очереди мгновенно).
- `queue.join()` разблокируется, когда число `task_done()` сравняется с числом
  `put_nowait`. Каждый воркер обязан вызвать `task_done()` в `finally` (см.
  ниже) — иначе `join()` зависнет навсегда.
- Отмена воркеров + `gather(..., return_exceptions=True)` глушит
  `CancelledError` от висящих `queue.get()`.

#### 2b. Метод `async def _worker(self) -> None`

Бесконечный цикл забора из очереди; на каждый элемент вызывает `_process`;
`task_done()` строго в `finally`.

```python
async def _worker(self) -> None:
    while True:
        url, depth = await self._queue.get()
        try:
            await self._process(url, depth)
        finally:
            self._queue.task_done()
```

- `_process` НЕ должен пробрасывать исключения наружу (все ошибки сети
  ловятся внутри `fetch`; парсинг ссылок на пустом body безопасен). Но
  `try/finally` вокруг `_process` обязателен: если `_process` внезапно кинет
  (напр. неожиданный баг), `task_done()` всё равно вызовется и `join()` не
  зависнет. Ловить и глотать исключение здесь не надо — `finally` достаточно;
  необработанное исключение всплывёт в `gather` (но воркер к тому моменту уже
  сделал `task_done`). Для устойчивости обхода допускается обернуть тело
  `_process` в `try/except Exception` внутри самого `_process`, но проще
  оставить контракт «`_process` не бросает».

#### 2c. Метод `async def _process(self, url, depth) -> None` — конвейер

Строго по ТЗ (строки 230–242), порядок шагов фиксирован. `host` вычисляется
один раз для счётчиков.

```python
async def _process(self, url: str, depth: int) -> None:
    host = urlsplit(url).netloc

    # 1. dedup: атомарная проверка-и-вставка. Уже видели -> skipped_dedup.
    if not self._dedup.add(url):
        self.stats.record_skipped_dedup(host)
        return

    # 2. robots: запрещено -> skipped_robots.
    if not await self._robots.allowed(url, self._user_agent):
        self.stats.record_skipped_robots(host)
        return

    # 3-4. per-domain слот вокруг самой загрузки.
    async with self._limiter.slot(host):
        result = await fetch(self._client, url)

    # 5. ошибка сети -> errors; иначе visited.
    if result.error is not None:
        self.stats.record_error(host)
        return
    self.stats.record_visited(host)

    # 6. извлечь ссылки и положить в очередь, если глубина позволяет.
    if depth + 1 <= self._max_depth:
        for link in extract_links(url, result.body):
            self._queue.put_nowait((link, depth + 1))
```

Замечания по конвейеру:
- **Порядок шагов не менять.** dedup ПЕРЕД robots — чтобы уже виденный URL не
  тратил обращение к robots-кешу; это же гарантирует, что цикл ссылок
  (страница ссылается сама на себя) не даёт бесконечного обхода: повторный URL
  отсекается на шаге 1.
- **`host` для слота и для счётчиков — один и тот же** `urlsplit(url).netloc`.
  Это ключ per-domain лимита (как задумано в Этапе 3: «домен» = хост из URL).
- **Дедуп на входе, а не на выходе.** В очередь кладём ссылки БЕЗ фильтрации
  дублей; отсекает их шаг 1 у воркера, который потом заберёт ссылку. Это и есть
  «add атомарен» — два воркера, забравшие один и тот же URL, не обойдут его
  дважды: первый `add` вернёт `True`, второй `False`.
- **`max_depth` режет ИМЕННО постановку в очередь.** При `max_depth=0` условие
  `depth+1 <= 0` ложно для seed (`depth=0`) — ссылки не кладутся вовсе,
  обходятся только seed-ы. При `max_depth=2` страницы глубины 3 в очередь не
  попадают → и не запрашиваются (что проверяет тест по `route.call_count`).

### 3. Парсер аргументов — `_build_parser`

```python
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="politecrawl",
        description="Polite structural async web crawler.",
    )
    parser.add_argument("seeds", nargs="+", help="one or more seed URLs")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="crawl depth from each seed (seed = depth 0)")
    parser.add_argument("--per-domain-concurrency", type=int, default=2,
                        help="max concurrent requests to a single host")
    parser.add_argument("--total-workers", type=int, default=8,
                        help="size of the worker pool")
    parser.add_argument("--user-agent", type=str, default="politecrawl/0.0",
                        help="User-Agent for requests and robots.can_fetch")
    return parser
```

- Имена атрибутов после парсинга: `args.seeds` (list[str]),
  `args.max_depth`, `args.per_domain_concurrency`, `args.total_workers`,
  `args.user_agent` (argparse превращает `--per-domain-concurrency` в
  `per_domain_concurrency`).
- Дефолты — ровно из таблицы ТЗ (строки 216–222): `--max-depth 2`,
  `--per-domain-concurrency 2`, `--total-workers 8`,
  `--user-agent "politecrawl/0.0"`.

### 4. Async-сборка и обход — `_run`

Конструирует клиента и зависимости ВНУТРИ event loop, гоняет обход, возвращает
`CrawlStats` и затраченное время. Выделен отдельной async-функцией, чтобы
`main` оставался синхронным и легко тестируемым, а тесты могли дергать `_run`
напрямую под `@respx.mock` без `asyncio.run`.

```python
async def _run(
    seeds: list[str],
    *,
    max_depth: int,
    per_domain_concurrency: int,
    total_workers: int,
    user_agent: str,
) -> tuple[CrawlStats, float]:
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
    return crawler.stats, elapsed
```

- `follow_redirects=True` — как во всех существующих тестах (fetcher/robots
  создают клиент именно так; редиректы следует клиент, см. Этап 1).
- `headers={"user-agent": ...}` — тот же UA уходит и в HTTP-запросы, и в
  `robots.allowed(url, user_agent)`. Для тестов респкс это не критично, но
  соответствует смыслу аргумента.
- `time.perf_counter()` оборачивает именно сетевую фазу.

### 5. Формат отчёта — `_format_report`

Возвращает строку (для тестируемости), `main` её печатает. Текстовый формат —
на усмотрение реализации, но ОБЯЗАНЫ присутствовать: `visited`,
`skipped_dedup`, `skipped_robots`, `errors` (глобально), таблица по хостам и
общее время. Предлагаемый вид:

```
politecrawl report
  visited:        <N>
  skipped_dedup:  <N>
  skipped_robots: <N>
  errors:         <N>
  elapsed:        <sec>s

per host:
  <host>  visited=<n> skipped_dedup=<n> skipped_robots=<n> errors=<n>
  ...
```

```python
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
```

- `sorted(stats.per_host)` — детерминированный порядок хостов (важно для
  стабильности тестов).
- `Counter` возвращает `0` для отсутствующего ключа — не нужно проверять
  наличие.

### 6. `main`

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

- Сигнатура **`def main(argv: list[str] | None = None) -> int`** — сохраняется
  из заглушки (совместимость с entry point и `if __name__ == "__main__"`).
- `parse_args(sys.argv[1:] if argv is None else argv)` — при `argv=None`
  argparse взял бы `sys.argv[1:]` сам, но явная передача делает тестирование
  через `main(["http://...", "--max-depth", "1"])` предсказуемым.
- Возвращает `0` всегда (ошибки обхода не роняют процесс — они в отчёте).
  `argparse` при неверных аргументах сам делает `SystemExit(2)` — это ожидаемо.
- Оставить хвост `if __name__ == "__main__":  # pragma: no cover
  raise SystemExit(main())`.

---

## Тесты — `tests/test_cli.py`

Стиль: `from __future__ import annotations`, `import asyncio/httpx/respx`,
импорт из `politecrawl.cli`. Все сетевые тесты — под `@respx.mock`. Тестировать
преимущественно `_run` напрямую (async, под `@respx.mock`), т.к. это возвращает
`CrawlStats` для ассертов; для `main` — 1–2 теста через `capsys`.

Вспомогательный хелпер мини-сайта (внутри теста или модульная функция), мокающий
связанный граф на одном хосте `https://site.test`. Каждой странице — свой respx
route, чтобы проверять `route.call_count`. Обязательно мокать
`https://site.test/robots.txt` (иначе клиент реально пойдёт в сеть за robots —
respx кинет, но лучше явно вернуть 404 → allow-all, либо пустой 200).

Граф для проверки глубины:
- `/` (depth 0) → ссылки на `/a` и `/b`
- `/a` (depth 1) → ссылка на `/deep` и обратно на `/` (цикл!)
- `/b` (depth 1) → ссылка на `/a` (дубль — уже в очереди/посещён)
- `/deep` (depth 2) → ссылка на `/toodeep`
- `/toodeep` (depth 3) — НЕ должна запрашиваться при `max_depth=2`

### Перечень тест-кейсов (имена функций)

1. **`test_argparse_defaults`** — `_build_parser().parse_args(["http://x"])`;
   проверить `seeds == ["http://x"]`, `max_depth == 2`,
   `per_domain_concurrency == 2`, `total_workers == 8`,
   `user_agent == "politecrawl/0.0"`. (Без сети, без `@respx.mock`.)

2. **`test_argparse_overrides`** — передать все флаги
   (`["http://x", "http://y", "--max-depth", "5",
   "--per-domain-concurrency", "3", "--total-workers", "4",
   "--user-agent", "bot/1"]`), проверить, что все атрибуты разобраны,
   `seeds` из двух URL.

3. **`test_crawl_visits_reachable_graph`** (`@respx.mock`) — замокать граф
   выше + robots 404. Прогнать `_run([seed], max_depth=2,
   per_domain_concurrency=2, total_workers=4, user_agent="ua")`. Проверить
   `stats.visited == <число достижимых в пределах depth 2 уникальных
   страниц>` (`/`, `/a`, `/b`, `/deep` = 4). `stats.errors == 0`.

4. **`test_max_depth_cuts_frontier`** (`@respx.mock`) — тот же граф. Повесить
   на `/toodeep` route со счётчиком. После `_run(max_depth=2)` проверить
   `toodeep_route.call_count == 0` (за пределом глубины — не запрашивается) и
   что `/deep` (глубина 2) — запрашивалась (`deep_route.call_count == 1`).

5. **`test_max_depth_zero_only_seeds`** (`@respx.mock`) — `_run(max_depth=0)`
   на `/` со ссылками. Проверить: `stats.visited == 1` (только seed),
   `/a`.call_count == 0, `/b`.call_count == 0 (ссылки не ставятся в очередь).

6. **`test_dedup_counted_not_visited`** (`@respx.mock`) — граф с циклом (`/a`
   ссылается на `/` и `/b` ссылается на `/a`). Проверить, что каждая страница
   запрошена ровно один раз (`route.call_count == 1` на каждую), а повторные
   попадания учтены в `stats.skipped_dedup > 0` и НЕ в `visited`. Цикл
   (`/a`→`/`) не приводит к зависанию / повторному обходу.

7. **`test_robots_disallow_counted`** (`@respx.mock`) — `robots.txt` с
   `Disallow: /private`; seed `/` ссылается на `/private` и `/public`.
   Проверить: `/private` учтён в `stats.skipped_robots >= 1` и НЕ в `visited`;
   `/public` — в `visited`; `route` на `/private` имеет `call_count == 0`
   (robots отсёк ДО загрузки).

8. **`test_network_error_counted_as_error`** (`@respx.mock`) — одна страница
   мокается `side_effect=httpx.ConnectError(...)`, seed ссылается на неё.
   Проверить `stats.errors >= 1`, эта страница НЕ в `visited`, обход не падает
   (исключение поглощено `fetch`).

9. **`test_join_terminates_no_hang`** (`@respx.mock`) — прогнать `_run` на
   небольшом графе под таймаутом, чтобы поймать зависание `queue.join()`:
   `await asyncio.wait_for(_run(...), timeout=5.0)`. Тест зелёный ⇒ `join()`
   не виснет, воркеры корректно отменены. (Также косвенно проверяет
   `task_done()` в `finally`.)

10. **`test_per_host_breakdown_in_stats`** (`@respx.mock`) — граф на одном
    хосте с visited + один disallow + один дубль. Проверить, что
    `stats.per_host["site.test"]["visited"]`, `["skipped_dedup"]`,
    `["skipped_robots"]` соответствуют глобальным (для одного хоста —
    равны глобальным).

11. **`test_seed_disallowed_by_robots`** (`@respx.mock`) — edge case: сам seed
    запрещён robots (`Disallow: /`). Проверить `stats.visited == 0`,
    `stats.skipped_robots == 1`, обход завершается (не виснет).

12. **`test_report_contains_counters`** (`@respx.mock`, `capsys`) — вызвать
    `main([seed, "--max-depth", "1"])`, поймать stdout через `capsys`.
    Проверить, что в выводе присутствуют подстроки `visited`, `skipped_dedup`,
    `skipped_robots`, `errors`, `elapsed` и имя хоста. `main` вернул `0`.

13. **`test_empty_links_page`** (`@respx.mock`) — seed без исходящих ссылок
    (`<html></html>`). Проверить `stats.visited == 1`, обход завершается,
    очередь пустеет (нет зависания).

### Замечания по тестам

- **Мокать `robots.txt` для каждого используемого хоста** — иначе `RobotsCache`
  реально запросит его и respx кинет `ConnectError` (обработается как allow-all,
  но лучше явно вернуть 404/пустой 200, чтобы тест был детерминирован и
  `route.call_count` считал только целевые страницы).
- **respx-роуты — по точному URL** (`respx.get("https://site.test/a")`), чтобы
  `call_count` был осмысленным. HTML отдавать через
  `httpx.Response(200, html="<a href='/a'>a</a>...")`.
- **Относительные ссылки в HTML** абсолютизируются `extract_links` через
  `urljoin(url, href)` — использовать `href='/a'` (от корня хоста), тогда
  нормализация/дедуп сойдутся с точными respx-URL.
- **`asyncio_mode = "auto"`** — на async-тестах `@pytest.mark.asyncio` не
  ставить; `@respx.mock` — ставить.
- Тесты, дёргающие `_run`, а не `main`, не должны сами вызывать
  `asyncio.run` — pytest-asyncio уже даёт event loop (`async def test_*`).

---

## Порядок проверок / критерий готовности

Перед завершением этапа (чеклист SKILL.md):

- `.venv/bin/pytest -q` — весь набор зелёный (существующие + `test_cli.py`).
- `.venv/bin/ruff check .` — чисто (в т.ч. `ASYNC`-правила: нет блокирующих
  вызовов в async, нет `asyncio.run` внутри async).
- `.venv/bin/mypy` — чисто (strict): аннотировать `Counter[str]`, `Queue[...]`,
  все сигнатуры. `_run` возвращает `tuple[CrawlStats, float]`.
- Заглушка `# TODO(Этап 5)` в `cli.py` снята, `print("...заглушка...")` удалён.
- Ручная проверка (опционально, вне тестов, требует сети — НЕ в CI):
  `politecrawl` как команда доступна (entry point), `politecrawl --help`
  печатает 5 аргументов.

## Ключевые инварианты, которые обязаны выполняться

1. `queue.join()` не виснет: каждый воркер вызывает `task_done()` в `finally`.
2. Дедуп предотвращает повторный обход и бесконечный цикл ссылок (шаг 1
   конвейера, `add` атомарен).
3. `max_depth` режет постановку в очередь → страницы за пределом не
   запрашиваются (`route.call_count == 0`).
4. robots отсекает URL ДО загрузки (шаг 2 перед шагом 3–4).
5. Ошибки сети не роняют обход — учитываются в `errors`.
6. Счётчики консистентны без блокировок (single-loop атомарность инкрементов).
