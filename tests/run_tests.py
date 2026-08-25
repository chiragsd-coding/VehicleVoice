"""
run_tests.py -- dependency-light test runner.

Runs every `test_*` function in tests/test_core.py with plain asserts, needing
only the Python standard library (no pytest). Invoke from the project root:

    python3 tests/run_tests.py

The same test functions also run unchanged under pytest:
    .venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_MODULE = "tests.test_core"


def collect_tests(module):
    return [
        (name, fn) for name, fn in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and fn.__module__ == module.__name__
    ]


def run_module(module_name: str = DEFAULT_MODULE) -> int:
    module = importlib.import_module(module_name)
    tests = collect_tests(module)
    passed = failed = 0
    print(f"Running {len(tests)} tests from {module_name}...")
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


def main(module_name: str = DEFAULT_MODULE):
    sys.exit(run_module(module_name))


if __name__ == "__main__":
    main()
