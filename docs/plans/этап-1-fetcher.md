# План реализации — Этап 1: Базовый asyncio-краулер (`fetcher.py`)

## 0. Контекст и границы этапа

Ты реализуешь **Этап 1** проекта politecrawl — асинхронного веб-краулера на Python 3.10 / asyncio. Цель этапа: **асинхронно скачать страницу и извлечь из неё исходящие абсолютные ссылки**. Это два строительных блока — `fetch` и `extract_links` — плюс тесты на них.

Стек (обязательно соблюдать, не отклоняться):
- HTTP-клиент — **httpx** (`AsyncClient`), НЕ aiohttp.
- Парсинг ссылок — **stdlib** `html.parser.HTMLParser` + `urllib.parse.urljoin`/`urlsplit`. Никаких сторонних парсеров (bs4/lxml) — на Этапе 1 ноль новых зависимостей.
- Тесты — **pytest** + **pytest-asyncio** в режиме `asyncio_mode = "auto"` (задан в `pyproject.toml`; отдельный `@pytest.mark.asyncio` на async-тест НЕ нужен).
- HTTP-моки в тестах — **respx** (мокает httpx-транспорт). Реальных сетевых обращений в тестах быть НЕ должно.
- Линт/типы — **ruff** + **mypy strict**; оба должны быть зелёными.
- Код, идентификаторы и комментарии в коде — на **английском**.

Все зависимости уже объявлены в `pyproject.toml` (`httpx>=0.27` в runtime; `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy` в `dev`). Ставить ничего нового не нужно. Предполагается, что окружение уже создано (`.venv` с `pip install -e ".[dev]"`); если нет — создать по командам из `CLAUDE.md`.

## 1. Файлы для создания/изменения

Относительно корня репозитория `/home/alkom/Projects/ai-projects/politecrawl`:

| Файл | Действие |
|---|---|
| `src/politecrawl/fetcher.py` | **Изменить** — заменить заглушку (докстринг + два `# TODO(Этап 1)`) на реальную реализацию `FetchResult`, `fetch`, `extract_links`. |
| `tests/test_fetcher.py` | **Создать** — новый файл с тестами на `fetch` и `extract_links`. |

**Не трогать** ничего другого: `robots.py`, `ratelimit.py`, `dedup.py`, `cli.py`, `__init__.py`, `pyproject.toml`, `tests/test_smoke.py` — вне скоупа этого этапа. Версию пакета (`0.0.0`) не менять.

## 2. Решение по «простейшему циклу обхода» (важно — прочитать до кода)

TECHNICAL_PLAN.md в описании Этапа 1 упоминает «простейший цикл-обход поверх `asyncio.gather` по фронтиру (ещё БЕЗ rate-limit/robots/dedup)». **Решение: НЕ добавлять отдельную функцию цикла обхода в `fetcher.py` на этом этапе.** Обоснование:

- Полноценный цикл обхода — это ответственность **Этапа 5** (`cli.py` + сборка `Crawler`): там появляются `asyncio.Queue[tuple[str,int]]`, пул воркеров, ограничение глубины, дедуп, robots, rate-limit, счётчики и корректное завершение (`queue.join()` + отмена тасок). Любая функция-цикл, написанная сейчас в `fetcher.py`, была бы либо выброшена, либо переписана на Этапе 5 — это мёртвый код и ложный API-контракт.
- `fetch` и `extract_links` самодостаточны как строительные блоки: их композиция (скачать seed → извлечь ссылки) полностью покрывается acceptance-тестом (см. §7), который вызывает `fetch`, затем `extract_links` на теле ответа. Демонстрация `asyncio.gather` по нескольким URL тоже делается в тесте, а не в production-коде.
- Формулировка «Готово когда: можно скачать seed и получить список исходящих абсолютных ссылок» доказывается **интеграционным тестом** `test_fetch_then_extract_links_end_to_end` (§4, п. E), а не отдельной функцией в модуле.

Итог: `fetcher.py` экспортирует ровно три публичных имени — `FetchResult`, `fetch`, `extract_links`. «Цикл обхода» на Этапе 1 существует только в виде теста-демонстрации.

## 3. Реализация `src/politecrawl/fetcher.py`

Сохранить существующий модульный докстринг (первые строки файла — он корректен) и `from __future__ import annotations`. Удалить обе строки `# TODO(Этап 1): ...`. Дальше — реализация.

### 3.1. Импорты (верх файла)

