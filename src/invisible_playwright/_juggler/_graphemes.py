"""Typed text in the units a person enters it: one grapheme cluster at a time.

⛔ A CHARACTER WITH NO KEY WAS INSERTED ONE CODE POINT AT A TIME, and a
cluster is several. invisible_core's `Keyboard.type` (55.50.0, and upstream
main as of 69.82.0) walks the text by Python character and sends one
`Page.insertText` for each code point the layout has no key for. Each insert
is a composition of its own, so the page saw:

- "🇿🇦" (two regional indicators) as two compositions, and Lexical 0.21 and
  0.53 both kept it as "🇿🇦🇦": the second composition's `compositionstart`
  arrived with the first half selected, and the editor duplicated it;
- "🛍️" (U+1F6CD U+FE0F) as the emoji, then a lone VS16 committed by itself;
- "é" written decomposed, or a keycap "1️⃣", as a KEY for the base followed
  by inserted marks, which no keyboard and no picker produces.

An emoji picker, a dead key or an IME commits the whole cluster in one
composition, and that is what this makes the engine do: a cluster that is one
code point the layout has a key for is still pressed as that key; every other
cluster goes in whole, in one `Page.insertText`.

Segmentation follows UAX #29's extended grapheme clusters for what typed text
carries - combining marks and spacing marks, ZWJ sequences, variation
selectors, emoji modifiers, tag sequences, keycaps and regional-indicator
pairs - and never joins anything to a control character, so "\\n" and "\\r"
stay keys (Enter). Not covered: Hangul syllables written as conjoining jamo
and Indic conjunct rules (GB9c); those split where UAX #29 would not, which
is what happened to every cluster before this.
"""
from __future__ import annotations

import unicodedata
from typing import List

_ZWJ = "‍"


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_control(ch: str) -> bool:
    # GB4/GB5: nothing extends a control, and a control extends nothing.
    return ch in "\r\n" or unicodedata.category(ch) in ("Cc", "Zl", "Zp")


def _extends(ch: str) -> bool:
    """GB9/GB9a: what joins the cluster before it."""
    cp = ord(ch)
    if ch == _ZWJ:
        return True
    if unicodedata.category(ch) in ("Mn", "Me", "Mc"):
        return True
    return (0xFE00 <= cp <= 0xFE0F            # variation selectors
            or 0xE0100 <= cp <= 0xE01EF       # variation selectors supplement
            or 0x1F3FB <= cp <= 0x1F3FF       # emoji skin-tone modifiers
            or 0xE0020 <= cp <= 0xE007F)      # tag characters (subdivision flags)


def graphemes(text: str) -> List[str]:
    """`text` split into the clusters a person enters one at a time."""
    out: List[str] = []
    ri_run = 0  # regional indicators in the current cluster
    for ch in text:
        if out:
            prev = out[-1]
            last = prev[-1]
            join = False
            if _is_control(ch) or _is_control(last):
                join = False
            elif _extends(ch):
                join = True
            elif last == _ZWJ and not ch.isspace():
                join = True                       # GB11
            elif _is_regional_indicator(ch) and _is_regional_indicator(last) \
                    and ri_run % 2 == 1:
                join = True                       # GB12/GB13: pairs
            if join:
                out[-1] = prev + ch
                if _is_regional_indicator(ch):
                    ri_run += 1
                continue
        out.append(ch)
        ri_run = 1 if _is_regional_indicator(ch) else 0
    return out
