"""Дедупликация URL: нормализация + проверка «видели ли уже».

Этап 4: нормализация URL (схема/хост в нижний регистр, отбрасывание
фрагмента, сортировка query, дефолтные порты) + отметка «видели».

Выбор структуры для MVP: обычный in-memory set нормализованных URL.
Bloom filter отложен в POST_MVP — на single-machine скоупе с ограниченной
глубиной множество URL умещается в памяти, а set даёт нулевой false-positive
(bloom может ошибочно счесть новый URL уже посещённым и пропустить его).
См. docs/TECHNICAL_PLAN.md §Этап 4.
"""

from __future__ import annotations

# TODO(Этап 4): реализовать normalize(url) -> canonical str
# TODO(Этап 4): реализовать UrlDedup на базе set[str]: add()/seen()
