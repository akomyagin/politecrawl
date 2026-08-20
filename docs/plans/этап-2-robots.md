# План реализации — Этап 2: robots.txt (парсинг и per-domain кеш)

## 0. Контекст и цель

Модуль `robots.py` — обёртка над stdlib `urllib.robotparser.RobotFileParser` с ленивой per-domain загрузкой `robots.txt` и кешированием распарсенного результата. Ключевое требование: `robots.txt` каждого домена (ключ — `scheme://netloc`) грузится **ровно один раз**, даже при N параллельных вызовах, а ошибка загрузки не роняет обход (трактуется как «разрешено всё»).

Формальный источник требований — `docs/TECHNICAL_PLAN.md`, секция «## Этап 2 — robots.txt: парсинг и per-domain кеш» (строки 96–131). Паттерн конкурентной защиты — `.claude/skills/py-crawler-dev/SKILL.md`, «Ключевой паттерн» (строки 20–75).

Ветка: `этап-2-robots` (уже текущая). **Не** делать git-коммитов.

## 1. Файлы

- **Изменить:** `src/politecrawl/robots.py` — сейчас это заглушка (докстринг + два `# TODO(Этап 2)`, строки 8–9). Реализовать `RobotsCache` полностью, снять TODO.
- **Создать:** `tests/test_robots.py`.
- **Не трогать:** `src/politecrawl/fetcher.py`, `ratelimit.py`, `dedup.py`, `cli.py`, `pyproject.toml`, любые доки.

## 2. Реализация `RobotsCache` в `src/politecrawl/robots.py`

### Стиль
Придерживаться стиля `fetcher.py`: `from __future__ import annotations` вверху, английские идентификаторы и докстринги, аккуратные многострочные докстринги с объяснением инвариантов (как в `fetch`, строки 25–31).

### Импорты
```python
from __future__ import annotations

import asyncio
import urllib.robotparser
from urllib.parse import urlsplit

import httpx
```

### `__init__`
```python
class RobotsCache:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._registry_lock = asyncio.Lock()
```
- `client` хранится (как в Этапе 1, клиент передаётся снаружи — сам `RobotsCache` его не создаёт и не закрывает).
- `_parsers` — реестр по ключу `scheme://netloc`.
- `_registry_lock` — единый `asyncio.Lock`, защищает ленивое создание записи (см. §3, обоснование единого lock).

### Вычисление ключа
Вынести в приватный статический/модульный helper для читаемости и переиспользования в тестах ключа при желании (не обязательно публичный):
```python
def _host_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
```
`netloc` включает порт, если он есть (это ожидаемо — см. TECHNICAL_PLAN.md стр. 107). Схема входит в ключ: `http://` и `https://` одного хоста — разные записи (стр. 104).

### `allowed`
```python
async def allowed(self, url: str, user_agent: str) -> bool:
    key = _host_key(url)
    rfp = await self._get_parser(key)
    return rfp.can_fetch(user_agent, url)
```
Финальный возврат — `rfp.can_fetch(user_agent, url)`. Порядок аргументов `can_fetch(useragent, url)` — именно такой (сначала UA, потом URL); не перепутать.

### `_get_parser` — double-checked locking (сердце этапа)
Адаптировать структуру `PerDomainLimiter._get_sem` (SKILL.md строки 39–50) — та же схема быстрый-путь / медленный-путь-под-lock, но объект здесь не `Semaphore`, а `RobotFileParser`, и в медленном пути дополнительно выполняется сетевая загрузка.

```python
async def _get_parser(self, key: str) -> urllib.robotparser.RobotFileParser:
    # Fast path: parser already cached, no lock needed.
    rfp = self._parsers.get(key)
    if rfp is not None:
        return rfp
    # Slow path: create exactly once under the registry lock.
    async with self._registry_lock:
        rfp = self._parsers.get(key)
        if rfp is None:
            rfp = await self._load_parser(key)
            self._parsers[key] = rfp
        return rfp
```
Критично: повторная проверка `self._parsers.get(key)` внутри lock — иначе воркер, ждавший lock, повторно скачает robots.txt. Именно эта повторная проверка обеспечивает инвариант «скачано ровно один раз».

