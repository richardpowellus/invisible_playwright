"""A fixture that needs the package says why it cannot have it.

⛔ THE KNOWN-BAD INPUT, measured 2026-10-25: with a core whose version the
wrapper's pin did not name, `invisible_playwright` was not importable, the
conftest kept `BINARY_ENTRY_REL = None`, and `firefox_binary` answered

    TypeError: argument of type 'NoneType' is not iterable

which names neither the pin nor the import. `_require_package()` existed for
exactly this and two fixtures did not call it.
"""
from __future__ import annotations

import pytest

import conftest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("fixture", ["firefox_binary", "sample_profile"])
def test_the_fixture_names_the_import_error(monkeypatch, fixture):
    monkeypatch.delenv("INVPW_BINARY_PATH", raising=False)
    monkeypatch.setattr(conftest, "_IMPORT_ERROR",
                        ImportError("the pin names a core that is not installed"))
    monkeypatch.setattr(conftest, "BINARY_ENTRY_REL", None)
    monkeypatch.setattr(conftest, "generate_profile", None)
    body = getattr(conftest, fixture).__wrapped__
    with pytest.raises(pytest.UsageError, match="the pin names a core"):
        body()
