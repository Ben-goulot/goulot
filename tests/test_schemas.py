"""schemas/check_schemas.py as a test: API constraints, examples, business checks."""

from __future__ import annotations

import importlib.util

from goulot.settings import REPO_ROOT


def test_schemas_and_examples() -> None:
    spec = importlib.util.spec_from_file_location("check_schemas", REPO_ROOT / "schemas" / "check_schemas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0, module.errors
