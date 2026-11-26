"""Before `fill` types into an editor, the selection is moved inside its blocks.

The injected `fill` selects an editable host with `selectNodeContents`, so the
selection sits on the host, around the editor's block wrappers. A composition
(an emoji) as the first thing typed is committed by Firefox over that range,
which removes the wrappers: Draft.js (Meta Business Suite's composer) then threw
on every event and dropped every edit it made itself for the rest of the text
(2026-11-26). Measured on Draft.js 0.11.7: "🛍️ Bx's" was kept as "🛍️Bxs".
"""
from __future__ import annotations

import pytest

from invisible_core.juggler.actions import Actions
from invisible_core.juggler.injected import EvaluationError

from invisible_playwright._juggler import _credential_fill as cf

pytestmark = pytest.mark.unit


class _Inj:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def call(self, frame, js, args):
        if js is cf._EDITOR_SELECTION_JS:
            self.log.append(("selection", frame, args["objectId"]))
            if self.fail:
                raise EvaluationError("field: error:notconnected")
            return "caret"
        return "in"


class _Kb:
    guard = None


def _actions(log, fail=False):
    a = object.__new__(cf.CredentialActions)
    a.inj, a.keyboard = _Inj(log, fail), _Kb()
    a._typing_target = ("F", "E")
    return a


@pytest.fixture
def typed(monkeypatch):
    log = []
    monkeypatch.setattr(Actions, "_type", lambda self, text, delay_ms=None: log.append(("type", text)))
    return log


def test_the_selection_is_moved_inside_the_editor_before_the_first_key(typed):
    _actions(typed)._type("🛍️ Bx's")
    assert typed == [("selection", "F", "E"), ("type", "🛍️ Bx's")]


def test_a_field_that_went_away_still_reaches_the_typing_and_its_guard(typed):
    _actions(typed, fail=True)._type("x")
    assert typed == [("selection", "F", "E"), ("type", "x")]


def test_typing_that_is_not_a_fill_is_not_touched(typed):
    a = _actions(typed)
    a._typing_target = None
    a._type("x")
    assert typed == [("type", "x")]


def test_the_script_leaves_a_selection_the_page_placed_and_skips_what_is_not_editable():
    js = cf._EDITOR_SELECTION_JS
    assert "r.startContainer !== el && r.endContainer !== el" in js and "'kept'" in js
    assert "if (!el.isContentEditable) return 'not-editable'" in js