Нужны: `from dataclasses import dataclass`; `from html.parser import HTMLParser`; `from urllib.parse import urljoin, urlsplit`; `import httpx`. Порядок импортов приведи в соответствие с ruff-правилом `I` (isort): сначала stdlib (`dataclasses`, `html.parser`, `urllib.parse`), затем сторонние (`httpx`), с пустой строкой между группами. `from __future__ import annotations` идёт самым первым импортом (уже есть в файле).

### 3.2. Dataclass `FetchResult`

Точно по спецификации TECHNICAL_PLAN.md — `@dataclass(frozen=True)`, поля в этом порядке и с этими типами:

```python
@dataclass(frozen=True)
class FetchResult:
    url: str                 # requested URL (before redirects)
    status: int | None       # HTTP status; None if request never completed (network/timeout)
    body: str                # response body ("" on error)
    content_type: str | None # value of Content-Type header (or None)
    error: str | None        # repr() of exception, or None on success
```

Комментарии — на английском (как показано). `frozen=True` обязателен.

### 3.3. `async def fetch(client, url) -> FetchResult`

Сигнатура: `async def fetch(client: httpx.AsyncClient, url: str) -> FetchResult`.

Поведение:
1. Выполнить GET: `response = await client.get(url)` внутри `try`.
2. **НЕ вызывать** `response.raise_for_status()`. HTTP-статусы 4xx/5xx — это НЕ ошибка обхода: они возвращаются как нормальный `FetchResult` со своим `status` и `error=None`.
3. При успехе (запрос дошёл, любой статус) вернуть:
   - `url=url` (исходный запрошенный URL, до редиректов — именно аргумент, не `response.url`);
   - `status=response.status_code`;
   - `body=response.text` (httpx декодирует по заголовкам/charset);
   - `content_type=response.headers.get("content-type")` — вернёт `str` или `None`, если заголовка нет. Ключ регистронезависим в httpx-`Headers`, брать его строчными буквами;
   - `error=None`.
4. Ловить **только** `httpx.HTTPError` (это базовый класс httpx для сетевых ошибок и таймаутов, включая `httpx.TimeoutException` и `httpx.ConnectError`). При перехвате вернуть:
   - `url=url`, `status=None`, `body=""`, `content_type=None`, `error=repr(exc)`.

   Ловить именно `httpx.HTTPError`, а не голый `Exception` — чтобы не глотать программные ошибки (баги) и `CancelledError`. `httpx.TimeoutException` — подкласс `httpx.HTTPError`, отдельного `except` для него не нужно.

Редиректы: `follow_redirects` НЕ задаётся внутри `fetch` — это забота вызывающего кода при создании `AsyncClient` (`httpx.AsyncClient(follow_redirects=True)`). В `fetch` просто делается `client.get(url)`; если клиент настроен на follow, httpx проследует редирект сам и вернёт финальный ответ.

### 3.4. `def extract_links(base_url, html) -> list[str]`

Сигнатура: `def extract_links(base_url: str, html: str) -> list[str]` (синхронная — парсинг CPU-bound, без await).

Реализация через вложенный/модульный подкласс `HTMLParser`:

- Определить класс-парсер (можно локально внутри функции или на уровне модуля — предпочтительно на уровне модуля, имя `_LinkParser`, приватное с подчёркиванием). Он накапливает найденные значения `href` в списке-атрибуте.
- В `__init__`: вызвать `super().__init__()` и завести `self.hrefs: list[str] = []`.
- Переопределить метод `handle_starttag`. **Точная сигнатура из stdlib (важно для mypy strict):**
  ```python
  def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
  ```
  Логика: если `tag != "a"` — `return`. Иначе пройти по `attrs`; для пары `(name, value)` где `name == "href"` и `value` — непустая строка (`value` может быть `None` или `""` — оба пропускаем), добавить `value` в `self.hrefs`. Достаточно взять первый `href` в теге (у валидного `<a>` он один), но простой проход по всем парам с проверкой имени тоже корректен.
- В `extract_links`: создать экземпляр парсера, вызвать `parser.feed(html)`, затем итерировать по `parser.hrefs`.

Абсолютизация и фильтрация (порядок сохраняется, дедуп НЕ делается):