### `_load_parser` — загрузка и парсинг с обработкой ошибок
```python
async def _load_parser(self, key: str) -> urllib.robotparser.RobotFileParser:
    rfp = urllib.robotparser.RobotFileParser()
    try:
        response = await self._client.get(f"{key}/robots.txt")
    except httpx.HTTPError:
        rfp.parse([])            # network/timeout -> allow-all
        return rfp
    if response.status_code >= 400:
        rfp.parse([])            # 4xx/5xx robots.txt -> allow-all
        return rfp
    rfp.parse(response.text.splitlines())
    return rfp
```

**Решение по «разрешено всё» — `rfp.parse([])` (парсинг пустого списка строк). Обоснование:**
Свежесозданный `RobotFileParser` до вызова `parse`/`read` находится в состоянии, где `can_fetch` может вести себя неопределённо в зависимости от внутренних флагов (`last_checked`). Явный `rfp.parse([])` (или `rfp.parse("".splitlines())`, что даёт тот же пустой список) переводит парсер в валидное состояние «правил нет» — при отсутствии правил `RobotFileParser.can_fetch` возвращает `True` для любого URL и любого UA. Это документированное поведение stdlib: пустой набор правил = всё разрешено. Не использовать флаги-обёртки или подклассы — `parse([])` даёт ровно нужную семантику минимальными средствами и совпадает с формулировкой TECHNICAL_PLAN.md стр. 112–113 («распарсенный из пустой строки»).

**Обработка ошибок — два независимых случая, оба → allow-all:**
1. `httpx.HTTPError` (сеть/таймаут/connect) — ловится `except`, как в `fetcher.fetch` (стр. 34). Обход не роняется.
2. HTTP-статус ответа `>= 400` (4xx/5xx) — `robots.txt` не отдан. Проверять `response.status_code >= 400`, не `raise_for_status()` (клиент может и не иметь его настроенным; явная проверка проще и совпадает со стилем `fetch`, который тоже не вызывает `raise_for_status`).

Замечание про редиректы: клиент, переданный снаружи, обычно создан с `follow_redirects=True` (см. тесты Этапа 1). Логику редиректов не трогаем — httpx сам их отработает; на входе `_load_parser` уже финальный ответ.

## 3. Обоснование: сетевой запрос **под** удерживаемым lock — осознанный trade-off

Расписать это комментарием в коде (у `_get_parser` или `_load_parser`) и держать в голове при реализации:

В Этапе 3 (`PerDomainLimiter`) ожидание слота (`async with sem`) **намеренно вынесено ИЗ** `_registry_lock` — потому что слот удерживается на всё время HTTP-запроса страницы, и держать его под общим lock деградировало бы per-domain backpressure в глобальный throttle (SKILL.md строки 55–58, 71–73).

Здесь ситуация иная и **сетевой запрос robots.txt выполняется ПОД удерживаемым `_registry_lock` осознанно**:
- Кешируемый объект (`RobotFileParser`) должен быть создан ровно один раз на ключ. Если отпустить lock на время загрузки, два воркера на новый хост оба увидят пустой кеш и оба скачают robots.txt — нарушение инварианта «однократно».
- Цена: параллельная загрузка robots.txt **разных** хостов сериализуется единым lock. Для учебного single-machine скоупа это приемлемо — robots.txt мал, грузится один раз на хост за весь обход, и после прогрева быстрый путь (`_parsers.get`) вообще не берёт lock.
- Записи разных хостов независимы по данным; сериализуется только фаза первичной загрузки. TECHNICAL_PLAN.md (строки 116–124) явно выбирает единый lock как MVP-компромисс и указывает per-key реестр lock'ов как возможное будущее усложнение **вне** скоупа.

**НЕ реализовывать** реестр per-key lock'ов (`dict[str, asyncio.Lock]`) — только единый `_registry_lock`. Это явное требование.

