# politecrawl — TECHNICAL_PLAN

## Стек и обоснование выборов

| Область | Выбор | Обоснование |
|---|---|---|
| Язык | Python 3.10 (`/usr/bin/python3`) | Целевой рантайм машины; `X | Y`-типы и `match` доступны. |
| Конкурентность | `asyncio` (stdlib) | Ядро учебной цели проекта. |
| HTTP-клиент | **httpx** (`>=0.27`) | Нативный async `AsyncClient`, чистый API, хорошие type hints (дружит с mypy strict), HTTP/2, connection pooling из коробки. Альтернатива `aiohttp` отклонена: менее строгая типизация, API тяжелее для учебного кода. |
| Парсинг ссылок | stdlib `html.parser` (Этап 1) | Ноль зависимостей для MVP; при необходимости позже можно вынести за интерфейс. `urllib.parse.urljoin`/`urlsplit` для абсолютизации/нормализации. |
| robots.txt | stdlib `urllib.robotparser` как основа | Готовый парсер `RobotFileParser`; оборачиваем в собственный per-domain async-кеш (сам парсер синхронный, загрузку делаем через httpx). |
| Дедуп URL | in-memory `set[str]` нормализованных URL | См. §Этап 4 — обоснование против bloom filter. |
| Тесты | **pytest** + **pytest-asyncio** (`asyncio_mode=auto`) | Стандарт для async-Python. |
| Мок HTTP | **respx** | Мокает именно httpx-транспорт на уровне роутинга запросов, а не monkeypatch. Пара «httpx + respx» — канонична (как «aiohttp + aioresponses»); раз клиент httpx, мок — respx. |
| Линт/формат | **ruff** | Быстрый линтер+форматтер, единый инструмент. |
| Типы | **mypy** (strict) | Ловит ошибки в async-коде до рантайма. |

**Инструменты качества (ruff/mypy) включены уже в Этапе 0** — конфиги в
`pyproject.toml`, гоняются на скелете. Порог входа для последующих этапов —
чистый `ruff check` + `mypy`.

## Структура репозитория

```
politecrawl/
├── pyproject.toml                 # PEP 621, deps, ruff/mypy/pytest конфиг
├── README.md
├── docs/                          # PLAN / TECHNICAL_PLAN / POST_MVP_PLAN
├── src/politecrawl/
│   ├── __init__.py                # __version__
│   ├── fetcher.py                 # Этап 1
│   ├── robots.py                  # Этап 2
│   ├── ratelimit.py               # Этап 3
│   ├── dedup.py                   # Этап 4
│   └── cli.py                     # Этап 5
├── tests/
│   └── test_smoke.py
└── .claude/skills/py-crawler-dev/SKILL.md
```

`src`-layout (пакет в `src/`) — чтобы тесты гоняли установленный пакет
(`pip install -e .`), а не случайный локальный импорт.

---

## Этап 1 — Базовый asyncio-краулер (без politeness)

**Цель:** асинхронно скачать страницу и достать из неё ссылки.

**Модуль:** `fetcher.py`

Тип результата (dataclass, поле `error` отличает успех от ошибки — обход не
роняется):

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class FetchResult:
    url: str                 # запрошенный URL (до редиректов)
    status: int | None       # HTTP-статус; None если запрос не дошёл (сеть/таймаут)
    body: str                # тело ответа (пустая строка при ошибке)
    content_type: str | None # значение заголовка Content-Type (или None)
    error: str | None        # текст ошибки (repr исключения) или None при успехе
```

Функции:

- `async def fetch(client: httpx.AsyncClient, url: str) -> FetchResult` — GET по
  `url`. Любое `httpx.HTTPError` (включая `httpx.TimeoutException`) перехватывается
  и возвращается как `FetchResult(url=url, status=None, body="", content_type=None,
  error=repr(exc))`. HTTP-статусы 4xx/5xx — это **не** ошибка обхода: возвращаются
  как обычный `FetchResult` со своим `status` и пустым `error` (`raise_for_status`
  НЕ вызывается). Редиректы следуются httpx-клиентом (`follow_redirects=True` задаёт
  вызывающий код при создании клиента).
- `def extract_links(base_url: str, html: str) -> list[str]` — парсинг `<a href>`
  через подкласс stdlib `html.parser.HTMLParser` (собирать значения атрибута `href`
  тегов `a`). Каждое значение абсолютизируется через `urllib.parse.urljoin(base_url,
  href)`. Оставляем только результаты со схемой `http`/`https` (проверка через
  `urlsplit(...).scheme`); тем самым отбрасываются `mailto:`, `javascript:`,
  `tel:`, чистые якоря (`#frag` → тот же base, схема http/https — остаётся как есть,
  дедуп на Этапе 4 схлопнёт фрагмент). Порядок сохраняется, дубли на этом этапе
  НЕ убираются (это ответственность `dedup`). Возвращает `list[str]` абсолютных URL.
- Простейший цикл-обход поверх `asyncio.gather` по фронтиру (ещё БЕЗ
  rate-limit/robots/dedup — они добавятся этапами 2–4).

**Тесты (respx):** мок 200/404/redirect; `extract_links` на фикстур-HTML со
всеми видами ссылок — относительные (`/next`, `sub/page`), абсолютные
(`https://other/x`), якорные (`#section`), `mailto:`/`javascript:` (должны
отсеяться); таймаут → `FetchResult` с `status=None` и заполненным `error`.

**Готово когда:** можно скачать seed и получить список исходящих абсолютных ссылок.

---

## Этап 2 — robots.txt: парсинг и per-domain кеш

**Цель:** перед загрузкой URL проверять разрешение в `robots.txt` его домена;
`robots.txt` каждого домена грузится **один раз** и кешируется.

**Модуль:** `robots.py`
- `RobotsCache(client: httpx.AsyncClient)` — держит httpx-клиент и реестр
  `self._parsers: dict[str, urllib.robotparser.RobotFileParser]` по ключу
  `scheme://host` (схема входит в ключ: http и https — разные robots).
  Плюс `self._registry_lock = asyncio.Lock()` (см. ниже).
