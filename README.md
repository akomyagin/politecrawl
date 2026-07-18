# politecrawl

**Вежливый структурный асинхронный веб-краулер.** Учебный pet-проект с фокусом
на `asyncio`-конкурентности: параллельный обход множества доменов, который при
этом уважает `robots.txt`, соблюдает **per-domain** rate-limit, дедуплицирует
URL и честно применяет backpressure к медленным доменам.

Ключевая техническая изюминка — per-domain rate-limiting **без** глобального
throttling всей очереди: лениво создаваемые семафоры по ключу (домену), а не
один глобальный семафор (который сериализовал бы весь обход).

Single-machine CLI-инструмент. Без БД, брокеров и Docker.

## Статус

Ранний bootstrap (Этап 0): скелет модулей + инфраструктура тестов/линта.
Функционал реализуется поэтапно — см. `docs/`.

## Документация

- [`docs/PLAN.md`](docs/PLAN.md) — видение, архитектура, список этапов.
- [`docs/TECHNICAL_PLAN.md`](docs/TECHNICAL_PLAN.md) — стек, обоснования, детальная разбивка по этапам.
- [`docs/POST_MVP_PLAN.md`](docs/POST_MVP_PLAN.md) — идеи после MVP.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy
```

Пайплайн разработки и конвенции проекта — в [`CLAUDE.md`](CLAUDE.md) и
[`.claude/skills/py-crawler-dev/SKILL.md`](.claude/skills/py-crawler-dev/SKILL.md).

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
