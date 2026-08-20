"""Per-domain rate-limiting через лениво создаваемые семафоры.

Этап 3 (главная техническая задача): per-domain concurrency control,
который НЕ деградирует в глобальную сериализацию очереди. Семафоры
создаются лениво по ключу (домену) и живут в общем реестре; создание
защищено единым asyncio.Lock (см. docs/TECHNICAL_PLAN.md §Этап 3 и
.claude/skills/py-crawler-dev/SKILL.md).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


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