- `async def allowed(self, url: str, user_agent: str) -> bool`:
  - ключ хоста = `f"{scheme}://{netloc}"` из `urllib.parse.urlsplit(url)` (`netloc`
    включает порт, если он есть);
  - если для ключа ещё нет `RobotFileParser` — лениво скачать `{key}/robots.txt`
    через `self._client.get(...)` и скормить текст в `rfp.parse(text.splitlines())`;
  - отсутствие/ошибка `robots.txt` — HTTP 4xx/5xx **или** `httpx.HTTPError` (сеть/
    таймаут) → трактуем как «разрешено всё»: кладём в кеш `RobotFileParser`, для
    которого `can_fetch` всегда `True` (напр. распарсенный из пустой строки).
    Ошибка загрузки robots НЕ роняет обход;
  - вернуть `rfp.can_fetch(user_agent, url)`.
- Конкурентная защита: параллельные воркеры не должны грузить один и тот же
  `robots.txt` дважды. Применяем **тот же паттерн per-key lock (double-checked
  locking)**, что и в Этапе 3 (см. SKILL.md): под `self._registry_lock` проверяем-
  и-создаём запись кеша по ключу хоста; сам сетевой запрос robots.txt при этом
  выполняется под lock намеренно (запись создаётся ровно один раз) — на разных
  хостах записи независимы, но параллельная загрузка robots РАЗНЫХ хостов
  сериализуется этим единым lock. Для учебного скоупа это приемлемо; если понадобится
  параллельная загрузка robots разных хостов — перейти на per-key `asyncio.Lock`
  (реестр lock'ов), но это усложнение вне MVP.

**Тесты (respx):** мок `robots.txt` с `Disallow`; проверка что запрещённый путь
даёт `False`, разрешённый `True`; 404 robots → всё разрешено; **robots.txt
скачивается ровно один раз** при N параллельных запросах к одному хосту
(счётчик обращений в respx).

**Готово когда:** обход уважает `Disallow`, robots на домен грузится однократно.

---

## Этап 3 — Per-domain rate-limiting (ГЛАВНАЯ ТЕХ-ЗАДАЧА)

**Цель:** ограничить одновременные запросы **к каждому домену** отдельно
(например, ≤2 к домену A и ≤2 к домену B **параллельно**), НЕ сериализуя весь
обход одним глобальным семафором.

**Модуль:** `ratelimit.py`
- `PerDomainLimiter(per_domain: int)`:
  - реестр `self._sems: dict[str, asyncio.Semaphore]`;
  - один общий `self._registry_lock = asyncio.Lock()` **только** для защиты
    ленивого создания семафора (не удерживается на время самого запроса!);
  - `slot(self, domain: str)` — `@asynccontextmanager`, аннотация возврата
    `AsyncIterator[None]` (используется как `async with limiter.slot(domain):`).
    Внутри: достаёт-или-создаёт `Semaphore(per_domain)` под `_registry_lock`
    (double-checked locking), затем **вне** lock делает `async with sem: yield`
    (это и есть точка backpressure к домену). Готовую реализацию см. в
    `.claude/skills/py-crawler-dev/SKILL.md` — этот этап реализуется точно по ней.
    «Домен» здесь — ключ лимита; вызывающий (Этап 5) передаёт хост из URL
    (`urlsplit(url).netloc`).

**Критический инвариант (что тестируем усиленно):**
1. Слоты разных доменов **не блокируют друг друга** — при `per_domain=1` две
   задачи к разным доменам выполняются параллельно, к одному — сериализуются.
2. Одновременность к одному домену **никогда** не превышает `per_domain`
   (счётчик «сейчас активно», фиксируем максимум).
3. Нет гонки при первом обращении к домену из N воркеров одновременно —
   создаётся ровно один семафор (per-key lock защищает `dict`-вставку).

**Почему lock, а не голый `setdefault`:** `dict.setdefault(k, asyncio.Semaphore(n))`
создаёт объект-семафор на каждый вызов ещё до вставки — на «горячем» домене это
плодит лишние объекты, а в более сложных вариантах фабрики (с побочными
эффектами) даёт настоящую гонку. Явный `async with registry_lock` вокруг
«проверить-и-вставить» — предсказуемый и тестируемый паттерн; см. SKILL.md.

**Тесты (детерминированные, без реальной сети):** задачи-заглушки с
контролируемой задержкой (`asyncio.Event`/`sleep`); замер max-concurrency на
домен; проверка параллельности разных доменов; стресс — много воркеров, один
домен, ровно один семафор. Мок HTTP тут вторичен — тестируем сам лимитер.

**Готово когда:** инварианты 1–3 покрыты тестами и зелёные.

---

## Этап 4 — Дедупликация URL

**Цель:** не посещать один и тот же URL дважды; считать `http://a/x` и
`http://a/x#frag` одним URL.

**Модуль:** `dedup.py`
- `normalize(url) -> str`:
  - схема и хост → lower-case;
  - убрать фрагмент (`#...`);
  - убрать дефолтный порт (`:80` для http, `:443` для https);
  - отсортировать query-параметры (стабильный порядок);
  - нормализовать пустой путь → `/`.
- `UrlDedup`: обёртка над `set[str]`; `add(url) -> bool` (True, если URL новый),
  `seen(url) -> bool`.

**Выбор структуры — set, не bloom filter (задокументировано):**
На single-machine скоупе с ограниченной глубиной и seed-листом множество
посещённых URL умещается в памяти. `set` даёт **нулевой false-positive**; bloom
filter экономит память ценой вероятности ложного «уже видели» — а это молча
пропущенная страница, что для учебного корректного краулера неприемлемо. Bloom
filter вынесен в POST_MVP (для сценария распределённого/очень крупного обхода).

**Тесты:** таблица кейсов нормализации (фрагмент/порт/регистр/порядок query);
`add` дважды один URL → второй раз `False`; разные-по-виду-но-эквивалентные URL
схлопываются в один.

**Готово когда:** эквивалентные URL не обходятся повторно.

---

## Этап 5 — Ограничение глубины + CLI + отчёт

**Цель:** собрать всё вместе за CLI, ограничить глубину, напечатать отчёт.

**Модуль:** `cli.py` (+ сборка `Crawler`)

**Аргументы (`argparse`):**

| Аргумент | Тип | Дефолт | Смысл |
|---|---|---|---|
| `seeds` | позиционные, `nargs="+"` | — | один или несколько seed-URL |
| `--max-depth` | `int` | `2` | глубина от seed; seed = глубина 0 |
| `--per-domain-concurrency` | `int` | `2` | лимит одновременных запросов к одному хосту |
| `--total-workers` | `int` | `8` | размер пула воркеров |
| `--user-agent` | `str` | `"politecrawl/0.0"` | UA для запросов и `robots.can_fetch` |

`main(argv)` парсит аргументы и вызывает `asyncio.run(crawl(...))`; возвращает `0`.

**Фронтир и элемент работы:** `asyncio.Queue[tuple[str, int]]` из пар
`(url, depth)`. Seed-URL кладутся с `depth=0`. Ссылки, извлечённые со страницы
глубины `d`, кладутся с `d+1`, только если `d + 1 <= max_depth`.

**Конвейер на один элемент `(url, depth)`** (порядок фиксирован):
1. `dedup.add(url)` → если `False` (уже видели) — учесть `skipped_dedup`, `continue`.
   (Проверка/вставка в один шаг, чтобы два воркера не обошли один URL — `add`
   атомарен в рамках одного event-loop-шага.)
2. `await robots.allowed(url, user_agent)` → если `False` — учесть `skipped_robots`,
   `continue`.
3. `async with limiter.slot(urlsplit(url).netloc):` — занять per-domain слот.
4. `result = await fetcher.fetch(client, url)` — внутри слота.
5. если `result.error is not None` → учесть `errors`, `continue`; иначе учесть
   `visited` (и запомнить `result.status` для отчёта).
6. `for link in fetcher.extract_links(url, result.body):` при `depth+1 <= max_depth`
   — `queue.put_nowait((link, depth+1))` (дедуп-фильтр выполнит шаг 1 у воркера,
   забравшего ссылку).

**Пул воркеров и завершение:** `total_workers` задач-`asyncio.Task`, каждая в
цикле `item = await queue.get()` → конвейер → `queue.task_done()`. Корректный
стоп: `await queue.join()` (ждёт, пока все положенные элементы обработаны), затем
все воркер-таски отменяются (`task.cancel()` + `gather(..., return_exceptions=True)`).
Каждый воркер **обязан** вызвать `task_done()` в `finally` даже при исключении,
иначе `queue.join()` зависнет.

**Счётчики (структура отчёта):** агрегируем `visited`, `skipped_dedup`,
`skipped_robots`, `errors` — глобально и в разбивке по хосту
(`dict[str, Counter]`). Печать в stdout: итоговые числа, таблица по доменам,
общее время (`time.perf_counter()` вокруг обхода). Точный текстовый формат —
на усмотрение реализации, но перечисленные счётчики обязаны присутствовать.

**Тесты (respx):** мок мини-сайта из нескольких связанных страниц; проверка что
`--max-depth` реально режет глубину (страницы за пределом не запрашиваются —
`route.call_count`); отчёт содержит корректные счётчики; end-to-end проход по
фейковому графу; страница с `Disallow` и дубль-ссылка учитываются в
`skipped_robots`/`skipped_dedup`, а не в `visited`.

**Готово когда:** `politecrawl <seed> --max-depth 2` обходит фейк-сайт и печатает отчёт.

---

## Этап 6 — Crawl-delay из robots.txt + адаптивный backoff (POST_MVP)

**Цель:** уважать `Crawl-delay:` из robots.txt (явно заявленный сайтом минимальный
интервал между запросами к домену) и адаптивно увеличивать интервал при ответах
429/5xx или сетевых ошибках — с постепенным затуханием backoff при успешных
ответах. Оба поведения — расширение per-domain politeness, уже заложенной в
Этапах 2-3, а не новая подсистема.

**Модули:** `robots.py` (+1 метод у `RobotsCache`), `ratelimit.py` (расширение
`PerDomainLimiter`), `cli.py` (проброс `crawl_delay` в `slot()` + вызов
`record_response()` после fetch — заодно использует `result.status`, который
Этап 5 вычислял, но не использовал).

### `robots.py` — `RobotsCache.crawl_delay`

```python
async def crawl_delay(self, url: str, user_agent: str) -> float | None:
    """Return the site's declared Crawl-delay for user_agent, if any."""
    rfp = await self._get_parser(_host_key(url))
    return rfp.crawl_delay(user_agent)
```

`urllib.robotparser.RobotFileParser.crawl_delay()` — часть stdlib (Python ≥3.6),
дополнительный парсинг не нужен. Возвращает `None`, если директивы нет.

### `ratelimit.py` — расширение `PerDomainLimiter`

**Новое состояние (в дополнение к `_sems`/`_registry_lock`):**
- `_next_dispatch: dict[str, float]` — `time.monotonic()`-метка, раньше которой
  следующий запрос к домену стартовать не может.
- `_backoff: dict[str, float]` — текущая адаптивная надбавка к интервалу (сек),
  по умолчанию `0.0`.

**`slot(domain, crawl_delay=0.0)`** — сигнатура расширяется необязательным
кварг-параметром `crawl_delay: float = 0.0` (обратная совместимость: старые
вызовы `slot(domain)` продолжают работать, интервал = только backoff, обычно 0).
Тело: после захвата семафора (как сейчас) — вызвать `_wait_for_dispatch(domain,
crawl_delay)` и только затем `yield`.

**`_wait_for_dispatch(domain, crawl_delay)` — БЕЗ ЛОКА, по прецеденту
`UrlDedup.add`/`CrawlStats` (Этапы 4-5):**

```python
async def _wait_for_dispatch(self, domain: str, crawl_delay: float) -> None:
    interval = max(crawl_delay, self._backoff.get(domain, 0.0))
    now = time.monotonic()
    ready_at = self._next_dispatch.get(domain, now)
    wait = max(0.0, ready_at - now)
    # Резервируем следующий слот СИНХРОННО, без await между чтением ready_at
    # и записью ниже — как и в UrlDedup.add/CrawlStats, это делает резервацию
    # атомарной в рамках шага event loop без явного лока: два воркера одного
    # домена не могут получить одинаковый ready_at.
    self._next_dispatch[domain] = max(ready_at, now) + interval
    if wait > 0:
        await asyncio.sleep(wait)
```

- `interval = max(crawl_delay, backoff)` — берётся более строгое из двух
  ограничений, а не сумма.
- Критично: резервация (`_next_dispatch[domain] = ...`) стоит ДО `await
  asyncio.sleep`, а не после — иначе окно между чтением и записью откроет
  гонку, и несколько воркеров одного домена смогут одновременно вычислить
  один и тот же `wait` и стартовать без нужного интервала между собой.

**`record_response(domain, status)` — синхронный метод, вызывается ПОСЛЕ
`fetch()` (уже вне `async with slot(...)`, слот к этому моменту освобождён):**

```python
def record_response(self, domain: str, status: int | None) -> None:
    """Adjust adaptive backoff for domain from one fetch outcome.

    429, 5xx, or a transport error (status=None) doubles the backoff (capped
    at 60s, floored at 1s on first trigger). Any other status halves it back
    toward 0. Independent of _next_dispatch — only affects future intervals.
    """
    current = self._backoff.get(domain, 0.0)
    if status is None or status == 429 or status >= 500:
        self._backoff[domain] = min(60.0, max(1.0, current * 2))
    elif current > 0.01:
        self._backoff[domain] = current / 2
    else:
        self._backoff[domain] = 0.0
```

- Пороги (`1.0` старт, `x2` рост, `60.0` потолок, `/2` спад) — фиксированные
  константы, конфигурируемость через CLI не входит в этот этап.
- Вызывается один раз на fetch, `domain` — тот же `host`, что уже вычисляется
  в `cli.py._process` (`urlsplit(url).netloc` / `_safe_host`).

### `cli.py` — проводка

В `Crawler._process`, шаг 3-4 (было: `async with self._limiter.slot(host):
result = await fetch(...)`):

```python
delay = await self._robots.crawl_delay(url, self._user_agent)
async with self._limiter.slot(host, crawl_delay=delay or 0.0):
    result = await fetch(self._client, url)
self._limiter.record_response(host, result.status)
```

- `crawl_delay` запрашивается у уже прогретого `RobotsCache` (тот же
  `robots.txt`, что грузился на шаге 2 конвейера для `allowed()` — повторного
  сетевого обращения нет, `_get_parser` кеширует).
- `record_response` вызывается ПОСЛЕ выхода из `async with slot(...)` (слот уже
  освобождён — backoff не должен держать семафор занятым дольше самого fetch)
  и ДО `if result.error is not None:` (порядок с существующими шагами 5-6 ТЗ не
  меняется, это дополнительный шаг между 4 и 5).
- `result.status` при транспортной ошибке — `None` (см. `fetcher.FetchResult`);
  `record_response` обрабатывает `None` как повод для backoff.

### Тесты

`tests/test_ratelimit.py` (стиль как у существующих — реальные малые
`asyncio.sleep`, проверка порядка/интервалов, без моков времени):
- `test_crawl_delay_spaces_out_same_domain_requests` — `per_domain=2`,
  `crawl_delay=0.05`; два воркера в `slot(domain, crawl_delay=0.05)`
  одновременно; замерить `time.monotonic()` на входе в тело каждого — разница
  между стартами `>= ~0.05` (с допуском), несмотря на `per_domain=2`
  (конкурентность не блокирует интервал).
- `test_crawl_delay_zero_is_noop` — `crawl_delay=0.0` (дефолт) не меняет
  поведение относительно существующих тестов Этапа 3 (без интервала).
- `test_no_race_on_concurrent_dispatch_reservation` — много воркеров одного
  домена с `crawl_delay`; последовательные старты не ближе `crawl_delay` друг к
  другу ни в одной паре (не только по порядку, но и по времени).
- `test_backoff_doubles_on_429_and_5xx` — вызвать `record_response(domain,
  429)` несколько раз подряд; надбавка растёт (`1.0 -> 2.0 -> 4.0 ...`), не
  превышает `60.0`.
- `test_backoff_doubles_on_network_error` — `record_response(domain, None)` —
  тот же рост, что и на `429`/`5xx`.
- `test_backoff_decays_on_success` — после нескольких `record_response(domain,
  429)` вызвать `record_response(domain, 200)` несколько раз — надбавка падает
  к `0.0`, не уходит в отрицательные значения.
- `test_backoff_affects_next_dispatch_via_slot` — интеграционный: взвинтить
  backoff через `record_response`, затем измерить, что `slot(domain)` (без
  `crawl_delay`) всё равно ждёт из-за backoff.

`tests/test_robots.py`:
- `test_crawl_delay_parsed` — `robots.txt` с `Crawl-delay: 10` → `await
  cache.crawl_delay(url, ua) == 10.0`.
- `test_crawl_delay_absent_is_none` — без директивы → `None`.

`tests/test_cli.py`:
- `test_crawl_delay_from_robots_applied` (`@respx.mock`) — `robots.txt` с
  небольшим `Crawl-delay` (`0.02`–`0.05`, чтобы тест не был медленным), граф из
  2+ страниц одного домена; измерить, что общее время обхода (`elapsed` из
  `_run`) не меньше ожидаемого нижнего порога с учётом интервала между
  запросами (допуск на дрожание расписания event loop).
- `test_5xx_response_triggers_backoff_call` — можно проверить косвенно (через
  `limiter._backoff` в интеграционном тесте) либо через spy/monkeypatch на
  `record_response`, что он реально вызывается с ожидаемым `status`.

### Границы / что НЕ входит в этот этап

- Директива `Sitemap:` из robots.txt (обнаружение и, тем более, автоматическое
  добавление во фронтир) — отдельная фича с другим скоупом (seed discovery, а
  не rate-limiting), намеренно вынесена за рамки Этапа 6. Остаётся в
  `POST_MVP_PLAN.md`.
- Конфигурируемость констант backoff через CLI-флаги — не требуется, фиксированные
  значения в коде.
- Persist backoff/next_dispatch между запусками процесса — не требуется
  (краулер однопроцессный, состояние живёт в памяти одного прогона, как и
  весь остальной rate-limiting).

**Готово когда:** сайт с `Crawl-delay:` в robots.txt обходится с реальным
интервалом между запросами к нему; серия 429/5xx от домена увеличивает
интервал между последующими запросами к этому домену, не влияя на другие
домены.

---

## Этап 7 — Экспорт результатов обхода (POST_MVP)

**Цель:** дать структурированный вывод обхода поверх текстового отчёта Этапа 5.
Три опциональных экспорта, включаемых CLI-флагами (по умолчанию выключены — обход
не зависит от экспорта):
- **граф ссылок** — рёбра «страница → исходящая ссылка» в JSONL или CSV;
- **sitemap-подобный XML** — все успешно посещённые URL;
- **дамп метаданных страниц** — `url`, `status`, `content_type`, `title` в JSONL
  или CSV.

Экспорт — чистая фаза сериализации ПОСЛЕ обхода: `Crawler` копит данные по ходу
конвейера, а запись файлов происходит один раз в конце, из уже собранных
списков. Это отделяет форматирование (без сети/async) от обхода и делает
`export.py` тестируемым без `respx`.

**Модули:** новый `export.py` (чистые функции сериализации), `fetcher.py`
(+`extract_title`), `cli.py` (сбор данных в `Crawler` + флаги + вызов экспорта).

### `fetcher.py` — `extract_title`

`<title>` сейчас нигде не извлекается. Добавляется чистая функция рядом с
`extract_links`, тем же `HTMLParser`-подходом; контракты `extract_links` и
`FetchResult` **не меняются** (title извлекается в `cli.py` из `result.body`, а не
хранится в `FetchResult` — это не трогает существующие места создания
`FetchResult` в тестах fetcher/cli).

```python
def extract_title(html: str) -> str | None:
    """Return the text of the first <title>…</title>, or None if absent.

    Whitespace is collapsed and trimmed. Only the first <title> is used;
    empty or whitespace-only titles yield None.
    """
```

Отдельный `_TitleParser(HTMLParser)`: ловит вход в `<title>` (флаг в
`handle_starttag`), собирает текст в `handle_data`, закрывает на `</title>`. Первый
встреченный title фиксируется; парсинг можно не прерывать (HTMLParser не бросает).

### `export.py` — контракт

Чистые синхронные функции. Принимают уже собранные данные + путь, пишут файл.
Без импорта `httpx`/`asyncio`. Типы данных:

```python
Edge = tuple[str, str]               # (source_url, target_url)
PageMeta = dict[str, str | int | None]  # keys: url, status, content_type, title
```

Публичные функции:

```python
def write_edges(edges: list[Edge], path: str) -> None: ...
def write_pages(pages: list[PageMeta], path: str) -> None: ...
def write_sitemap(urls: list[str], path: str) -> None: ...   # always XML
```

- `write_edges` / `write_pages` выбирают формат **по расширению пути**:
  `.jsonl` → JSON Lines (по объекту на строку), `.csv` → CSV с заголовком.
  Нераспознанное расширение → `ExportFormatError` (см. ниже).
- `write_sitemap` игнорирует расширение (формат всегда XML), но пишет по
  переданному пути.
- **Формат ошибки:** модуль объявляет `class ExportFormatError(ValueError)`.
  `_format_from_path(path) -> str` нормализует суффикс (`.JSONL` → `jsonl`) и
  бросает `ExportFormatError` с человекочитаемым сообщением на нераспознанном.
  `cli.main` ловит `ExportFormatError`, печатает сообщение в `stderr` и
  возвращает код выхода `2` — **никакого traceback пользователю**.
- **JSONL:** `json.dumps(obj, ensure_ascii=False)` на строку, финальный `\n`.
- **CSV:** `csv.DictWriter` со стабильным порядком колонок; `edges` — колонки
  `source,target`; `pages` — `url,status,content_type,title`. `None` → пустая
  ячейка (CSV) / `null` (JSON естественно).
- **Sitemap XML:** корень `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">`,
  на каждый URL `<url><loc>ESC(url)</loc></url>`. `xml.sax.saxutils.escape` на
  `<loc>`. Заголовок `<?xml version="1.0" encoding="UTF-8"?>`.
- Все функции пишут через `open(path, "w", encoding="utf-8", newline="")`
  (для CSV `newline=""` обязателен).

### `cli.py` — сбор данных и проводка

**Сбор в `Crawler`** (новые атрибуты, мутируются в одном event loop без лока — по
прецеденту `CrawlStats`/`UrlDedup`, Этапы 4-5):
- `self.edges: list[Edge]` — на **шаге 6** конвейера, при извлечении ссылок:
  фиксируется пара `(url, link)` для КАЖДОЙ исходящей ссылки, **до** проверки
  глубины/дедупа/robots — это граф ссылок, а не граф обхода. Рёбра пишутся, даже
  если target не будет поставлен в очередь (за пределом глубины) или отсеётся
  дедупом/robots при заборе.
- `self.pages: list[PageMeta]` — на **шаге 5**, когда `result.error is None`:
  `{"url": url, "status": result.status, "content_type": result.content_type,
  "title": extract_title(result.body)}`. Только успешно полученные страницы
  (совпадает с `record_visited`).

**Инвариант рёбер:** `edges` собираются в блоке `if depth + 1 <= self._max_depth`?
**Нет** — вопрос принципиальный. Рёбра фиксируются для всех исходящих ссылок
страницы **независимо от `max_depth`**: даже если ссылки не ставятся в очередь
(за пределом глубины), сам факт «страница ссылается на» — часть графа. То есть
`extract_links(url, result.body)` вызывается всегда при успешном fetch, из него
пишутся рёбра; постановка в очередь под `max_depth` — отдельная ветка на том же
списке ссылок. (Уточнение к существующему шагу 6: раньше `extract_links` звался
только внутри `if depth+1<=max_depth`; теперь — всегда, а условие глубины режет
только `put_nowait`.)

**Флаги** (`_build_parser`, опциональные, дефолт `None` = выключено):
- `--export-edges PATH` — граф ссылок (формат по расширению `.jsonl`/`.csv`);
- `--export-sitemap PATH` — sitemap XML посещённых URL;
- `--export-pages PATH` — метаданные страниц (`.jsonl`/`.csv`).

**Экспорт после обхода** (в `main`, после печати отчёта — или в отдельной функции
`_run_exports(crawler, args)`): для каждого не-`None` флага вызвать
соответствующую `write_*`. `sitemap` берёт `[p["url"] for p in crawler.pages]`
(только реально посещённые). Обёрнуто в `try/except ExportFormatError` →
сообщение в stderr + `return 2`.

`_run` должен возвращать сам `Crawler` (а не только `stats, elapsed`), чтобы
`main` имел доступ к `crawler.edges`/`crawler.pages`. Сигнатура меняется на
`tuple[Crawler, float]`; `main` берёт `crawler.stats` для отчёта — существующий
тест-контракт `_run` затрагивается, проверить `tests/test_cli.py`.

### Тесты

`tests/test_export.py` (новый, **без сети/respx** — чистые функции; читают
записанный файл через `tmp_path`):
- `test_write_edges_jsonl` / `test_write_edges_csv` — формат по расширению,
  содержимое парсится обратно, порядок рёбер сохранён.
- `test_write_pages_jsonl` / `test_write_pages_csv` — все ключи, `None`
  сериализуется корректно (пустая ячейка в CSV / `null` в JSON).
- `test_write_sitemap_xml` — валидный XML, все URL в `<loc>`, спецсимволы
  (`&`, `<`) экранированы.
- `test_unrecognized_extension_raises` — `write_edges(..., "out.txt")` бросает
  `ExportFormatError`.
- `test_extension_case_insensitive` — `.JSONL`/`.CSV` распознаются.

`tests/test_fetcher.py`:
- `test_extract_title_basic` — `<title>Hi</title>` → `"Hi"`.
- `test_extract_title_absent` — нет `<title>` → `None`.
- `test_extract_title_whitespace_collapsed` — многострочный/пробельный title
  сворачивается; пустой → `None`.

`tests/test_cli.py` (`@respx.mock`):
- `test_crawler_collects_edges` — граф со ссылками; `crawler.edges` содержит
  ожидаемые пары `(source, target)`, включая ссылки за пределом `max_depth`.
- `test_crawler_collects_pages_with_title` — страницы с `<title>`;
  `crawler.pages` содержит `url/status/content_type/title` посещённых.
- `test_export_flags_write_files` — `main([seed, "--export-edges", edges_path,
  "--export-pages", pages_path, "--export-sitemap", sitemap_path])` через
  `tmp_path`; файлы созданы и содержат данные.
- `test_export_bad_extension_exits_nonzero` — `main([..., "--export-edges",
  "x.txt"])` возвращает `2`, сообщение в stderr (через `capsys`), без traceback.
- `test_no_export_flags_writes_nothing` — без флагов файлы не создаются, поведение
  обхода не меняется.

### Границы / что НЕ входит

- Не менять `FetchResult` и `extract_links` — title извлекается отдельной чистой
  функцией из `result.body`, хранится только в `crawler.pages`.
- Не потоковый экспорт: файлы пишутся один раз в конце из накопленных списков
  (для single-machine скоупа с ограниченной глубиной память не проблема — как и
  `set` в дедупе, Этап 4).
- Sitemap — только `<loc>` (без `<lastmod>`/`<priority>`/`<changefreq>`): у
  краулера нет этих данных, добавлять пустые теги смысла нет.
- Дедуп рёбер не делается: граф ссылок отражает страницу как есть (та же ссылка
  дважды на странице = два ребра), это соответствует `extract_links`, который
  сохраняет дубли.

**Готово когда:** обход с `--export-edges out.jsonl --export-pages pages.csv
--export-sitemap sitemap.xml` пишет три корректных файла (граф рёбер, метаданные
с извлечёнными title, sitemap посещённых URL); без флагов ничего не пишется и
обход не меняется; путь с нераспознанным расширением даёт понятную ошибку и
ненулевой код выхода, а не traceback.

---

## Этап 8 — Sitemap: из robots.txt (POST_MVP)

**Цель:** уважать директиву `Sitemap:` в `robots.txt`. Обнаруживать sitemap-URL,
объявленные в `robots.txt` домена, скачивать sitemap XML, извлекать из него
страничные URL и добавлять их в очередь обхода как **дополнительный источник
ссылок** — сверх того, что находится через `<a href>` на самих страницах. Это
даёт краулеру страницы, на которые нигде нет внутренних ссылок, но которые сайт
сам объявил в sitemap.

`Crawl-delay:` из той же группы POST_MVP уже реализован в Этапе 6
(`RobotsCache.crawl_delay`). Здесь закрывается вторая половина пункта —
`Sitemap:`.

**Ключевой факт о stdlib:** `urllib.robotparser.RobotFileParser` уже парсит
директивы `Sitemap:` и отдаёт их через `.site_maps() -> list[str] | None`
(с Python 3.8; проект на 3.10). `RobotsCache._load_parser` уже вызывает
`rfp.parse(response.text.splitlines())`, то есть sitemaps **уже распарсены** в
существующем кеше парсеров — дополнительный сетевой запрос `robots.txt` не нужен,
как и для `crawl_delay`.

**Модули:** `robots.py` (+`sitemaps()`), `fetcher.py`
(+`extract_sitemap_urls` — чистый парсер XML рядом с `extract_links`/
`extract_title`), `cli.py` (обнаружение и загрузка sitemap в `Crawler`,
+счётчик `CrawlStats.sitemap_urls`).

### `robots.py` — `RobotsCache.sitemaps`

Новый async-метод, тем же паттерном, что `crawl_delay`: использует уже
закешированный парсер (`_get_parser`), никакого дополнительного сетевого
round-trip для хоста, чей `robots.txt` уже загружен. Контракты `allowed` и
`crawl_delay` **не меняются** — существующие тесты `tests/test_robots.py` не
затрагиваются.

```python
async def sitemaps(self, url: str, user_agent: str) -> list[str]:
    """Return sitemap URLs declared in the host's robots.txt (may be empty).

    Uses the same cached parser as allowed()/crawl_delay(): no extra network
    round-trip. site_maps() ignores user_agent (Sitemap: is a global
    directive, not per-agent); the parameter is kept for call-site symmetry
    with allowed()/crawl_delay().
    """
    rfp = await self._get_parser(_host_key(url))
    return rfp.site_maps() or []
```

- `site_maps()` возвращает `list[str] | None` (None = директив нет) → `or []`
  нормализует в пустой список.
- `user_agent` не используется парсером (`Sitemap:` — глобальная директива), но
  оставлен в сигнатуре ради единообразия с `allowed`/`crawl_delay` — все три
  вызываются из `_process` одинаково.

### `fetcher.py` — `extract_sitemap_urls`

Чистая функция парсинга sitemap XML рядом с `extract_links`/`extract_title` —
симметрия: все три извлекают структуру из тела ответа. Парсинг через
`xml.etree.ElementTree` (stdlib), а НЕ `HTMLParser`: sitemap — строгий XML с
namespace `http://www.sitemaps.org/schemas/sitemap/0.9`, тот же формат, что
пишет `export.write_sitemap`.

```python
def extract_sitemap_urls(xml: str) -> tuple[list[str], list[str]]:
    """Parse a sitemap XML, returning (page_urls, nested_sitemap_urls).

    Handles both sitemaps.org 0.9 document types:
      - <urlset>:      <url><loc> entries -> page_urls
      - <sitemapindex>: <sitemap><loc> entries -> nested_sitemap_urls
    Namespace-agnostic (matches by local tag name, tolerating a missing or
    unexpected xmlns). Malformed XML yields ([], []) — a bad sitemap must not
    crash the crawl. Only http/https <loc> values are kept.
    """
```

- **Возврат — кортеж двух списков**: страничные URL (`<urlset>/<url>/<loc>`) и
  вложенные sitemap-URL (`<sitemapindex>/<sitemap>/<loc>`). Так `Crawler` сам
  решает, что ставить в очередь обхода (страницы), а что — догрузить как
  дочерний sitemap (см. решение по `<sitemapindex>` ниже).
- **Namespace-agnostic**: сопоставление по локальному имени тега
  (`tag.rsplit("}", 1)[-1]`), чтобы не ломаться на sitemap без объявленного
  xmlns или с нестандартным префиксом. Это устойчивее, чем хардкод namespace.
- **Битый XML → `([], [])`**: `ET.ParseError` перехватывается; кривой sitemap
  учитывается как «ничего не нашли», а не роняет обход (сквозной принцип
  «ошибки не роняют обход»).
- **Фильтр схем**: только `http`/`https` в `<loc>` (как `extract_links`).

### `cli.py` — обнаружение и загрузка sitemap в `Crawler`

**Когда:** при обработке seed-URL (`depth == 0`) в `_process`, **после** того как
seed прошёл robots-проверку (шаг 2) — не тратить sitemap-загрузку на хост,
который сам запрещён. Sitemap-обнаружение выполняется **один раз на хост за
обход**.

**Дедуп на уровне хоста:** новый атрибут `Crawler._sitemap_hosts: set[str]` —
множество хостов, для которых обнаружение sitemap уже запущено. Атомарная
проверка-и-вставка (`host in set` → `set.add`) без `await` между чтением и
записью, тот же лок-фри прецедент, что `UrlDedup.add`/`edges`/`pages`. Первый
воркер, дошедший до seed данного хоста, «застолбит» sitemap-обнаружение; воркеры
других URL того же хоста (депт > 0) его не повторяют.

**Отдельный дедуп самих sitemap-URL:** `Crawler._fetched_sitemaps: set[str]` —
чтобы не скачивать один и тот же sitemap дважды (важно при `<sitemapindex>`,
ссылающемся на общие дочерние sitemap, и при пересечении по хостам).

**Как загружается sitemap:** каждый sitemap-URL скачивается тем же `fetch()` и
под тем же per-domain `limiter.slot()`, что обычные страницы — это тоже сетевой
запрос к домену, политесс (concurrency-cap + crawl-delay + backoff) обязан
применяться. Загрузка через вспомогательный метод
`Crawler._discover_sitemaps(seed_url, host)`:

1. `sitemap_urls = await self._robots.sitemaps(seed_url, self._user_agent)`.
2. Для каждого sitemap-URL (пропуская уже бывшие в `_fetched_sitemaps`):
   - `crawl_delay = await self._robots.crawl_delay(sitemap_url, UA)` (тот же
     warm cache);
   - `async with self._limiter.slot(sm_host, crawl_delay=delay or 0.0):
     result = await fetch(self._client, sitemap_url)`;
   - `self._limiter.record_response(sm_host, result.status)` после слота;
   - при `result.error is None`: `pages, nested = extract_sitemap_urls(result.body)`;
   - страничные URL кладутся в очередь как `(page_url, 0)` (депт-0, см. ниже) и
     считаются в `stats.sitemap_urls`;
   - вложенные sitemap-URL (`<sitemapindex>`) — **один уровень**: догружаются тем
     же способом в пределах этого же вызова (не рекурсивно вглубь второго
     индекса), см. границы.

**С какой глубиной ставить страничные URL из sitemap: `depth = 0`.** Обоснование:
sitemap — это объявленный сайтом список канонических точек входа, семантически
это дополнительные seed-подобные URL, а не «ссылки, найденные на странице». Депт-0
позволяет обойти их собственные исходящие ссылки на полную `max_depth` — иначе
страницы, известные только из sitemap, дали бы обход на один уровень мельче seed.
Все они всё равно проходят обычный конвейер `_process` (дедуп → robots → fetch),
так что депт-0 не создаёт обхода мимо политесса.

**Sitemap-URL идут через обычный конвейер:** извлечённые страничные URL кладутся в
`self._queue` как любые другие и на заборе проходят `dedup.add` → `robots.allowed`
→ rate-limited fetch. Для них НЕ создаётся отдельного пути мимо дедупа/robots.

**Интеграция в `_process`:** после успешной robots-проверки seed (депт-0), перед
или параллельно основному fetch seed, вызвать обнаружение — один раз на хост:

```python
# 2b. sitemap discovery (Stage 8): once per host, only for depth-0 seeds that
# passed robots. Enqueues sitemap-declared page URLs as additional depth-0
# entry points. Guarded so it runs once per host across all workers.
if depth == 0 and host not in self._sitemap_hosts:
    self._sitemap_hosts.add(host)
    await self._discover_sitemaps(url, host)
```

`_discover_sitemaps` сам не роняет `_process`: обёрнут так, что ошибки
загрузки/парсинга sitemap учитываются (через `fetch()`/`extract_sitemap_urls`,
которые не бросают), а не срывают обход seed.

**Счётчик отчёта:** да, новый — `CrawlStats.sitemap_urls` (общий + per-host через
`_bump`) и `record_sitemap_url(host)`. Считает страничные URL, **обнаруженные**
в sitemap и поставленные в очередь (не обязательно посещённые — часть отсеётся
дедупом/robots на заборе; это отражает вклад sitemap как источника, что и есть
цель наблюдаемости). Печатается в `_format_report` строкой
`sitemap_urls: N` и в per-host разбивке.

### Тесты

`tests/test_robots.py` (дополнить, `@respx.mock`):
- `test_sitemaps_parsed` — `robots.txt` с двумя `Sitemap:` строками; `sitemaps()`
  возвращает оба URL в порядке объявления.
- `test_sitemaps_absent_is_empty` — `robots.txt` без `Sitemap:`; `sitemaps()`
  возвращает `[]` (не `None`).
- `test_sitemaps_uses_cached_parser` — после `allowed()` вызвать `sitemaps()` на
  том же хосте; `route.call_count == 1` (нет второго запроса robots.txt).

`tests/test_fetcher.py` (дополнить, **без сети/respx** — чистая функция):
- `test_extract_sitemap_urls_urlset` — `<urlset>` с двумя `<loc>`; вернулись
  `(["https://x/a", "https://x/b"], [])`.
- `test_extract_sitemap_urls_index` — `<sitemapindex>` с двумя `<sitemap><loc>`;
  вернулись `([], [sm1, sm2])`.
- `test_extract_sitemap_urls_malformed` — не-XML строка → `([], [])`, не бросает.
- `test_extract_sitemap_urls_no_namespace` — `<urlset>` без `xmlns`; всё равно
  извлекает `<loc>` (namespace-agnostic).
- `test_extract_sitemap_urls_filters_non_http` — `<loc>` с `ftp://…` отброшен.
- `test_extract_sitemap_urls_empty` — пустой `<urlset/>` → `([], [])`.

`tests/test_cli.py` (дополнить, `@respx.mock`; мокать `/robots.txt` и sitemap-URL):
- `test_sitemap_urls_discovered_and_enqueued` — `robots.txt` объявляет
  `/sitemap.xml`; sitemap отдаёт две страницы, ни на одну нет `<a href>` с других
  страниц; после обхода обе посещены (`stats.visited` их включает),
  `stats.sitemap_urls == 2`.
- `test_sitemap_fetched_once_per_host` — несколько seed одного хоста (или
  граф с несколькими страницами хоста); `robots.txt`-route и `sitemap.xml`-route
  каждый `call_count == 1` (обнаружение один раз на хост).
- `test_sitemap_index_one_level` — `robots.txt` → sitemap-index → два дочерних
  sitemap → страницы; страницы из дочерних sitemap обойдены.
- `test_sitemap_urls_go_through_robots` — sitemap объявляет URL, запрещённый
  `robots.txt` (Disallow); URL считается в `sitemap_urls` (обнаружен), но при
  заборе отсеётся robots (`skipped_robots`), не посещён — sitemap-URL идут через
  обычный конвейер, не мимо него.
- `test_sitemap_absent_no_effect` — `robots.txt` без `Sitemap:`; обход как
  раньше, `stats.sitemap_urls == 0`, sitemap-запросов нет.
- `test_malformed_sitemap_does_not_crash` — sitemap-route отдаёт мусор; обход
  завершается, seed посещён, `sitemap_urls == 0`.

### Границы / что НЕ входит

- **`<sitemapindex>` — только ОДИН уровень.** Индекс, ссылающийся на дочерние
  sitemap, обрабатывается: дочерние sitemap догружаются и их страничные URL
  ставятся в очередь. Но индекс, ссылающийся на другой индекс (второй уровень
  вложенности), **не** раскрывается рекурсивно — на практике не встречается, а
  неограниченная рекурсия по индексам — источник циклов и разрастания. Явно
  вынесено как несделанное: **более одного уровня вложенности sitemap-index —
  POST_MVP**.
- **Gzip-сжатые sitemap (`.xml.gz`) не поддерживаются.** `fetch()` отдаёт
  `response.text`, а `.gz`-тело — бинарь, не текст; распаковка потребовала бы
  ветки по Content-Type/суффиксу и работы с байтами. Многие сайты отдают и
  несжатый XML; gzip вынесен в POST_MVP.
- **`<lastmod>`/`<changefreq>`/`<priority>` игнорируются** — краулеру нужен
  только `<loc>` (список URL). Это симметрично `write_sitemap`, который тоже
  пишет только `<loc>`.
- **robots.txt повторно НЕ качается** — sitemaps берутся из уже загруженного
  парсера (тот же warm cache, что `allowed`/`crawl_delay`).
- **Публичные контракты `robots`/`ratelimit`/`dedup`/`fetch`/`FetchResult` не
  меняются** — добавляются только новые функции/методы. `sitemaps()` —
  аддитивный метод на `RobotsCache`, `extract_sitemap_urls` — новая функция в
  `fetcher.py`.

**Готово когда:** обход seed-хоста, чей `robots.txt` объявляет `Sitemap:`,
скачивает объявленные sitemap под per-domain-политессом, извлекает страничные
URL (включая один уровень `<sitemapindex>`) и ставит их в очередь как депт-0;
эти URL проходят обычный дедуп/robots-конвейер; отчёт показывает
`sitemap_urls: N`; хост, чей `robots.txt` не объявляет sitemap, обходится как
прежде без лишних запросов; sitemap скачивается один раз на хост; битый или
gzip-sitemap не роняет обход.

---

## Сквозные принципы

- **Ошибки не роняют обход** — сетевые/парсинг-ошибки логируются и учитываются в отчёте.
- **Backpressure — честный**: медленный домен тормозит только свои задачи (его
  семафор занят), а не весь обход.
- **Детерминированные тесты**: конкурентные инварианты проверяются задачами с
  управляемыми задержками, HTTP — через respx; без обращений в реальный интернет.
- `ruff check` + `mypy` (strict) зелёные на каждом этапе.
