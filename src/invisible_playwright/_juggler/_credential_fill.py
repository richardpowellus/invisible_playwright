"""Fork-only credential fills; all ordinary actions stay in invisible_core."""
from __future__ import annotations

import time

from invisible_core.juggler.actions import ActionMissed, Actions, ElementNotActionable
from invisible_core.juggler.connection import ProtocolError, TargetClosedError
from invisible_core.juggler.injected import EvaluationError, UTILITY_WORLD

from .._origin import redact_fill_value, validate_fill_expectations
from ._grapheme_keyboard import install as _type_graphemes


_CHECK_FILL_EXPECTATIONS = """function checkFillExpectations(
    injected, element, snapshot, expectOrigin, expectInputType, value) {
  const actualOrigin = element.ownerDocument.defaultView?.origin ?? "unavailable";
  const refused = reason => ({status: "error:origin: " + reason, actualOrigin});
  if (!element.isConnected || element.ownerDocument !== snapshot.document ||
      element.ownerDocument !== document ||
      element.ownerDocument.defaultView?.document !== document ||
      element.ownerDocument.location.origin !== expectOrigin ||
      actualOrigin === "null" || actualOrigin !== expectOrigin)
    return refused("origin mismatch, opaque origin or target detached/stale");
  if (element.nodeName !== snapshot.nodeName || element.type !== snapshot.type)
    return refused("input kind changed");
  if (expectInputType !== null &&
      (element.nodeName !== "INPUT" ||
       element.type.toLowerCase() !== expectInputType.toLowerCase()))
    return refused("input type does not match expect_input_type");
  if (element.nodeName !== "INPUT" && element.nodeName !== "TEXTAREA")
    return refused("expected an input or textarea");
  if (element.nodeName === "INPUT" && !new Set([
    "email", "number", "password", "search", "tel", "text", "url",
    "color", "date", "time", "datetime-local", "month", "range", "week"
  ]).has(element.type))
    return refused("input type cannot be filled");
  if (value !== null) {
    // Do not clone a customized built-in or copy event handlers onto a probe.
    const probe = document.createElement(element.nodeName.toLowerCase());
    for (const name of element.getAttributeNames()) {
      if (!name.startsWith("on"))
        probe.setAttribute(name, element.getAttribute(name));
    }
    probe.value = value;
    if (probe.value !== value)
      return refused("value is not valid for this control");
  }
  return {status: "done", actualOrigin};
}"""


#: Where the focus is, relative to the field being typed into: "in" when the
#: field (or, for editable content, a node inside it or the editing host
#: around it) holds it; "editable" when the page moved it to another text
#: field, as a one-time code split across boxes does (the keys follow it
#: there, as a person's would, and the MCP reports how much the first box
#: kept); "gone" when the field left the document; else a short description
#: of what has it. Read in the utility world: the page sees no
#: event and none of its own code runs.
_FOCUS_JS = (
    "(injected, el) => {"
    "  if (!el.isConnected) return 'gone';"
    "  const r = el.getRootNode ? el.getRootNode() : document;"
    "  const a = r.activeElement;"
    "  if (a && (a === el || el.contains(a) || (a.isContentEditable && a.contains(el))))"
    "    return 'in';"
    "  const t = a ? (a.type || '').toLowerCase() : '';"
    "  if (a && (a.isContentEditable || a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' &&"
    "      ['', 'text', 'search', 'tel', 'url', 'email', 'number', 'password'].includes(t))))"
    "    return 'editable';"
    "  if (!a || a === document.body) return 'the page itself';"
    "  const label = a.getAttribute('aria-label') || a.getAttribute('name') || '';"
    "  return '<' + a.tagName.toLowerCase() + (a.id ? '#' + a.id : '') +"
    "    (label ? ' ' + JSON.stringify(label.slice(0, 60)) : '') + '>'; }")

#: How long a field that lost the focus mid-typing has to get it back before
#: typing stops. Editors that rebuild their DOM (Draft.js resolving a
#: composition) hand it back within a few frames.
FOCUS_RETURN_S = 2.0
_FOCUS_POLL_S = 0.05


class FocusLeftField(ActionMissed):
    """Typing stopped because the field no longer had the focus.

    ⛔ NOT RETRYABLE, for `ActionMissed`'s reason: part of the text is in the
    field. And NOT IGNORED: a key sent while another element has the focus
    goes to that element. On 2026-11-26 Meta Business Suite's composer lost the
    focus partway through a 356-character `fill`; the keys after it reached
    the page's own keyboard shortcuts and it navigated to Leads Center, while
    characters went missing from the post (bug report, BBDTX Advertising).
    """