Для каждого сырого `href`:
1. `absolute = urljoin(base_url, href)` — превращает относительные (`/next`, `sub/page`, `../up`) в абсолютные относительно `base_url`; абсолютные (`https://other/x`) оставляет как есть; чистый якорь `#frag` превращает в `base_url#frag` (схема остаётся http/https).
2. Проверить схему: `if urlsplit(absolute).scheme in {"http", "https"}:` — только тогда добавить в результат. Это отсекает `mailto:`, `javascript:`, `tel:` (после `urljoin` их схема остаётся `mailto`/`javascript`/`tel`, а не http).
3. Порядок появления в HTML сохраняется. Дубликаты **не** убираются (это ответственность `dedup.py`, Этап 4). Возвращаемый список может содержать повторы.

Вернуть `list[str]` абсолютных URL.

**Пограничные случаи, которые должны обрабатываться корректно (покрыты тестами):**
- `<a>` без атрибута `href` вообще → пропускается (в `attrs` нет пары с именем `href`).
- `href=""` (пустая строка) → пропускается на этапе сбора (пустое значение не добавляем в `hrefs`), чтобы `urljoin(base, "")` не дал сам base_url как «ссылку».
- `href="#section"` → `urljoin` даёт `base#section`, схема http/https → **остаётся** в результате (фрагмент схлопнётся дедупом на Этапе 4, здесь его не трогаем).
- `mailto:`, `javascript:`, `tel:` → отфильтрованы по схеме.

## 4. Тесты — `tests/test_fetcher.py`

Новый файл. Шапка: докстринг на русском (одна строка, напр. «Тесты Этапа 1: fetch() и extract_links()»), `from __future__ import annotations`, импорты: `import httpx`, `import respx`, и `from politecrawl.fetcher import FetchResult, extract_links, fetch`.

Соглашения по тестам:
- Async-тесты — обычные `async def test_...`, **без** `@pytest.mark.asyncio` (режим auto).
- HTTP-тесты декорируются `@respx.mock` и создают клиент внутри: `async with httpx.AsyncClient(follow_redirects=True) as client: ...`. Для теста, который проверяет follow редиректа, `follow_redirects=True` обязателен; для остальных — можно тоже ставить, для единообразия.
- Реальной сети быть не должно — всё через respx.

### A. Тесты `fetch` (async, respx)

**`test_fetch_ok`** — мок 200:
- `respx.get("https://example.com/").mock(return_value=httpx.Response(200, html="<a href='/next'>x</a>", headers={"content-type": "text/html; charset=utf-8"}))`. (Хелпер `html=` у `httpx.Response` сам проставит content-type `text/html`, но задать заголовок явно надёжнее для assert по `content_type`.)
- `result = await fetch(client, "https://example.com/")`.
- Asserts: `result.status == 200`; `result.error is None`; `"next" in result.body` (или `result.body == "<a href='/next'>x</a>"`); `result.content_type is not None and result.content_type.startswith("text/html")`; `result.url == "https://example.com/"`.

**`test_fetch_404_is_not_crawl_error`** — мок 404:
- `respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))`.
- Asserts: `result.status == 404`; `result.error is None` (ключевой инвариант — 4xx НЕ ошибка обхода); `result.body == ""` или тело, которое отдал мок (если тело не задавали — пустая строка).

**`test_fetch_follows_redirect`** — мок 301/302 → 200:
- Замокать два роута: `respx.get("https://example.com/old").mock(return_value=httpx.Response(301, headers={"location": "https://example.com/new"}))` и `respx.get("https://example.com/new").mock(return_value=httpx.Response(200, html="ok"))`.
- Клиент создать с `follow_redirects=True`.
- `result = await fetch(client, "https://example.com/old")`.
- Asserts: `result.status == 200` (финальный статус после follow); `result.error is None`; `"ok" in result.body`. Поле `result.url` при этом равно исходному `"https://example.com/old"` (проверить — это фиксирует контракт «url = запрошенный до редиректов»).

**`test_fetch_timeout_returns_error_result`** — таймаут → не ошибка обхода, а `FetchResult` с ошибкой:
- `respx.get("https://example.com/slow").mock(side_effect=httpx.TimeoutException("timed out"))`.
- `result = await fetch(client, "https://example.com/slow")`.
- Asserts: `result.status is None`; `result.error is not None`; `"Timeout" in result.error` (в `repr` исключения будет имя класса `TimeoutException`; проверяй по подстроке `"Timeout"`); `result.body == ""`; `result.content_type is None`; `result.url == "https://example.com/slow"`.
- Здесь важно, что `fetch` **не пробросил** исключение — сам факт, что тест дошёл до assert без `raise`, это доказывает.