## 4. mypy strict — подводные камни

- `urllib.robotparser.RobotFileParser` типизирован в stdlib-стабах (`typeshed`). Сигнатуры: `parse(self, lines: Iterable[str]) -> None` и `can_fetch(self, useragent: str, url: str) -> bool`. Никакой дополнительной ручной типизации методов не требуется — `mypy` увидит их из стабов.
- `response.text` — `str`; `.splitlines()` → `list[str]`, совместимо с `Iterable[str]` для `parse`. OK.
- Аннотировать реестр явно: `self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}` — без явной аннотации mypy strict может вывести `dict[str, <partial>]`. Аннотация обязательна.
- `_registry_lock: asyncio.Lock` — тип выводится из `asyncio.Lock()`, явной аннотации не требует, но можно добавить для единообразия.
- `httpx.HTTPError` — базовый класс всех транспортных ошибок httpx (как в `fetcher.py`), покрывает `TimeoutException`, `ConnectError`. Ловить именно его.
- Возвраты всех методов аннотированы явно (`-> bool`, `-> urllib.robotparser.RobotFileParser`) — иначе strict ругнётся на untyped def.

## 5. Тесты — `tests/test_robots.py`

Стиль строго как `tests/test_fetcher.py`: `from __future__ import annotations`, `import respx`, `@respx.mock` над async-тестами, **без** `@pytest.mark.asyncio` (режим `asyncio_mode=auto`), конкретные assert. Клиент создавать через `async with httpx.AsyncClient(follow_redirects=True) as client:` и передавать в `RobotsCache(client)`. UA в тестах — например `"politecrawl/0.0"`.

Импорт: `from politecrawl.robots import RobotsCache`.

### Обязательные тесты

**T1 — Disallow запрещает путь.**
Мок `GET https://example.com/robots.txt` → `httpx.Response(200, text="User-agent: *\nDisallow: /private\n")`. (Использовать `text=`, не `html=` — это plain text.) Проверить:
- `await cache.allowed("https://example.com/private/page", ua)` → `assert result is False`
- `await cache.allowed("https://example.com/public/page", ua)` → `assert result is True`

**T2 — 404 на robots.txt → всё разрешено.**
Мок `GET https://example.com/robots.txt` → `httpx.Response(404)`. Проверить, что произвольный путь разрешён:
- `assert await cache.allowed("https://example.com/anything", ua) is True`

**T3 — сетевая ошибка/таймаут при загрузке robots.txt → всё разрешено.**
Мок `robots.txt` с `side_effect=httpx.ConnectError("refused")` (и/или отдельным тестом `httpx.TimeoutException("timed out")` — по образцу `test_fetch_timeout_returns_error_result`, стр. 59–68). Проверить:
- `assert await cache.allowed("https://example.com/anything", ua) is True`
Обход не должен упасть — сам факт, что `allowed` вернул `bool`, а не пробросил исключение, и есть проверка «не роняет обход».

**T4 — КЛЮЧЕВОЙ конкурентный тест: robots.txt скачан ровно один раз.**
Повесить `route = respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))`. Запустить N (например 10) параллельных вызовов к одному хосту:
```python
results = await asyncio.gather(
    *(cache.allowed(f"https://example.com/p{i}", ua) for i in range(10))
)
```
Проверить:
- `assert route.call_count == 1`  ← главный assert этапа (аналог SKILL.md стр. 143–144)
- `assert len(cache._parsers) == 1` (доступ к приватному полю в тесте допустим — так же делает SKILL.md для `_sems`, стр. 122–124)
- опционально: `assert all(isinstance(r, bool) for r in results)`

Замечание для реализации теста: чтобы гонка была реальной, все 10 корутин должны стартовать до завершения первой загрузки. `asyncio.gather` этого достаточно — первый воркер берёт lock и уходит в `await client.get`, остальные встают на `_registry_lock`; после его загрузки они видят готовую запись. `route.call_count == 1` докажет отсутствие повторной загрузки.

