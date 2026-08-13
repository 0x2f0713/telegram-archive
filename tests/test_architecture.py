"""Dependency-direction guardrails for the modular monolith."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).parents[1] / "app"


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


@pytest.mark.parametrize("path", sorted((APP_ROOT / "domain").glob("*.py")))
def test_domain_has_no_outward_app_dependencies(path: Path) -> None:
    forbidden = ("app.application", "app.config", "app.infrastructure", "app.interfaces")

    assert not [name for name in _imports(path) if name.startswith(forbidden)]


@pytest.mark.parametrize("path", sorted((APP_ROOT / "application").glob("*.py")))
def test_application_does_not_import_adapters_or_interfaces(path: Path) -> None:
    forbidden = (
        "app.infrastructure",
        "app.interfaces",
        "fastapi",
        "sqlalchemy",
        "telethon",
    )

    assert not [name for name in _imports(path) if name.startswith(forbidden)]
