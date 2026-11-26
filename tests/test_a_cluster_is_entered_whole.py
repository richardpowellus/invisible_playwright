"""A character with no key goes in as its whole grapheme cluster, in one insert.

Before: "🇿🇦" was two `Page.insertText` (two compositions) and Lexical kept it
as "🇿🇦🇦"; "🛍️" committed a lone U+FE0F; "é" written decomposed was the key
`e` and then an inserted U+0301. Text with only keyed characters must send
exactly what it sent before.
"""
from __future__ import annotations

import pytest

import invisible_core
from invisible_core.juggler._behaviour import PageActs
from invisible_core.juggler.keyboard import Keyboard

from invisible_playwright._juggler._grapheme_keyboard import (
    CORE_VERSION, GraphemeKeyboard, install)
from invisible_playwright._juggler._graphemes import graphemes

pytestmark = pytest.mark.unit


class _Fake:
    def __init__(self):
        self.sent = []

    def send(self, method, params, **kw):
        self.sent.append((method, params))
        return {}


def _kb(cls=GraphemeKeyboard):
    c = _Fake()
    return c, cls(c, "S", acts=PageActs(1))


def _what(sent):
    return [("insert", p["text"]) if m == "Page.insertText" else (p["type"], p["key"])
            for m, p in sent]


@pytest.mark.parametrize("text, want", [
    ("🇿🇦", ["🇿🇦"]),
    ("🇿🇦🇺🇸", ["🇿🇦", "🇺🇸"]),
    ("🇿🇦🇦", ["🇿🇦", "🇦"]),
    ("🛍️ a", ["🛍️", " ", "a"]),
    ("é", ["é"]),
    ("1️⃣", ["1️⃣"]),
    ("👍🏽", ["👍🏽"]),
    ("👩‍💻!", ["👩‍💻", "!"]),
    ("🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
     ["🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"]),
    ("a\ń", ["a", "\n", "́"]),
    ("\r\n", ["\r", "\n"]),
    ("Brett's\n\nWhile", list("Brett's\n\nWhile")),
])
def test_text_is_split_where_a_person_enters_it(text, want):
    assert graphemes(text) == want
    assert "".join(graphemes(text)) == text


def test_a_flag_is_one_insert():
    c, kb = _kb()
    kb.type("X 🇿🇦")
    assert _what(c.sent) == [("keydown", "Shift"), ("keydown", "X"), ("keyup", "X"),
                             ("keyup", "Shift"), ("keydown", " "), ("keyup", " "),
                             ("insert", "🇿🇦")]


def test_an_emoji_with_its_variation_selector_is_one_insert_and_the_space_a_key():
    c, kb = _kb()
    kb.type("🛍️ B")
    assert _what(c.sent) == [("insert", "🛍️"), ("keydown", " "), ("keyup", " "),
                             ("keydown", "Shift"), ("keydown", "B"), ("keyup", "B"),
                             ("keyup", "Shift")]


def test_a_base_with_a_combining_mark_is_not_a_key_then_a_mark():
    c, kb = _kb()
    kb.type("é")
    assert _what(c.sent) == [("insert", "é")]


@pytest.mark.parametrize("text", [
    "Brett's Biltong!\n\nWhile stocks last.", "café 中a\U0001f600", "ABC !! a@b",
])
def test_text_without_clusters_sends_what_the_core_sent(text):
    if invisible_core.__version__ != CORE_VERSION:
        pytest.skip("the copied loop is only installed on %s" % CORE_VERSION)
    new_c, new = _kb()
    old_c, old = _kb(Keyboard)
    new.type(text)
    old.type(text)
    assert new_c.sent == old_c.sent


def test_it_is_installed_on_the_core_it_was_copied_from_and_only_there():
    """⛔ When this fails the engine's core pin moved: drop the fork copy if the
    core now types clusters itself, or port `GraphemeKeyboard` to the new loop."""
    assert invisible_core.__version__ == CORE_VERSION, (
        "invisible_core is %s; _grapheme_keyboard copies %s's Keyboard.type"
        % (invisible_core.__version__, CORE_VERSION))
    c = _Fake()
    kb = Keyboard(c, "S", acts=PageActs(1))
    assert install(kb) is True
    assert type(kb) is GraphemeKeyboard
