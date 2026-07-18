"""CLI-точка входа politecrawl.

Этап 5: разбор аргументов (seed-URL, max-depth, per-domain concurrency),
запуск обхода и печать итогового отчёта.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Пока заглушка — реальный обход появится на Этапе 5."""
    args = sys.argv[1:] if argv is None else argv
    # TODO(Этап 5): argparse — seed-URL(ы), --max-depth, --per-domain-concurrency, --user-agent
    # TODO(Этап 5): собрать Crawler из fetcher/robots/ratelimit/dedup и запустить asyncio.run(...)
    # TODO(Этап 5): напечатать отчёт по итогам обхода (посещено/пропущено/ошибки; по доменам)
    _ = args
    print("politecrawl: заглушка CLI (реализация — Этап 5)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
