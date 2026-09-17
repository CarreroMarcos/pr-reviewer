"""T062: AST import audit — deployed `lambda/**` imports only stdlib + boto3.

Constitution II: the runtime is Python 3.12 stdlib + boto3, nothing else.
This audit parses every module the terraform archive packages
(`lambda/**.py` — `compute.tf`'s `**/*.py` sources) and fails on ANY
import — including function-level deferred imports — whose top-level
package is outside {stdlib, boto3, this deployment's own modules}.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

LAMBDA_ROOT = Path(__file__).resolve().parents[2] / "lambda"
ALLOWED_THIRD_PARTY = frozenset({"boto3"})
# Deployed layout is /var/task: `common.*` resolves to lambda/common,
# handlers sit at the zip root.
LOCAL_TOP_LEVELS = frozenset(
    {p.name for p in LAMBDA_ROOT.iterdir() if p.is_dir()}
    | {p.stem for p in LAMBDA_ROOT.glob("*.py")}
)


def _deployed_modules() -> list[Path]:
    modules = sorted(LAMBDA_ROOT.rglob("*.py"))
    assert modules, "lambda/**.py glob came up empty — path regression"
    return modules


def _imported_top_levels(tree: ast.AST) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — intra-package by construction
                continue
            if node.module:
                found.append((node.module.split(".")[0], node.lineno, node.module))
    return found


@pytest.mark.parametrize("path", _deployed_modules(), ids=lambda p: str(p.relative_to(LAMBDA_ROOT)))
def test_only_stdlib_and_boto3(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [
        (line, name)
        for top, line, name in _imported_top_levels(tree)
        if top not in sys.stdlib_module_names
        and top not in ALLOWED_THIRD_PARTY
        and top not in LOCAL_TOP_LEVELS
    ]
    assert not bad, f"{path.relative_to(LAMBDA_ROOT)}: non-stdlib/boto3 imports {bad}"
