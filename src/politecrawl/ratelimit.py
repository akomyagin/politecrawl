"""Per-domain rate-limiting через лениво создаваемые семафоры.

Этап 3 (главная техническая задача): per-domain concurrency control,
который НЕ деградирует в глобальную сериализацию очереди. Семафоры
создаются лениво по ключу (домену) и живут в общем реестре; создание
защищено единым asyncio.Lock (см. docs/TECHNICAL_PLAN.md §Этап 3 и
.claude/skills/py-crawler-dev/SKILL.md).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# Adaptive backoff constants (Stage 6). Fixed on purpose: CLI-configurability
# is explicitly out of scope for this stage (see TECHNICAL_PLAN §Этап 6).
_BACKOFF_INITIAL = 1.0  # first 429/5xx/network error jumps straight here
_BACKOFF_CAP = 60.0  # backoff never grows past this
_BACKOFF_FLOOR = 0.01  # decayed below this -> snap to 0.0


class PerDomainLimiter:
    """Per-domain concurrency limiter backed by lazily created semaphores.

    Each domain gets its own asyncio.Semaphore(per_domain), created on first
    use and kept in a registry. A single registry lock guards ONLY the
    create-if-absent step, never the wait for a slot: releasing the lock before
    awaiting the semaphore is what keeps domains independent (per-domain
    backpressure instead of a global throttle).

    Stage 6 adds per-domain request spacing on top of the concurrency cap:
    an optional minimal interval between request STARTS (Crawl-delay from
    robots.txt) combined with an adaptive backoff that grows on 429/5xx and
    transport errors and decays on success. Both are per-domain: one throttled
    host never slows the others down.
    """

    def __init__(self, per_domain: int) -> None:
        self._per_domain = per_domain
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._registry_lock = asyncio.Lock()  # guards ONLY registry insertion
        # time.monotonic() mark before which the next request to the domain
        # must not start. Mutated synchronously (no await between read and
        # write), so no lock is needed — see _wait_for_dispatch.
        self._next_dispatch: dict[str, float] = {}
        # Current adaptive surcharge (seconds) added on top of crawl_delay;
        # 0.0 means "no backoff". Mutated only by the synchronous
        # record_response, read by _wait_for_dispatch.
        self._backoff: dict[str, float] = {}

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

    async def _wait_for_dispatch(self, domain: str, crawl_delay: float) -> None:
        """Wait until the domain's next allowed dispatch time, then reserve it.

        LOCK-FREE by the UrlDedup.add/CrawlStats precedent (Stages 4-5): the
        read of _next_dispatch and the write below happen with no await in
        between, so the reservation is atomic within one event-loop step. Two
        workers of the same domain can never observe the same ready_at.

        The interval is max(crawl_delay, backoff) — the stricter of the two
        constraints, not their sum. The reservation is written BEFORE the
        sleep: reserving after it would open a read-modify-write window in
        which several workers compute the same wait and start unspaced.
        """
        interval = max(crawl_delay, self._backoff.get(domain, 0.0))
        now = time.monotonic()
        ready_at = self._next_dispatch.get(domain, now)
        wait = max(0.0, ready_at - now)
        self._next_dispatch[domain] = max(ready_at, now) + interval
        if wait > 0:
            await asyncio.sleep(wait)

    def record_response(self, domain: str, status: int | None) -> None:
        """Adjust adaptive backoff for domain from one fetch outcome.

        429, 5xx, or a transport error (status=None) doubles the backoff
        (capped at 60s, floored at 1s on first trigger). Any other status
        halves it back toward 0. Independent of _next_dispatch — only affects
        future intervals. Synchronous on purpose: called AFTER the slot is
        released, so backoff never keeps a semaphore busy longer than the
        fetch itself.
        """
        current = self._backoff.get(domain, 0.0)
        if status is None or status == 429 or status >= 500:
            self._backoff[domain] = min(_BACKOFF_CAP, max(_BACKOFF_INITIAL, current * 2))
        elif current > _BACKOFF_FLOOR:
            self._backoff[domain] = current / 2
        else:
            self._backoff[domain] = 0.0

    @asynccontextmanager
    async def slot(self, domain: str, crawl_delay: float = 0.0) -> AsyncIterator[None]:
        """Acquire a per-domain slot for the duration of the `async with` body.

        The registry lock is already released by the time we await the
        semaphore: we wait on the slot of THIS domain only, never blocking other
        domains. This is the per-domain backpressure point.

        After the semaphore is acquired, the body additionally waits out the
        domain's dispatch interval — max(crawl_delay, adaptive backoff) since
        the previous request start. crawl_delay defaults to 0.0 for backward
        compatibility: plain slot(domain) behaves exactly as in Stage 3 unless
        backoff has been raised via record_response.
        """
        sem = await self._get_sem(domain)
        async with sem:
            await self._wait_for_dispatch(domain, crawl_delay)
            yield
