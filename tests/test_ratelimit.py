"""Тесты Этапа 3: PerDomainLimiter — per-domain rate-limiting.

Этап 6 добавляет тесты crawl-delay интервалов и адаптивного backoff (в конце
файла); тесты Этапа 3 не менялись — старый контракт slot(domain) сохранён.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from politecrawl.ratelimit import PerDomainLimiter


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
    assert peak == 2  # лимит используется полностью...
    assert active == 0  # ...и корректно освобождается


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

    assert calls == 1  # семафор создан РОВНО один раз, гонки нет
    assert len(limiter._sems) == 1  # и ровно одна запись в реестре


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


async def test_slot_is_async_context_manager() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    entered = False
    async with limiter.slot("x.com"):
        entered = True
    assert entered
    # слот освобождён на выходе: повторный вход не блокируется
    async with limiter.slot("x.com"):
        pass


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


# --- Этап 6: crawl-delay интервалы ------------------------------------------


async def test_crawl_delay_spaces_out_same_domain_requests() -> None:
    limiter = PerDomainLimiter(per_domain=2)
    starts: list[float] = []

    async def worker() -> None:
        async with limiter.slot("d.com", crawl_delay=0.05):
            starts.append(time.monotonic())
            await asyncio.sleep(0.01)

    await asyncio.gather(worker(), worker())

    starts.sort()
    # per_domain=2 допускает одновременность, но интервал всё равно
    # разводит СТАРТЫ запросов не ближе crawl_delay (с допуском на джиттер)
    assert starts[1] - starts[0] >= 0.045


async def test_crawl_delay_zero_is_noop() -> None:
    limiter = PerDomainLimiter(per_domain=2)
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with limiter.slot("d.com", crawl_delay=0.0):
            order.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            order.append(f"{tag}:end")

    await asyncio.gather(worker("w1"), worker("w2"))

    # без интервала оба воркера в слоте одновременно — поведение Этапа 3
    assert set(order[:2]) == {"w1:start", "w2:start"}
    assert set(order[2:]) == {"w1:end", "w2:end"}


async def test_no_race_on_concurrent_dispatch_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Реальный sleep измеряет ТАЙМИНГИ ОС (флейково под нагрузкой) и слабо
    # ловит именно ту регрессию, ради которой тест написан: гонку на
    # read-then-write _next_dispatch. Патчим asyncio.sleep no-op'ом — тогда
    # ни один worker никогда по-настоящему не приостанавливается (no-op
    # coroutine без await внутри не отдаёт управление event loop), и все N
    # gather'нутых воркеров синхронной domain-семафор+резервация проходят
    # строго один за другим в порядке планирования — детерминированно, без
    # реального времени. Если бы резервация НЕ была атомарной (что-то
    # реальное stalled между чтением ready_at и записью), несколько воркеров
    # схлопнули бы один и тот же ready_at, и итоговая резервация оказалась бы
    # МЕНЬШЕ N*delay вперёд — тест поймал бы это без всякого допуска на джиттер.
    delay = 0.03
    n = 5
    limiter = PerDomainLimiter(per_domain=n)

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    start = time.monotonic()

    async def worker() -> None:
        async with limiter.slot("d.com", crawl_delay=delay):
            pass

    await asyncio.gather(*(worker() for _ in range(n)))

    reserved = limiter._next_dispatch["d.com"]
    # n воркеров резервируют n последовательных слотов по delay; реальное
    # время не течёт (sleep пропатчен), поэтому итог обязан быть на n*delay
    # впереди отметки "now" на момент первого захода — небольшой допуск лишь
    # на накладные расходы измерения (микросекунды), не на sleep-джиттер.
    assert reserved - start >= n * delay - 0.005


# --- Этап 6: адаптивный backoff ---------------------------------------------


def test_backoff_doubles_on_429_and_5xx() -> None:
    limiter = PerDomainLimiter(per_domain=1)

    limiter.record_response("d.com", 429)
    assert limiter._backoff["d.com"] == 1.0  # старт с 1s
    limiter.record_response("d.com", 429)
    assert limiter._backoff["d.com"] == 2.0
    limiter.record_response("d.com", 500)
    assert limiter._backoff["d.com"] == 4.0  # 5xx растит так же, как 429
    limiter.record_response("d.com", 503)
    assert limiter._backoff["d.com"] == 8.0

    for _ in range(10):
        limiter.record_response("d.com", 429)
    assert limiter._backoff["d.com"] == 60.0  # потолок не превышается


def test_backoff_doubles_on_network_error() -> None:
    limiter = PerDomainLimiter(per_domain=1)

    limiter.record_response("d.com", None)  # транспортная ошибка: status=None
    assert limiter._backoff["d.com"] == 1.0
    limiter.record_response("d.com", None)
    assert limiter._backoff["d.com"] == 2.0
    limiter.record_response("d.com", None)
    assert limiter._backoff["d.com"] == 4.0


def test_backoff_decays_on_success() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    limiter.record_response("d.com", 429)
    limiter.record_response("d.com", 429)  # -> 2.0

    limiter.record_response("d.com", 200)
    assert limiter._backoff["d.com"] == 1.0
    limiter.record_response("d.com", 200)
    assert limiter._backoff["d.com"] == 0.5

    for _ in range(20):
        limiter.record_response("d.com", 200)
    assert limiter._backoff["d.com"] == 0.0  # затухает до нуля...
    limiter.record_response("d.com", 200)
    assert limiter._backoff["d.com"] == 0.0  # ...и не уходит в минус

    # успех на «чистом» домене оставляет backoff нулевым
    limiter.record_response("clean.com", 200)
    assert limiter._backoff["clean.com"] == 0.0


def test_backoff_is_per_domain() -> None:
    limiter = PerDomainLimiter(per_domain=1)
    limiter.record_response("bad.com", 429)
    assert limiter._backoff["bad.com"] == 1.0
    # чужой домен не затронут: у него backoff не появился
    assert limiter._backoff.get("good.com", 0.0) == 0.0


async def test_backoff_affects_next_dispatch_via_slot() -> None:
    limiter = PerDomainLimiter(per_domain=2)
    # взвинтить backoff публичным API, затем затушить успехами до
    # тест-пригодного размера (свежий 429 даёт целую секунду ожидания)
    limiter.record_response("d.com", 429)  # 1.0
    for _ in range(6):
        limiter.record_response("d.com", 200)  # 1.0 -> ... -> 0.015625
    backoff = limiter._backoff["d.com"]
    assert 0.01 < backoff < 0.05

    starts: list[float] = []

    async def worker() -> None:
        async with limiter.slot("d.com"):  # БЕЗ crawl_delay
            starts.append(time.monotonic())

    await asyncio.gather(worker(), worker())

    starts.sort()
    # интервал навязан одним лишь backoff'ом, без явного crawl_delay
    assert starts[1] - starts[0] >= backoff - 0.005