**T5 — разные схемы (http/https) одного хоста кешируются раздельно.**
Спецификация явно требует «схема входит в ключ» (TECHNICAL_PLAN.md стр. 104). Замокать оба:
- `GET http://example.com/robots.txt` → `200, text="User-agent: *\nDisallow: /private\n"`
- `GET https://example.com/robots.txt` → `200, text="User-agent: *\n"` (пусто, всё разрешено)
Проверить:
- `assert await cache.allowed("http://example.com/private/x", ua) is False`
- `assert await cache.allowed("https://example.com/private/x", ua) is True`
- `assert len(cache._parsers) == 2`

### Полезный дополнительный тест (для полноты `can_fetch`)

**T6 — UA-специфичные правила (опционально, но желательно).**
Мок robots.txt, различающего агентов:
```
User-agent: badbot
Disallow: /

User-agent: *
Disallow: /private
```
Проверить:
- `await cache.allowed("https://example.com/public", "badbot")` → `False`
- `await cache.allowed("https://example.com/public", "goodbot")` → `True`
- `await cache.allowed("https://example.com/private", "goodbot")` → `False`
Это подтверждает, что мы правильно прокидываем `user_agent` в `can_fetch`, а не игнорируем его.

> Примечание для кодера: у stdlib `RobotFileParser` сопоставление UA — по префиксу токена; в тесте использовать простые однословные UA (`badbot`, `goodbot`), чтобы поведение было предсказуемым. Если T6 окажется хрупким на конкретной версии stdlib — упростить до одного `User-agent: *` блока, но T1–T5 обязательны.

## 6. Порядок реализации

1. Написать `_host_key` + `__init__` `RobotsCache`.
2. Написать `_load_parser` (загрузка + обе ветки ошибок + `parse`).
3. Написать `_get_parser` (double-checked locking) и `allowed`.
4. Снять оба `# TODO(Этап 2)` из докстринг-заглушки, актуализировать модульный докстринг (оставить строки 1–4 по смыслу, убрать TODO).
5. Написать тесты в порядке T1 → T2 → T3 → T4 → T5 → T6.
6. Прогнать `ruff check .`, `ruff format .`, `mypy`, `pytest -q` — всё зелёное.

## 7. Acceptance-критерии (как доказать «готово»)

- **«Обход уважает Disallow»**: T1 (запрещённый путь `False`, разрешённый `True`) + T6 (UA-специфика) зелёные.
- **«robots на домен грузится однократно»**: T4 — `route.call_count == 1` и `len(cache._parsers) == 1` при 10 параллельных вызовах.
- **«Ошибка robots не роняет обход»**: T2 (404) и T3 (сеть/таймаут) — `allowed` возвращает `True`, исключение не пробрасывается.
- **«Схема в ключе»**: T5 — `len(cache._parsers) == 2`, разное поведение для http/https.
- `ruff check .`, `mypy` (strict), `pytest -q` — все чистые/зелёные. В тестах только respx, ни одного реального сетевого обращения.

## 8. Явно: чего НЕ делать

- **Не трогать** `fetcher.py`, `ratelimit.py`, `dedup.py`, `cli.py`, `pyproject.toml`, доки.
- **Не интегрировать** `robots.allowed` в общий цикл обхода — интеграция robots+fetch+dedup в конвейер это Этап 5 (`cli.py`).
- **Не делать** реестр per-key lock'ов (`dict[str, asyncio.Lock]`) — только единый `_registry_lock`. TECHNICAL_PLAN.md явно выбирает единый lock как MVP-компромисс.
- **Не выносить** сетевой запрос robots.txt из-под lock — здесь он под lock намеренно (в отличие от Этапа 3).
- **Не вызывать** `raise_for_status()` — проверять `status_code >= 400` явно.
- **Не создавать/закрывать** httpx-клиент внутри `RobotsCache` — он приходит снаружи.
- **Не добавлять** новые зависимости.
- **Не делать** git-коммитов/пушей.
