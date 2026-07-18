"""Парсинг и per-domain кеширование robots.txt.

Этап 2: загрузка /robots.txt, парсинг правил, кеш результата на домен.
"""

from __future__ import annotations

# TODO(Этап 2): реализовать RobotsCache — per-domain кеш распарсенных правил
# TODO(Этап 2): реализовать can_fetch(url, user_agent) с ленивой подгрузкой robots.txt
