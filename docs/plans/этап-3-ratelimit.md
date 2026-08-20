# План реализации: Этап 3 — Per-domain rate-limiting (`ratelimit.py`)

## Контекст и цель

Ты реализуешь **Этап 3** проекта politecrawl — вежливый асинхронный веб-краулер на Python 3.10 / `asyncio`. Это **главная техническая задача проекта**: `PerDomainLimiter` — лимитер одновременных запросов, ограничивающий конкурентность **отдельно на каждый домен**, НЕ сериализуя весь обход единым глобальным семафором. Семафоры создаются лениво по ключу (домену) и живут в общем реестре; ленивое создание защищено единым `asyncio.Lock`.

Ветка репозитория — `этап-3-ratelimit` (уже заведена). **Git-коммиты делать ЗАПРЕЩЕНО** — только правка файлов в рабочем дереве.

Проект: `src`-layout, пакет в `src/politecrawl/`. Тесты гоняют установленный пакет. `pytest-asyncio` с `asyncio_mode="auto"` (отдельный `@pytest.mark.asyncio` НЕ нужен — async-тест-функции подхватываются автоматически). Типизация — `mypy strict`; линт/формат — `ruff`.

---

## Файлы

| Файл | Действие |
|---|---|
| `src/politecrawl/ratelimit.py` | **Изменить**: заглушка (сейчас только docstring + TODO-комментарии) → полная реализация `PerDomainLimiter`. |
| `tests/test_ratelimit.py` | **Создать**: детерминированные тесты конкурентных инвариантов. |

**НЕ трогать** `fetcher.py`, `robots.py`, `dedup.py`, `cli.py`, `pyproject.toml`, никакие другие модули или тесты. **НЕ** интегрировать `PerDomainLimiter` в реальный HTTP-конвейер — это Этап 5.

---

## Часть 1. Реализация `src/politecrawl/ratelimit.py`

Заменить TODO-заглушку (строки 12–14 текущего файла) на реализацию класса `PerDomainLimiter`. Реализуй **ровно эту структуру** (это эталонный код из `.claude/skills/py-crawler-dev/SKILL.md`, воспроизвести с этим уровнем точности, а не «вдохновиться»):

- Сохрани существующий модульный docstring (строки 1–8) — он корректен. Убери три строки `# TODO(Этап 3): ...`.
- Сохрани `from __future__ import annotations`.

Импорты (строго `collections.abc.AsyncIterator`, НЕ `typing.AsyncIterator` — последний устарел в 3.9+ и mypy strict / ruff на него ругаются):

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
```

Класс:

```python
class PerDomainLimiter:
    """Per-domain concurrency limiter backed by lazily created semaphores.

    Each domain gets its own asyncio.Semaphore(per_domain), created on first
    use and kept in a registry. A single registry lock guards ONLY the
    create-if-absent step, never the wait for a slot: releasing the lock before
    awaiting the semaphore is what keeps domains independent (per-domain
    backpressure instead of a global throttle).
    """

    def __init__(self, per_domain: int) -> None:
        self._per_domain = per_domain
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._registry_lock = asyncio.Lock()  # guards ONLY registry insertion

    async def _get_sem(self, domain: str) -> asyncio.Semaphore:
        """Return the semaphore for domain, creating it exactly once.

        Double-checked locking: fast path (no lock) for an already-registered
        domain; slow path re-checks under the registry lock so concurrent first
        callers on a fresh domain share ONE semaphore instead of racing to
        create several.
        """
        # Fast path: semaphore already registered, no lock needed.
        sem = self._sems.get(domain)
        if sem is not None:
            return sem
        # Slow path: create exactly once under the registry lock.
        async with self._registry_lock:
            sem = self._sems.get(domain)
            if sem is None:
                sem = asyncio.Semaphore(self._per_domain)
                self._sems[domain] = sem
            return sem

    @asynccontextmanager
    async def slot(self, domain: str) -> AsyncIterator[None]:
        """Acquire a per-domain slot for the duration of the `async with` body.

        The registry lock is already released by the time we await the
        semaphore: we wait on the slot of THIS domain only, never blocking other
        domains. This is the per-domain backpressure point.
        """
        sem = await self._get_sem(domain)
        async with sem:
            yield
