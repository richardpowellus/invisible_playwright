"""Fork-only: the keyboard types text one grapheme cluster at a time.

See `_graphemes` for the defect. This is the pinned invisible_core's
`Keyboard.type`, unchanged except that it walks clusters instead of code
points: a cluster that is one character with a key is pressed exactly as
before (same plan, same Shift runs, same gaps), and any other cluster goes in
whole through one `insert_text`.

⛔ IT IS A COPY OF ONE CORE'S LOOP, SO IT RUNS ON THAT LOOP ONLY. Installed
over a different core it would replace that core's typing (upstream 69.x sends
every key at its planned instant, `_stroke`) with this one. So `install`
compares a SHA-256 of the core's own `Keyboard.type` source with the one this
was copied from, refuses on any difference and leaves the core's keyboard in
place, and `test_a_cluster_is_entered_whole` fails when the core changes,
which is the prompt to drop this file once the core types clusters itself
(the upstream core PR) or to port it.
"""
from __future__ import annotations

import hashlib
import inspect

from invisible_core.juggler import keyboard as _kb
from invisible_core.juggler.keyboard import Keyboard, LAYOUT_CLOSURE

from ._graphemes import graphemes

#: SHA-256 of the source of the core `Keyboard.type` this file copies. A
#: digest, not a version: the core version is declared in pyproject.toml only
#: (`tests/test_core_pin.py`), and the loop is what has to match.
COPIED_TYPE_SHA256 = "534cf5aa30245033f28fc7f4ed2aa5c506061df84f4be62fd70f288d5268be00"


def core_type_sha256() -> str:
    try:
        return hashlib.sha256(inspect.getsource(Keyboard.type).encode()).hexdigest()
    except (OSError, TypeError):  # no source to read: cannot show it matches
        return ""


class GraphemeKeyboard(Keyboard):
    #: Called with (index, units) before every unit after the first, when
    #: set: `CredentialActions` uses it to check the field still has the
    #: focus before a key goes out (`_credential_fill._FocusGuard`).
    guard = None

    def type(self, text: str, *, delay_ms: float = 0.0) -> None:
        units = graphemes(text)
        # The plan draws one (dwell, gap) per unit. A unit with a key is its
        # own character, so ordinary text draws exactly the plan it drew
        # before; a cluster with no key stands in as its first code point,
        # which has no hand, as it had none before either.
        plan = self._plan("".join(u[0] for u in units))
        keyed = [len(u) == 1 and u in LAYOUT_CLOSURE for u in units]
        shifted = [k and self.needs_shift(u) and "Shift" not in self.modifiers
                   for u, k in zip(units, keyed)]
        n = len(units)
        starts = [s and (i == 0 or not shifted[i - 1]) for i, s in enumerate(shifted)]
        ends = [s and (i == n - 1 or not shifted[i + 1]) for i, s in enumerate(shifted)]
        timing = iter(self._plan_shift(sum(starts)))
        lead_at, lag_at = {}, {}
        for i in range(n):
            if starts[i]:
                lead_at[i], lag = next(timing)
            if ends[i]:
                lag_at[i] = lag
        for i, unit in enumerate(units):
            dwell_ms, gap_ms = plan[i]
            if i and self.guard is not None:
                try:
                    self.guard(i, units)
                except BaseException:
                    # Shift is held between a run's first and last key: a
                    # stop inside a run must not leave it down.
                    if shifted[i - 1] and not ends[i - 1]:
                        self.up("Shift")
                    raise
            if starts[i]:
                self.down("Shift")
                _kb._sleep_ms(lead_at[i])
            if keyed[i]:
                self.press(unit, dwell_ms=dwell_ms)
            else:
                self.insert_text(unit)
            if i == n - 1:
                if ends[i]:
                    _kb._sleep_ms(lag_at[i])
                    self.up("Shift")
                break
            wait_ms = delay_ms if delay_ms else gap_ms
            if ends[i]:
                _kb._sleep_ms(lag_at[i])
                self.up("Shift")
                wait_ms -= lag_at[i]
            if starts[i + 1]:
                wait_ms -= lead_at[i + 1]
            _kb._sleep_ms(wait_ms)


def install(keyboard) -> bool:
    """Make `keyboard` type clusters, when it is the core this was copied
    from. Answers whether it did."""
    if core_type_sha256() != COPIED_TYPE_SHA256 or type(keyboard) is not Keyboard:
        return False
    keyboard.__class__ = GraphemeKeyboard
    return True
