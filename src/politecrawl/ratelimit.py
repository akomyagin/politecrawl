"""Per-domain rate-limiting через лениво создаваемые семафоры.

Этап 3 (главная техническая задача): per-domain concurrency control,
который НЕ деградирует в глобальную сериализацию очереди. Семафоры
создаются лениво по ключу (домену) и живут в общем реестре; создание
защищено единым asyncio.Lock (см. docs/TECHNICAL_PLAN.md §Этап 3 и
.claude/skills/py-crawler-dev/SKILL.md).
"""

from __future__ import annotations

# TODO(Этап 3): реализовать PerDomainLimiter с реестром dict[str, asyncio.Semaphore]
# TODO(Этап 3): защитить ленивое создание семафора одним asyncio.Lock
# TODO(Этап 3): предоставить async-контекст-менеджер slot(domain) для acquire/release