```

**Ключевые инварианты реализации (не отклоняться):**

1. **`_registry_lock` удерживается только на «проверить-и-вставить» в `dict`**, НЕ на время `async with sem`. Ожидание слота (`async with sem`) происходит строго **после** выхода из `async with self._registry_lock` — семафор уже получен, лок отпущен. Если бы ожидание слота шло под `registry_lock`, занятость одного домена сериализовала бы доступ ко всем доменам — та самая деградация в глобальный throttle, которой этот этап и избегает.

2. **Double-checked locking**: `self._sems.get(domain)` вызывается дважды — быстрый путь без лока (горячий, уже созданный домен) и повторная проверка внутри лока (чтобы воркер, дождавшийся лока, не создал второй семафор поверх уже созданного).

3. **НЕ использовать `self._sems.setdefault(domain, asyncio.Semaphore(self._per_domain))`.** Аргумент `setdefault` вычисляется всегда, то есть `asyncio.Semaphore(...)` конструируется на КАЖДЫЙ вызов ещё до вставки — мусорные объекты на горячем пути, а с фабриками-с-побочными-эффектами это даёт настоящую гонку. Явный `async with registry_lock` вокруг «проверить-и-вставить» — предсказуемый и тестируемый паттерн (см. объяснение в SKILL.md, раздел «Почему именно так»).

**Отличие от Этапа 2 (`robots.py`) — осознанное, не копировать вслепую:** В `RobotsCache._get_parser` сетевой запрос robots.txt выполняется **под** `_registry_lock` намеренно (чтобы robots.txt каждого хоста грузился ровно один раз). Здесь наоборот: ожидание ресурса (`async with sem`) выносится **из-под** лока — это канонический случай, ради которого паттерн и придуман. Не переноси удержание-под-локом из robots.py сюда.

**Замечания под mypy strict:**
- Возврат `slot` аннотируется `AsyncIterator[None]` из `collections.abc` (декоратор `@asynccontextmanager` превращает такую async-генераторную функцию в контекст-менеджер; mypy это понимает при этой аннотации).
- `_sems: dict[str, asyncio.Semaphore]` и `_registry_lock: asyncio.Lock` — аннотации как показано.
- `_get_sem` возвращает `asyncio.Semaphore`. Все ветки возвращают значение — mypy доволен.
- Никаких `Any`, никаких `# type: ignore`.

**Последний шаг:** убедиться, что снятые TODO-строки не оставили висячих комментариев, и что модульный docstring остался.

---

## Часть 2. Тесты `tests/test_ratelimit.py`

Создать новый файл. Шапка по стилю проекта (см. `tests/test_fetcher.py`, `tests/test_robots.py`):

```python
"""Тесты Этапа 3: PerDomainLimiter — per-domain rate-limiting."""

from __future__ import annotations

import asyncio

from politecrawl.ratelimit import PerDomainLimiter
```

**Никакого HTTP / httpx / respx** в этом файле — лимитер тестируется сам по себе, задачами-заглушками с управляемой задержкой (`asyncio.sleep` / `asyncio.Event`). Реальных сетевых обращений быть не должно (их тут в принципе нет — нет клиента). Все async-тесты — обычные `async def test_...` (без декоратора, `asyncio_mode="auto"`).

Ниже — обязательные тесты. Инварианты 1–3 из TECHNICAL_PLAN.md должны быть покрыты и зелёными.

### Тест A — Инвариант 2: hard cap на конкурентность одного домена

Имя: `test_single_domain_concurrency_capped_and_fully_used`.

20 воркеров на ОДИН домен при `per_domain=2`. Замерять `active` (сейчас в критической секции) и `peak` (максимум за прогон). Проверка **строгая с двух сторон**: `peak == 2`, не только `<= 2`. Верхняя граница (`<= 2`) доказывает, что лимит не превышается; равенство (`== 2`) доказывает, что лимит реально используется полностью (иначе тест прошёл бы и при сломанном лимитере, который просто никогда не пускает больше одного).