class _FocusGuard:
    """Called before every key of a `fill`: the field must still have focus."""

    def __init__(self, actions, frame_id, element, total: int):
        self.a, self.f, self.el, self.total = actions, frame_id, element, total

    def where(self) -> str:
        try:
            got = self.a.inj.call(self.f, _FOCUS_JS, {"objectId": self.el})
        except Exception as exc:  # the field's world went away
            return "unreadable (%s)" % type(exc).__name__
        return got if isinstance(got, str) else "unreadable"

    def __call__(self, index: int, units) -> None:
        seen = self.where()
        if seen in ("in", "editable"):
            return
        until = time.monotonic() + FOCUS_RETURN_S
        while time.monotonic() < until:
            time.sleep(_FOCUS_POLL_S)
            seen = self.where()
            if seen in ("in", "editable"):
                return
        typed = sum(len(u) for u in units[:index])
        raise FocusLeftField(
            "fill: typing stopped after %d of %d characters: the field lost the "
            "focus to %s and did not get it back within %.0f s, so the rest was "
            "not typed anywhere" % (typed, self.total, seen, FOCUS_RETURN_S))


#: Where a click would leave the selection in an editor, instead of around
#: its root. The injected `fill` selects an editable host with
#: `selectNodeContents(host)`, so both ends of the selection sit ON the host,
#: outside the blocks an editor keeps inside it. A key is fine with that (an
#: editor applies it to its own model), but a composition is not: Firefox
#: commits it natively over the selected range, which removes the editor's
#: block wrappers. Draft.js (Meta Business Suite's composer) then threw on every
#: event and dropped every edit it made itself - spaces after emoji, the
#: apostrophe, newlines - for the rest of the text (2026-11-26). So the ends
#: are moved into the first and last leaf, and an empty field gets a caret
#: inside its block, where a person's click puts it. A selection the page
#: already placed elsewhere is left alone.
_EDITOR_SELECTION_JS = (
    "(injected, el) => {"
    "  if (!el.isContentEditable) return 'not-editable';"
    "  const s = el.ownerDocument.getSelection();"
    "  if (!s || !s.rangeCount) return 'no-selection';"
    "  const r = s.getRangeAt(0);"
    "  if (r.startContainer !== el && r.endContainer !== el) return 'kept';"
    "  const first = (n) => { for (;;) { const c = n.firstChild;"
    "      if (!c) return [n, 0]; if (c.nodeType === 1 && c.tagName === 'BR') return [n, 0];"
    "      if (c.nodeType !== 1 && c.nodeType !== 3) return [n, 0]; n = c; } };"
    "  const last = (n) => { for (;;) { const c = n.lastChild;"
    "      if (!c) return [n, n.nodeType === 3 ? n.data.length : 0];"
    "      if (c.nodeType === 1 && c.tagName === 'BR') return [n, n.childNodes.length - 1];"
    "      if (c.nodeType !== 1 && c.nodeType !== 3) return [n, n.childNodes.length]; n = c; } };"
    "  const [a, ao] = first(el);"
    "  const empty = el.textContent.replace(/[\\u200b\\ufeff]/g, '') === '';"
    "  const [b, bo] = empty ? [a, ao] : last(el);"
    "  s.setBaseAndExtent(a, ao, b, bo);"
    "  return empty ? 'caret' : 'leaves'; }")