(Опционально, для полноты — можно добавить аналогичный `test_fetch_connect_error` с `side_effect=httpx.ConnectError("refused")` и теми же assert: `status is None`, `error` заполнен. Не обязателен, но дёшев и усиливает покрытие ветки `except httpx.HTTPError`.)

### B. Тесты `extract_links` (синхронные, без respx)

Все — обычные `def test_...` (не async). Используют одну общую фикстур-строку HTML или локальные строки в каждом тесте.

**`test_extract_links_relative_are_absolutized`**:
- HTML с `<a href="/next">`, `<a href="sub/page">`, `<a href="../up">`; `base_url="https://example.com/dir/"`.
- Assert, что в результате присутствуют `"https://example.com/next"`, `"https://example.com/dir/sub/page"`, `"https://example.com/up"` (проверь точные значения через `urljoin`-семантику: относительный `/next` от base с путём `/dir/` даёт `/next`; `sub/page` даёт `/dir/sub/page`; `../up` даёт `/up`).

**`test_extract_links_absolute_kept`**:
- HTML с `<a href="https://other.example/x">`; любой base.
- Assert: `"https://other.example/x" in result`.

**`test_extract_links_anchor_kept_with_base`**:
- HTML с `<a href="#section">`; `base_url="https://example.com/page"`.
- Assert: `"https://example.com/page#section" in result` (якорь остаётся, схема http → не фильтруется; фрагмент не срезается на Этапе 1).

**`test_extract_links_non_http_schemes_filtered`**:
- HTML с `<a href="mailto:a@b.com">`, `<a href="javascript:void(0)">`, `<a href="tel:+123">`.
- Assert: результат НЕ содержит ни одного из этих значений; для чистоты — `assert result == []` (если в HTML только эти три ссылки).

**`test_extract_links_empty_and_missing_href_skipped`**:
- HTML с `<a href="">empty</a>`, `<a>no href</a>`, и один валидный `<a href="/ok">`.
- Assert: `result == ["https://example.com/ok"]` (при `base_url="https://example.com/"`) — пустой href и тег без href пропущены, `urljoin(base, "")` НЕ попал в результат как псевдо-ссылка.

**`test_extract_links_preserves_order_and_duplicates`**:
- HTML с последовательностью, например `<a href="/a"><a href="/b"><a href="/a">`.
- Assert: `result == ["https://example.com/a", "https://example.com/b", "https://example.com/a"]` — порядок сохранён, дубликат `/a` НЕ схлопнут (дедуп — Этап 4).

### E. Интеграционный тест (доказывает «Готово когда»)

**`test_fetch_then_extract_links_end_to_end`** (async, respx):
- Замокать `respx.get("https://example.com/").mock(return_value=httpx.Response(200, html='<a href="/page1">1</a><a href="https://other.example/x">2</a><a href="mailto:z@z">3</a>'))`.
- `result = await fetch(client, "https://example.com/")`; assert `result.error is None`.
- `links = extract_links("https://example.com/", result.body)`.
- Asserts: `links == ["https://example.com/page1", "https://other.example/x"]` — скачали seed, получили список исходящих **абсолютных** ссылок, относительная абсолютизирована, внешняя сохранена, `mailto:` отфильтрован. Это прямая проверка acceptance-критерия Этапа 1.

(Опционально — мини-демонстрация `asyncio.gather` по двум seed-URL: замокать два роута, сделать `await asyncio.gather(fetch(client, u1), fetch(client, u2))`, проверить, что оба вернули `status==200`. Не обязателен для acceptance, но иллюстрирует конкурентный fetch. Если добавляешь — `import asyncio` вверху.)

## 5. Порядок реализации (чтобы `pytest -q` шёл по нарастающей)

1. **`FetchResult`** — добавить dataclass в `fetcher.py`.
2. **`fetch`** — реализовать функцию.
3. Написать тесты группы A (`test_fetch_*`), прогнать `pytest -q tests/test_fetcher.py` — зелёные.
4. **`extract_links`** + класс `_LinkParser` — реализовать.
5. Написать тесты группы B (`test_extract_links_*`), прогнать — зелёные.
6. Написать интеграционный тест E, прогнать — зелёный.
7. Финальные проверки качества (§8).

На каждом шаге после написания кода+тестов гонять `.venv/bin/pytest -q` и видеть, что число зелёных растёт, а `test_smoke.py` продолжает проходить (его не ломать).

## 6. mypy strict — где может закапризничать и как типизировать