Чтобы `peak == per_domain` достигался детерминированно (а не «повезло по таймингам»): внутри слота инкрементировать `active`, обновить `peak`, затем `await asyncio.sleep(0.01)` — так одновременно вошедшие воркеры точно пересекутся внутри критической секции. При 20 воркерах и лимите 2 в каждый момент внутри окажется ровно 2 задачи, которые спят параллельно.

```python
async def test_single_domain_concurrency_capped_and_fully_used() -> None:
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

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 2       # лимит используется полностью...
    assert active == 0     # ...и корректно освобождается
```

### Тест B — Инвариант 1: изоляция разных доменов (параллельность)

Имя: `test_different_domains_run_in_parallel`.

При `per_domain=1` несколько доменов должны идти **параллельно**, а не сериализоваться. Использовать `order.append` для регистрации событий start/end и проверять **явными assert на взаимное вложение интервалов**, а не расплывчато.

Сценарий на два домена `a.com` и `b.com`, `per_domain=1`. Каждый воркер: `append(f"{domain}:start")`, `await asyncio.sleep(0.01)`, `append(f"{domain}:end")`. Запустить оба через `gather`. Проверка: оба стартуют раньше, чем любой завершается (интервалы перекрываются) — если бы домены сериализовались, один end встал бы раньше другого start.

```python
async def test_different_domains_run_in_parallel() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    order: list[str] = []

    async def worker(domain: str) -> None:
        async with limiter.slot(domain):
            order.append(f"{domain}:start")
            await asyncio.sleep(0.01)
            order.append(f"{domain}:end")

    await asyncio.gather(worker("a.com"), worker("b.com"))

    # оба домена входят в слот раньше, чем любой из них выходит:
    # интервалы перекрываются => домены не сериализованы.
    assert order.index("a.com:start") < order.index("b.com:end")
    assert order.index("b.com:start") < order.index("a.com:end")
    # для наглядности: первые два события — старты, последние два — концы
    assert set(order[:2]) == {"a.com:start", "b.com:start"}
    assert set(order[2:]) == {"a.com:end", "b.com:end"}
```

Дополни контрастным тестом `test_same_domain_serializes_at_one` (`per_domain=1`, два воркера на ОДИН домен): здесь порядок обязан быть строго последовательным — `start, end, start, end`, перекрытия НЕТ. Это доказывает, что при `per_domain=1` один домен действительно сериализуется (а параллельность в тесте B — заслуга изоляции доменов, а не того, что лимит вообще не работает).

```python
async def test_same_domain_serializes_at_one() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with limiter.slot("same.com"):
            order.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            order.append(f"{tag}:end")

    await asyncio.gather(worker("w1"), worker("w2"))

    # один домен, лимит 1 => строгая сериализация, без перекрытия интервалов.
    assert order[0].endswith(":start")
    assert order[1].endswith(":end")
    assert order[0].split(":")[0] == order[1].split(":")[0]  # тот же воркер закрылся
    assert order[2].endswith(":start")
    assert order[3].endswith(":end")
```

### Тест C — Инвариант 3: отсутствие гонки при первом создании семафора (ЯДРО ЭТАПА)

Это самый важный тест. Проверка `len(limiter._sems) == 1` **необходима, но НЕ достаточна**: dict мог бы оказаться размера 1 даже при частичной гонке, если один из конкурирующих семафоров создался, но так и не был записан (перезатёрт) или не был использован. Нужно доказать **физическую идентичность** объекта семафора: все воркеры держали ОДИН И ТОТ ЖЕ объект `asyncio.Semaphore`, и конструктор `asyncio.Semaphore` для этого домена был вызван **ровно один раз**.

Реализуй проверку **двумя независимыми механизмами** (оба в одном тесте или в двух — на твоё усмотрение, но покрыть оба):

