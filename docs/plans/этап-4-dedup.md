# Этап 4 — Дедупликация URL: план

## Цель

Не посещать один и тот же URL дважды: эквивалентные по написанию URL
(регистр схемы/хоста, дефолтный порт, fragment, порядок query-параметров,
пустой путь) должны схлопываться в один канонический вид.

## Что меняется

- `src/politecrawl/dedup.py` — реализация вместо TODO-заглушки:
  - `normalize(url: str) -> str` через `urllib.parse.urlsplit`/`urlunsplit` +
    `parse_qsl`/`urlencode`:
    1. lower-case `scheme` и `netloc` (hostname), сохраняя userinfo/порт как есть
       до шага удаления дефолтного порта;
    2. убрать `:80` при `scheme == "http"` и `:443` при `scheme == "https"`
       из netloc;
    3. `path` — пустая строка → `"/"`;
    4. `query` — `parse_qsl(keep_blank_values=True)`, `sorted()` по паре
       `(key, value)`, обратно через `urlencode`;
    5. `fragment` — всегда отбрасывается (`""`);
    6. собрать обратно `urlunsplit`.
  - `UrlDedup` — тонкая синхронная обёртка над `set[str]`:
    - `add(url) -> bool` — нормализует, `in`-проверка + `set.add` одним
      синхронным методом (атомарно в рамках шага event loop, await внутри
      нет — вызывается без `await` в Этапе 5);
    - `seen(url) -> bool` — только проверка, без мутации.
- `tests/test_dedup.py` — новый файл, без классов, в стиле
  `tests/test_ratelimit.py`/`tests/test_robots.py`.

## Тест-кейсы

1. Параметризованная таблица `normalize()`:
   - fragment отбрасывается (`http://a/x#frag` → `http://a/x`);
   - дефолтный порт убирается для http (`:80`) и https (`:443`) отдельно;
   - нестандартный порт (напр. `:8080`) — сохраняется;
   - схема и хост в upper/mixed case → lower-case;
   - query-параметры пересортировываются в стабильный порядок независимо
     от исходного;
   - пустой путь → `/`.
2. `UrlDedup.add()`: первый вызов → `True` и URL добавлен; повторный вызов
   с тем же (буквально идентичным) URL → `False`.
3. `UrlDedup.add()`/`seen()` на эквивалентных, но по-разному написанных URL
   (`HTTP://Example.com:80/x?b=2&a=1#frag` vs `http://example.com/x?a=1&b=2`)
   — схлопываются: второй `add` → `False`, `seen` на обоих → `True` после
   первого `add`.

## Критерий готовности

`pytest -q tests/test_dedup.py`, `ruff check src/politecrawl/dedup.py
tests/test_dedup.py`, `mypy` (весь проект) — все зелёные, без новых ошибок.
