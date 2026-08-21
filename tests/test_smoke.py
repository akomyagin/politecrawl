"""Smoke-тест: пакет импортируется и CLI отвергает вызов без seed-URL."""

from __future__ import annotations

import pytest

import politecrawl
from politecrawl.cli import main


def test_package_has_version() -> None:
    assert politecrawl.__version__ == "0.0.0"


def test_cli_requires_seeds() -> None:
    # Начиная с Этапа 5 seeds обязательны: argparse завершает процесс с кодом 2.
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
