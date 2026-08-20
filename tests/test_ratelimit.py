"""Тесты Этапа 3: PerDomainLimiter — per-domain rate-limiting."""

from __future__ import annotations

import asyncio

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