class CredentialActions(Actions):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Fork-only: text is typed one grapheme cluster at a time
        # (`_grapheme_keyboard`), so "🇿🇦" is one insert, not two.
        _type_graphemes(self.keyboard)
        self._typing_target = None

    def _reach_field(self, f, element, deadline):
        # `fill` reaches its field and then types into it; remember which, so
        # `_type` can keep checking it still has the focus.
        self._typing_target = (f, element)
        return super()._reach_field(f, element, deadline)

    def _type(self, text, delay_ms=None):
        target, self._typing_target = self._typing_target, None
        if target is None or not hasattr(self.keyboard, "guard"):
            return super()._type(text, delay_ms)
        try:
            self.inj.call(target[0], _EDITOR_SELECTION_JS, {"objectId": target[1]})
        except EvaluationError:
            pass  # the field went away: the focus guard says so at the next key
        self.keyboard.guard = _FocusGuard(self, target[0], target[1], len(text))
        try:
            return super()._type(text, delay_ms)
        finally:
            self.keyboard.guard = None

    def fill(self, selector: str, text: str, *, timeout: float = 30.0,
             frame_id: str | None = None, element_id: str | None = None,
             expect_origin: str | None = None,
             expect_input_type: str | None = None, **opts):
        if expect_origin is None and expect_input_type is None:
            return super().fill(selector, text, timeout=timeout,
                                frame_id=frame_id, element_id=element_id, **opts)
        validate_fill_expectations(expect_origin, expect_input_type)
        return self._fill_with_origin(
            selector, text, expect_origin, timeout=timeout,
            frame_id=frame_id, element_id=element_id,
            expect_input_type=expect_input_type, **opts)

    def _guarded_retry(self, selector, run, *, timeout, frame_id, element_id,
                       strict=False, force=False, trial=False, position=None):
        """Retry only resolution/actionability, using public core APIs.

        The core's callback loop is private. This no-pointer path owns its
        resolution loop instead; the guard and commit run outside its catch.
        """
        f = frame_id or self.lifecycle.main_frame
        if f is None:
            raise RuntimeError("no main frame: the page isn't ready")
        deadline = time.monotonic() + timeout
        turns = 0
        while True:
            turns += 1
            element = None
            ready = False
            try:
                element = (element_id if element_id is not None else
                           self.inj.query_selector(f, selector, strict=strict))
                if not element:
                    reason = "the selector finds nothing"
                elif force:
                    ready = True
                else:
                    result = self.inj.element_states(
                        f, element, ["visible", "stable", "enabled", "editable"])
                    ready = result.get("ok")
                    reason = "missing %s" % result.get("missing", "a state")
            except EvaluationError as error:
                if "notconnected" not in str(error):
                    raise
                reason = "the node detached while I was resolving it"
            else:
                if ready:
                    return None if trial else run(f, element, None)
            finally:
                if element and element_id is None:
                    self.inj.dispose(f, element)
            if time.monotonic() > deadline:
                raise ElementNotActionable(
                    "%r not actionable in %.0fs after %d attempts. Last "
                    "reason: %s" % (selector, timeout, turns, reason))
            time.sleep(0.05)

    def _fill_with_origin(self, selector, text, expect_origin, *, timeout,
                          frame_id, element_id, expect_input_type=None, **opts):
        """Check around a native commit without focusing or synthesizing events.

        These calls are not atomic. Clearing after a detected failure cannot
        undo disclosure to a page listener, so the write outcome stays unknown.
        """
        actual = "unavailable"
        outcome = "nothing was written"

        def run(f, element, point):
            nonlocal actual, outcome
            target = self.inj.call(
                f, "(injected, el) => injected.retarget(el, 'follow-label')",
                {"objectId": element}, by_value=False)
            if not target:
                raise EvaluationError("error:origin: target detached or stale")
            snapshot = None
            try:
                snapshot = self.inj.call(
                    f, "(injected, el) => ({document: el.ownerDocument, "
                    "nodeName: el.nodeName, type: el.type})",
                    {"objectId": target}, by_value=False)
                if not snapshot:
                    raise EvaluationError("error:origin: target unavailable")

                def check(value):
                    return self.inj.call(
                        f, _CHECK_FILL_EXPECTATIONS,
                        {"objectId": target}, {"objectId": snapshot},
                        expect_origin, expect_input_type, value)

                before = check(text)
                actual = before["actualOrigin"]
                if before["status"] != "done":
                    raise EvaluationError(before["status"])

                outcome = "write outcome unknown"
                try:
                    self.c.send("Page.setUserInput",
                                {"frameId": f, "objectId": target, "value": text},
                                session=self.session, timeout=10)
                    after = check(None)
                    actual = after["actualOrigin"]
                    if after["status"] != "done":
                        raise EvaluationError(after["status"])
                except (RuntimeError, TimeoutError, TargetClosedError):
                    # A failed reply is not evidence that the browser did not write.
                    try:
                        self.c.send("Page.setUserInput",
                                    {"frameId": f, "objectId": target, "value": ""},
                                    session=self.session, timeout=10)
                        empty = self.inj.call(
                            f, "(injected, el) => el.value === ''",
                            {"objectId": target})
                        outcome += ("; target cleared" if empty is True
                                    else "; clearing did not leave the target empty")
                    except (RuntimeError, TimeoutError, TargetClosedError):
                        outcome += "; native clearing failed or could not be verified"
                    raise EvaluationError(
                        "error:origin: native commit or post-check failed") from None
                return "done"
            finally:
                if snapshot:
                    self.inj.dispose(f, snapshot)
                self.inj.dispose(f, target)

        try:
            return self._guarded_retry(
                selector, run, element_id=element_id,
                timeout=timeout, frame_id=frame_id, **opts)
        except (RuntimeError, TimeoutError, TargetClosedError) as error:
            reason = (
                str(error) if str(error).startswith("error:origin:")
                else f"{type(error).__name__}: target unavailable, detached or stale"
            )
            f = frame_id or self.lifecycle.main_frame
            if actual == "unavailable" and (f, UTILITY_WORLD) in self.inj.contexts:
                try:
                    actual = self.inj.evaluate(f, "document.defaultView.origin", timeout=1.0)
                except (EvaluationError, ProtocolError, TimeoutError, TargetClosedError):
                    actual = "unavailable (document no longer accessible)"
            message = (
                f"fill expect_origin={expect_origin!r}, actual origin={actual!r}: "
                f"{reason}; {outcome}"
            )
            raise type(error)(redact_fill_value(message, text)) from None
