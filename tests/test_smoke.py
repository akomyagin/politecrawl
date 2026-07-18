"""Smoke-тест: пакет импортируется и CLI-заглушка возвращает 0."""

from __future__ import annotations

import politecrawl
from politecrawl.cli import main


def test_package_has_version() -> None:
    assert politecrawl.__version__ == "0.0.0"


def test_cli_main_returns_zero() -> None:
    assert main([]) == 0