**Механизм 1 — подсчёт вызовов конструктора `asyncio.Semaphore` через monkeypatch.** Обернуть `asyncio.Semaphore` счётчиком до создания лимитера, запустить 50 воркеров через `asyncio.gather` на ОДИН свежий домен без предварительного «прогрева», проверить что конструктор вызван ровно 1 раз. Это ловит именно гонку создания: при гонке несколько воркеров прошли бы быстрый путь (`get` вернул `None`) и — если бы double-checked locking был сломан (например, без повторной проверки под локом, или без лока вовсе) — сконструировали бы по семафору каждый.

Важно: патчить надо тот символ, который реально вызывается в `ratelimit.py`. В коде вызывается `asyncio.Semaphore(...)`, значит патчить `asyncio.Semaphore` (через `monkeypatch.setattr(asyncio, "Semaphore", counting_factory)`). Фабрика-счётчик должна возвращать **настоящий** `asyncio.Semaphore` (сохрани оригинал ДО патча и вызывай его), иначе сломаешь семантику.

```python
async def test_no_race_on_first_creation_constructor_called_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_semaphore = asyncio.Semaphore

    def counting_semaphore(value: int = 1) -> asyncio.Semaphore:
        nonlocal calls
        calls += 1
        return real_semaphore(value)

    monkeypatch.setattr(asyncio, "Semaphore", counting_semaphore)

    limiter = PerDomainLimiter(per_domain=3)

    async def worker() -> None:
        async with limiter.slot("fresh.com"):
            await asyncio.sleep(0)

    await asyncio.gather(*(worker() for _ in range(50)))

    assert calls == 1                 # семафор создан РОВНО один раз, гонки нет
    assert len(limiter._sems) == 1    # и ровно одна запись в реестре
```

Не забудь `import pytest` вверху файла для аннотации `pytest.MonkeyPatch` (mypy strict требует типизированный параметр, `# type: ignore` не использовать). `PerDomainLimiter(per_domain=3)` — `per_domain` >1, чтобы 50 воркеров не выстроились в очередь по 1 и тест не зависел от долгих ожиданий; `await asyncio.sleep(0)` внутри достаточно, чтобы дать шанс гонке проявиться (все воркеры проходят быстрый путь до первого создания).

**Механизм 2 — физическая идентичность объекта, полученного воркерами.** Собрать `id()` (или сам объект) семафора, который каждый воркер реально использовал, и проверить, что он один на всех. Получить объект напрямую из `slot` нельзя (он `yield`-ит `None`), поэтому обратись к `_get_sem` — это приватный метод, но тест того же модуля вправе его звать (как `test_robots.py` обращается к `cache._parsers`). Запусти 50 воркеров, каждый зовёт `await limiter._get_sem("fresh2.com")` и складывает результат; проверь, что все объекты — один и тот же (`len({id(s) for s in collected}) == 1`) и он же лежит в реестре.

```python
async def test_no_race_all_workers_share_one_semaphore_object() -> None:
    limiter = PerDomainLimiter(per_domain=3)
    collected: list[asyncio.Semaphore] = []

    async def worker() -> None:
        sem = await limiter._get_sem("fresh2.com")
        collected.append(sem)

    await asyncio.gather(*(worker() for _ in range(50)))

    assert len(collected) == 50
    # физическая идентичность: все воркеры получили ОДИН объект-семафор
    assert len({id(s) for s in collected}) == 1
    assert collected[0] is limiter._sems["fresh2.com"]
```

Вместе механизмы 1 и 2 доказывают: (а) конструктор вызван один раз — не было «потерянного» лишнего семафора; (б) все воркеры физически разделяют один объект — запись не перезатиралась. Ни `len(_sems) == 1` по отдельности такого не гарантирует.

### Тест D — `slot()` используется именно как async context manager

Имя: `test_slot_is_async_context_manager`.

Убедиться, что публичный интерфейс — `async with limiter.slot(domain):`, а не «получить семафор отдельным методом». Достаточно короткого позитивного теста: войти в `async with limiter.slot("x.com"):`, внутри что-то сделать, выйти; после выхода слот освобождён — повторный вход в тот же слот при `per_domain=1` не виснет.

