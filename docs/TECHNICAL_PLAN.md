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

## Сквозные принципы

- **Ошибки не роняют обход** — сетевые/парсинг-ошибки логируются и учитываются в отчёте.
- **Backpressure — честный**: медленный домен тормозит только свои задачи (его
  семафор занят), а не весь обход.
- **Детерминированные тесты**: конкурентные инварианты проверяются задачами с
  управляемыми задержками, HTTP — через respx; без обращений в реальный интернет.
- `ruff check` + `mypy` (strict) зелёные на каждом этапе.
