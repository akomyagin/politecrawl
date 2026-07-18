# CLAUDE.md

Guidance for Claude Code when working in the **politecrawl** repository.

## О проекте

politecrawl — вежливый структурный асинхронный веб-краулер (Python 3.10,
`asyncio`). Single-machine CLI, без внешних сервисов. Фокус — конкурентность:
per-domain rate-limiting через лениво создаваемые семафоры, robots.txt,
дедуп URL, backpressure. Полное описание — в `docs/PLAN.md` и
`docs/TECHNICAL_PLAN.md`.

## Конвенции

- **Язык:** документация и subject коммитов — на **русском**; код, идентификаторы
  и комментарии в коде — на **английском**.
- **Терминология этапов:** «Этап N» (см. `docs/TECHNICAL_PLAN.md`).
- **Стиль коммитов:** conventional-commit с русским subject, напр.
  `feat(этап-3): per-domain rate-limiting на лениво создаваемых семафорах`.
  Завершать коммит trailer'ом `Co-Authored-By: Claude <noreply@anthropic.com>`.
- **src-layout:** пакет в `src/politecrawl/`; тесты гоняют установленный пакет.
- Перед коммитом: `ruff check .`, `mypy`, `pytest -q` — всё зелёное.

## Команды

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q          # тесты (pytest-asyncio, asyncio_mode=auto)
.venv/bin/ruff check .       # линт
.venv/bin/ruff format .      # формат
.venv/bin/mypy               # типы (strict)
```

## Пайплайн разработки (проверенный, портфельный — НЕ Fable-based)

На каждый **Этап**:

1. **Opus 4.8** — планирование этапа + написание кода (реализация модуля,
   тесты пишет по ходу или передаёт на шаг 2).
2. **Sonnet 5** (основной чат) — проверка тестового покрытия, тестирование,
   проверка работоспособности (гоняет `pytest`/`ruff`/`mypy`, добивает покрытие
   конкурентных инвариантов).
3. **Opus** (через Agent-тул, `model: opus`) — независимое ревью `/code-review`
   на diff ветки этапа.
4. **Цикл исправлений** — до **3 итераций** между ревью и правками (Sonnet
   правит, Opus перепроверяет).
5. **Commit + push + PR** (conventional-commit, русский subject) в `master`.

## Git-workflow

- От `master` на **каждый Этап** заводится новая ветка (напр. `этап-3-ratelimit`
  или `feat/ratelimit`).
- Работа в ветке → **PR** в `master` → merge.
- **Не** коммитить и **не** пушить без явной просьбы пользователя.

## Замечания по коду

- **Конкурентные инварианты тестируются детерминированно** — задачами с
  управляемыми задержками (`asyncio.Event`/`sleep`), а не реальной сетью.
- **HTTP мокается через respx** (клиент — httpx). Реальные сетевые обращения
  в тестах запрещены.
- Ошибки сети/парсинга **не роняют обход** — учитываются в отчёте.
- Ключевой паттерн per-domain семафоров и стиль тестов — в
  `.claude/skills/py-crawler-dev/SKILL.md`.
