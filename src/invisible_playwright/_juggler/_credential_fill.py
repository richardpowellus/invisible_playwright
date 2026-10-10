"""Fork-only credential fills; all ordinary actions stay in invisible_core."""
from __future__ import annotations

import time

from invisible_core.juggler.actions import Actions, ElementNotActionable
from invisible_core.juggler.connection import ProtocolError, TargetClosedError
from invisible_core.juggler.injected import EvaluationError, UTILITY_WORLD

from .._origin import redact_fill_value, validate_fill_expectations


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


class CredentialActions(Actions):
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