```python
async def test_slot_is_async_context_manager() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    entered = False
    async with limiter.slot("x.com"):
        entered = True
    assert entered
    # слот освобождён на выходе: повторный вход не блокируется
    async with limiter.slot("x.com"):
        pass
```

### Тест E — Backpressure end-to-end (желательный, добавить)

Имя: `test_slow_domain_does_not_block_fast_domain`.

Смешанный сценарий: медленный домен не тормозит быстрый — «честный backpressure» из сквозных принципов проекта. `per_domain=1`. Один домен `slow.com` держит слот долго (например `asyncio.sleep(0.05)`), домен `fast.com` — быстро (`asyncio.sleep(0)` или очень коротко) и делает несколько итераций. Проверить, что быстрый домен успевает завершить все свои итерации ДО того, как завершится медленный (медленный занят своим семафором, но не мешает быстрому).

Сделать детерминированно через порядок событий, а не через сравнение абсолютного времени: пусть `fast.com` делает 3 последовательных коротких захода в свой слот и пишет в `order`; `slow.com` — один долгий. Проверить, что все три события `fast` появились в `order` раньше события `slow:end`.

```python
async def test_slow_domain_does_not_block_fast_domain() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    order: list[str] = []

    async def slow() -> None:
        async with limiter.slot("slow.com"):
            await asyncio.sleep(0.05)
            order.append("slow:end")

    async def fast() -> None:
        for i in range(3):
            async with limiter.slot("fast.com"):
                await asyncio.sleep(0)
                order.append(f"fast:{i}")

    await asyncio.gather(slow(), fast())

    # быстрый домен прогнал все свои итерации, пока медленный держал СВОЙ слот
    assert order.index("fast:0") < order.index("slow:end")
    assert order.index("fast:1") < order.index("slow:end")
    assert order.index("fast:2") < order.index("slow:end")
```

---

## Acceptance-критерии (сопоставление с тестами)

- **Инвариант 1 (изоляция доменов)** — покрыт `test_different_domains_run_in_parallel` (+ контраст `test_same_domain_serializes_at_one`).
- **Инвариант 2 (hard cap на конкурентность)** — покрыт `test_single_domain_concurrency_capped_and_fully_used` (строгая проверка `peak == 2`).
- **Инвариант 3 (нет гонки при первом создании)** — покрыт `test_no_race_on_first_creation_constructor_called_once` + `test_no_race_all_workers_share_one_semaphore_object`.
- Интерфейс `slot()` как async CM — `test_slot_is_async_context_manager`.
- Честный backpressure — `test_slow_domain_does_not_block_fast_domain`.

Все тесты детерминированные, задачами-заглушками с управляемой задержкой; реального HTTP нет.

## Чего НЕ делать (границы этапа)

- НЕ трогать `fetcher.py`, `robots.py`, `dedup.py`, `cli.py`, `pyproject.toml`.
- НЕ интегрировать `PerDomainLimiter` в HTTP-конвейер / `Crawler` — это Этап 5.
- НЕ делать реестр отдельных per-key `asyncio.Lock` (лок на каждый домен). Единый `_registry_lock` — только на **создание записи** в реестре, не на удержание слота. Per-key lock — усложнение вне MVP (упомянуто в TECHNICAL_PLAN.md для Этапа 2 как возможное будущее, но здесь его быть не должно).
- НЕ удерживать `_registry_lock` во время `async with sem`.
- НЕ использовать `setdefault` с конструктором семафора в аргументе.
- НЕ импортировать `AsyncIterator` из `typing`.

## Финальная проверка (обязательна перед сдачей)

Прогнать из корня репозитория и добиться зелёного по всем трём:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy
```

Все три чистые. TODO-заглушки `# TODO(Этап 3)` в `ratelimit.py` сняты. В отчёте вернуть: изменённые/созданные файлы, число прошедших тестов и статус ruff/mypy (сырой вывод команд).
