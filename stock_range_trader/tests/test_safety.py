"""Structural safeguards against accidental live-trading capabilities."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
PRODUCTION_DIRECTORIES = (
    "backtest",
    "config",
    "data",
    "indicators",
    "metrics",
    "reports",
    "risk",
    "screening",
    "strategy",
)
FORBIDDEN_NETWORK_IMPORTS = {
    "aiohttp",
    "httpx",
    "requests",
    "selenium",
    "socket",
    "urllib",
    "websocket",
}
FORBIDDEN_TRANSMISSION_METHODS = {
    "connect_broker",
    "place_order",
    "send_order",
    "submit_order",
}


def _production_trees() -> list[tuple[Path, ast.AST]]:
    trees: list[tuple[Path, ast.AST]] = []
    for directory in PRODUCTION_DIRECTORIES:
        for path in (PROJECT_ROOT / directory).glob("*.py"):
            trees.append(
                (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            )
    return trees


def test_phase_one_has_no_network_client_imports() -> None:
    violations: list[str] = []
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module.split(".")[0]]
            for module in imported:
                if module in FORBIDDEN_NETWORK_IMPORTS:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{module}")

    assert not violations, "Phase 1 network imports found: " + ", ".join(violations)


def test_phase_one_has_no_order_transmission_methods() -> None:
    violations: list[str] = []
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in FORBIDDEN_TRANSMISSION_METHODS:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}:{node.name}")

    assert not violations, "Live-order methods found: " + ", ".join(violations)