- **`handle_starttag`**: сигнатура должна ТОЧНО совпадать с базовой из stdlib, иначе mypy strict ругнётся на несовместимое переопределение (LSP): `def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None`. Значение атрибута — `str | None` (у булевых атрибутов вроде `<a disabled>` значение `None`), поэтому проверка `value` на непустую строку обязательна и заодно снимает вопрос типов.
- **`self.hrefs`**: аннотировать явно — `self.hrefs: list[str] = []` в `__init__`.
- **`content_type`**: `response.headers.get("content-type")` в httpx возвращает `str | None` — ровно тип поля `FetchResult.content_type`, приведения не нужно. Если mypy почему-то выведет иначе — НЕ добавляй `# type: ignore`, разберись (обычно тип корректен).
- **`repr(exc)`** — `str`, совпадает с `error: str | None`.
- Все функции и методы должны иметь аннотации возвращаемого типа (`-> None`, `-> FetchResult`, `-> list[str]`) — strict требует.
- В тестах strict тоже действует (`files = ["src", "tests"]`): у каждой тест-функции ставь `-> None`; переменные с `httpx.AsyncClient`/`FetchResult` типизируются сами.

## 7. Acceptance-критерии (как доказать «Готово когда»)

«Готово когда: можно скачать seed и получить список исходящих абсолютных ссылок» доказывается совокупностью:
- `test_fetch_ok` — seed скачивается, тело/статус/content-type доступны.
- `test_fetch_then_extract_links_end_to_end` — **основной** acceptance-тест: `fetch(seed)` → `extract_links(seed, body)` → список абсолютных http/https-ссылок с правильной абсолютизацией и фильтрацией. Именно этот тест — прямое доказательство критерия.
- Устойчивость к ошибкам (обход не роняется): `test_fetch_404_is_not_crawl_error` и `test_fetch_timeout_returns_error_result` показывают, что 4xx и таймаут возвращаются как `FetchResult`, а не как исключение.

Плюс формальные ворота (§8) — зелёные `pytest`/`ruff`/`mypy`.

## 8. Финальные проверки (чеклист перед завершением)

Из корня репозитория, все три должны быть зелёными:
- `.venv/bin/pytest -q` — все тесты (включая старый `test_smoke.py`) проходят.
- `.venv/bin/ruff check .` — чисто (следи за порядком импортов — правило `I`; неиспользуемых импортов быть не должно).
- `.venv/bin/ruff format .` — при желании отформатировать (не обязательно для прохождения, но принято в проекте).
- `.venv/bin/mypy` — чисто в strict-режиме.

## 9. Чего НЕ делать на этом этапе (явные границы)

- **НЕ** трогать `robots.py`, `ratelimit.py`, `dedup.py`, `cli.py` — даже TODO-комментарии в них не переписывать (они актуальны для будущих этапов).
- **НЕ** добавлять дедупликацию URL в `extract_links` — дубли остаются, порядок сохраняется. Это Этап 4 (`dedup.normalize`/`UrlDedup`).
- **НЕ** добавлять rate-limiting, семафоры, robots-проверки в `fetch` — это Этапы 2–3.
- **НЕ** вырезать фрагмент (`#frag`) из ссылок — это делает нормализация на Этапе 4.
- **НЕ** реализовывать полноценный цикл обхода / очередь / фронтир / пул воркеров в `fetcher.py` — это Этап 5 (`cli.py`). См. §2.
- **НЕ** менять `pyproject.toml`, версию пакета, `__init__.py`.
- **НЕ** вызывать `raise_for_status()`.
- **НЕ** ловить голый `except Exception` в `fetch` — только `httpx.HTTPError`.
- **НЕ** обращаться в реальную сеть в тестах — только respx.

## 10. Последний шаг — снять заглушки

Убедиться, что в финальном `src/politecrawl/fetcher.py`:
- Обе строки `# TODO(Этап 1): ...` **удалены** (они больше не актуальны — функционал реализован). Это явное требование чеклиста SKILL.md («Заглушки `# TODO(Этап N)` реализованного этапа сняты»).
- Модульный докстринг вверху файла сохранён (он описывает назначение модуля и корректен).
- Файл содержит ровно три публичных имени: `FetchResult`, `fetch`, `extract_links` (+ приватный `_LinkParser`).

После этого — коммит НЕ делать без явной просьбы пользователя (по `CLAUDE.md` git-workflow); просто оставить рабочее дерево с зелёными `pytest`/`ruff`/`mypy` и сообщить, что Этап 1 готов к проверке тестового покрытия (шаг 4 пайплайна).
