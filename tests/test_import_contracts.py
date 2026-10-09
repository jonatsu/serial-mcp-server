"""Every module the import-linter contracts in pyproject.toml name must exist.

import-linter keeps a forbidden contract whose modules are missing, so a renamed module would silently turn a
contract into a check of nothing.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def contract_modules() -> list[tuple[str, str]]:
    contracts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["importlinter"]["contracts"]
    named: list[tuple[str, str]] = []
    for contract in contracts:
        for key in ("source_modules", "forbidden_modules", "modules", "layers", "containers"):
            for entry in contract.get(key, []):
                for module in entry.replace("|", ":").split(":"):
                    if module.strip():
                        named.append((contract["name"], module.strip()))
    return named


@pytest.mark.parametrize(("contract", "module"), contract_modules())
def test_every_module_named_in_a_contract_exists(contract: str, module: str) -> None:
    assert importlib.util.find_spec(module) is not None, f"{contract!r} names missing module {module}"
