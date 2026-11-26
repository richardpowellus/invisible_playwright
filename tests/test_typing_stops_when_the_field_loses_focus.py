"""A `fill` never sends a key while something other than its field has the focus.

On 2026-11-26 Meta Business Suite's composer lost the focus partway through a
356-character fill: characters went missing from the post, and the keys after
that reached the page's keyboard shortcuts, which navigated to Leads Center.
Before each key the engine now checks the field still has the focus, waits a
bounded time for a re-rendering page to give it back, and otherwise stops.
"""
from __future__ import annotations

import pytest

from invisible_core.juggler._behaviour import PageActs
from invisible_core.juggler.actions import ActionMissed

from invisible_playwright._juggler import _credential_fill as cf
from invisible_playwright._juggler._grapheme_keyboard import GraphemeKeyboard

pytestmark = pytest.mark.unit


class _Conn:
    def __init__(self):
        self.sent = []

    def send(self, method, params, **kw):
        self.sent.append((method, params))
        return {}


class _Inj:
    """Answers the focus read from a script of answers, last one repeating."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.reads = 0

    def call(self, frame, js, args):
        assert js is cf._FOCUS_JS
        self.reads += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class _Actions:
    def __init__(self, inj):
        self.inj = inj


def _typed(conn):
    return "".join(p["text"] if m == "Page.insertText" else p.get("text", "")
                   for m, p in conn.sent
                   if m == "Page.insertText" or p.get("type") == "keydown")


def _kb(answers, text):
    conn = _Conn()
    kb = GraphemeKeyboard(conn, "S", acts=PageActs(1))
    inj = _Inj(answers)
    kb.guard = cf._FocusGuard(_Actions(inj), "F", "E", len(text))
    return conn, kb, inj


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(cf, "FOCUS_RETURN_S", 0.2)
    monkeypatch.setattr(cf, "_FOCUS_POLL_S", 0.01)


def test_a_field_that_keeps_the_focus_gets_every_key():
    conn, kb, inj = _kb(["in"], "ab cd")
    kb.type("ab cd")
    assert _typed(conn) == "ab cd"
    assert inj.reads == 4  # before every key but the first


def test_a_focus_that_comes_back_loses_no_key():
    conn, kb, _ = _kb(["in", "the page itself", "the page itself", "in"], "abc")
    kb.type("abc")
    assert _typed(conn) == "abc"


def test_a_focus_that_does_not_come_back_stops_the_typing_and_says_where_it_went():
    conn, kb, _ = _kb(["in", "<button \"Leads\">"], "abcdef")
    with pytest.raises(cf.FocusLeftField) as err:
        kb.type("abcdef")
    assert _typed(conn) == "ab"
    assert "after 2 of 6 characters" in str(err.value)
    assert '<button "Leads">' in str(err.value)
    assert isinstance(err.value, ActionMissed)  # never retried


def test_a_stop_inside_a_shift_run_releases_shift():
    conn, kb, _ = _kb(["in", "the page itself"], "ABC")
    with pytest.raises(cf.FocusLeftField):
        kb.type("ABC")
    downs = [p["key"] for m, p in conn.sent if p.get("type") == "keydown"]
    ups = [p["key"] for m, p in conn.sent if p.get("type") == "keyup"]
    assert downs == ["Shift", "A", "B"] and ups == ["A", "B", "Shift"]
    assert "Shift" not in kb.modifiers


def test_a_focus_the_page_moves_to_another_text_field_is_followed():
    """A one-time code split across boxes moves the focus on: the keys follow."""
    conn, kb, _ = _kb(["in", "editable"], "123456")
    kb.type("123456")
    assert _typed(conn) == "123456"


def test_a_field_that_left_the_document_stops_the_typing():
    conn, kb, _ = _kb(["gone"], "xy")
    with pytest.raises(cf.FocusLeftField):
        kb.type("xy")
    assert _typed(conn) == "x"
