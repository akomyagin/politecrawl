---
name: py-crawler-dev
description: Конвенции проекта politecrawl — асинхронный веб-краулер на Python/asyncio. Паттерн лениво создаваемых per-domain семафоров (реестр dict + один asyncio.Lock на создание), стиль тестирования (pytest + pytest-asyncio, HTTP-моки через respx, детерминированная проверка конкурентных инвариантов). Использовать при реализации любого этапа кодирования politecrawl.
---

# SKILL: py-crawler-dev — конвенции проекта `politecrawl`

Загружай при работе над кодом politecrawl (модули `fetcher`, `robots`,
`ratelimit`, `dedup`, `cli`). Контекст этапов — в `docs/TECHNICAL_PLAN.md`.

## Стек (кратко)

- Python 3.10, `asyncio`.
- HTTP-клиент — **httpx** (`AsyncClient`). НЕ aiohttp.
- Тесты — **pytest** + **pytest-asyncio** (`asyncio_mode = "auto"`, задан в
  `pyproject.toml` — отдельный `@pytest.mark.asyncio` на каждый тест не нужен).
- HTTP-моки — **respx** (мокает httpx-транспорт). НЕ aioresponses (та под aiohttp).
- Линт/типы — **ruff** + **mypy** (strict). Гоняем на каждом этапе.

## Ключевой паттерн: лениво создаваемые per-domain семафоры

Сердце проекта (Этап 3). Требование: ограничить одновременность **на каждый
домен отдельно**, НЕ сериализуя весь обход глобальным семафором.

### Правильный паттерн — реестр + один lock на создание

```python
import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class PerDomainLimiter:
    def __init__(self, per_domain: int) -> None:
        self._per_domain = per_domain
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._registry_lock = asyncio.Lock()  # защищает ТОЛЬКО создание записи

    async def _get_sem(self, domain: str) -> asyncio.Semaphore:
        # Быстрый путь без lock — если семафор уже есть.
        sem = self._sems.get(domain)
        if sem is not None:
            return sem
        # Медленный путь: под lock проверяем ещё раз и создаём ровно один раз.
        async with self._registry_lock:
            sem = self._sems.get(domain)
            if sem is None:
                sem = asyncio.Semaphore(self._per_domain)
                self._sems[domain] = sem
            return sem

    @asynccontextmanager
    async def slot(self, domain: str) -> AsyncIterator[None]:
        sem = await self._get_sem(domain)
        # ВАЖНО: registry_lock здесь уже отпущен. Ждём слот КОНКРЕТНОГО домена,
        # не блокируя другие домены. Это и есть точка per-domain backpressure.
        async with sem:
            yield
```

### Почему именно так

- **`registry_lock` держим только на время «проверить-и-вставить» в dict**, а не
  на время запроса. Иначе создание семафора одного домена сериализовало бы
  доступ ко всем доменам — та самая деградация в глобальный throttle.
- **Double-checked locking** (проверка `get` до и внутри lock): быстрый путь без
  lock на «горячем» уже созданном домене; lock — только на первое создание.
- **Не `setdefault(d, asyncio.Semaphore(n))`**: аргумент вычисляется всегда,
  т.е. `Semaphore` конструируется на КАЖДЫЙ вызов ещё до вставки — мусорные
  объекты на горячем пути, а с фабриками-с-эффектами — реальная гонка.
- **Ждать `async with sem` строго ВНЕ `registry_lock`**. Это критично: под
  общим lock ожидание слота заблокировало бы весь реестр.

Тот же per-key-lock паттерн переиспользуется в Этапе 2 для «robots.txt грузится
на домен ровно один раз».

## Стиль тестирования

### Конкурентные инварианты — детерминированно, без сети

Проверяем сам лимитер задачами-заглушками с управляемой задержкой, замеряя
фактическую одновременность. Никакого реального HTTP.

```python
import asyncio


async def test_per_domain_concurrency_capped() -> None:
    limiter = PerDomainLimiter(per_domain=2)
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with limiter.slot("example.com"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(10)))
    assert peak <= 2  # одновременность к домену НЕ превышена


async def test_different_domains_run_in_parallel() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    order: list[str] = []

    async def worker(domain: str) -> None:
        async with limiter.slot(domain):
            order.append(f"{domain}:start")
            await asyncio.sleep(0.01)
            order.append(f"{domain}:end")

    # Разные домены при per_domain=1 всё равно идут параллельно.
    await asyncio.gather(worker("a.com"), worker("b.com"))
    # оба стартуют до того, как любой завершится
    assert order.index("a.com:start") < order.index("b.com:end")
    assert order.index("b.com:start") < order.index("a.com:end")
```

Для проверки «ровно один семафор при гонке создания» — запусти N воркеров на
один новый домен через `asyncio.gather` и проверь `len(limiter._sems) == 1`
и что объект-семафор один и тот же.

### HTTP через respx

```python
import httpx
import respx


@respx.mock
async def test_fetch_ok() -> None:
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, html="<a href='/next'>x</a>")
    )
    async with httpx.AsyncClient() as client:
        result = await fetch(client, "https://example.com/")
    assert result.status == 200
```

Для «robots.txt скачивается один раз» — повесь на роут respx-счётчик
(`route.call_count`) и проверь, что после N параллельных запросов он равен 1.

## Чеклист перед завершением этапа

- [ ] `.venv/bin/pytest -q` — зелёный.
- [ ] `.venv/bin/ruff check .` — чисто.
- [ ] `.venv/bin/mypy` — чисто (strict).
- [ ] Конкурентные инварианты этапа покрыты детерминированными тестами.
- [ ] Реальных сетевых обращений в тестах нет (только respx).
- [ ] Заглушки `# TODO(Этап N)` реализованного этапа сняты.
