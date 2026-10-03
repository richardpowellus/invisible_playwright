"""A page's events from before its channel existed are delivered once, in order.

A page the site opens has usually requested and been answered its document
before its Page object exists, so the page holds its own events while it is
built and hands its Network events on once it can emit them. One that arrived
between the page subscribing and the browser's replay marking the session live
is held twice - live, and again from the browser's buffer - and the second
copy sits behind events older than it. These pin the hand-over without a
browser; the e2e file next door holds the outcome against a real one.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from invisible_playwright._juggler import server
from invisible_playwright._juggler.connection import EventListeners
from invisible_playwright._juggler.dispatcher import ProtocolException
from invisible_playwright._juggler.server import BrowserDispatcher, PageDispatcher, Server


def _page(held, replayed=()):
    page = PageDispatcher.__new__(PageDispatcher)
    page.session = "S1"
    page._held = list(held)
    page._held_lock = threading.Lock()
    page._replayed = list(replayed)
    page.got = []
    page._on_juggler_event = lambda m, p: page.got.append((m, p["n"]))
    return page


def _drain(page):
    # `_install_events` is the hand-over; `conn` is not touched by it any more.
    PageDispatcher._install_events(page)


@pytest.mark.unit
def test_a_twice_held_event_is_delivered_once_at_its_last_place():
    older = {"n": 1}
    racing = {"n": 2}
    later = {"n": 3}
    page = _page([("Network.responseReceived", racing),
                  ("Network.requestWillBeSent", older),
                  ("Network.responseReceived", racing),
                  ("Network.requestFinished", later)])
    _drain(page)
    assert page.got == [("Network.requestWillBeSent", 1),
                        ("Network.responseReceived", 2),
                        ("Network.requestFinished", 3)]


@pytest.mark.unit
def test_only_network_events_are_handed_on_and_holding_stops():
    page = _page([("Page.frameAttached", {"n": 1}),
                  ("Network.requestWillBeSent", {"n": 2})])
    _drain(page)
    assert page.got == [("Network.requestWillBeSent", 2)]
    assert page._held is None
    PageDispatcher._route_juggler_event(page, "Network.requestFinished", {"n": 3}, "S1")
    PageDispatcher._route_juggler_event(page, "Network.requestFinished", {"n": 4}, "S2")
    assert page.got[-1] == ("Network.requestFinished", 3) and len(page.got) == 2


@pytest.mark.unit
def test_while_building_events_are_held_not_handled():
    page = _page([])
    PageDispatcher._route_juggler_event(page, "Network.requestWillBeSent", {"n": 1}, "S1")
    assert page.got == [] and len(page._held) == 1


@pytest.mark.unit
def test_a_live_event_during_replay_follows_the_older_buffered_events():
    older, newer = {"n": 1}, {"n": 2}
    page = _page(
        [("Network.responseReceived", newer),
         ("Network.requestWillBeSent", older)],
        replayed=[("Network.requestWillBeSent", older)])
    _drain(page)
    assert page.got == [
        ("Network.requestWillBeSent", 1), ("Network.responseReceived", 2)]


@pytest.mark.unit
def test_a_buffered_event_delivered_live_after_the_handover_is_not_repeated():
    params = {"n": 1}
    event = ("Network.requestWillBeSent", params)
    page = _page([event], replayed=[event])
    _drain(page)
    # The reader had already buffered this event, then paused before reaching
    # the page subscriber. Its original dispatch resumes after the replay.
    page._route_juggler_event(*event, "S1")
    assert page.got == [("Network.requestWillBeSent", 1)]
    # Equal payloads from different events are not duplicates.
    page._route_juggler_event("Network.requestWillBeSent", {"n": 1}, "S1")
    assert len(page.got) == 2


@pytest.mark.unit
def test_overflow_and_replay_failures_are_not_silent():
    page = _page([("Network.requestWillBeSent", {"n": i})
                  for i in range(PageDispatcher.HELD_CAP)])
    with pytest.raises(ProtocolException, match="buffer overflow"):
        page._route_juggler_event("Network.responseReceived", {"n": 999}, "S1")
    page = _page([("Network.requestWillBeSent", {"n": 1})])
    page._on_juggler_event = Mock(side_effect=RuntimeError("cannot emit response"))
    with pytest.raises(RuntimeError, match="cannot emit response"):
        _drain(page)


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["install", "frame", "page"])
def test_failed_page_construction_unsubscribes_and_disposes(monkeypatch, stage):
    conn = EventListeners()
    conn.send = Mock(return_value={})
    browser = BrowserDispatcher(Server(), None, conn, "151.0")
    context = SimpleNamespace(browser=browser)
    before = list(conn._listeners)
    cleanup = Mock()

    def fail(page, *args):
        if stage == "frame":
            page.frame = SimpleNamespace(dispose=cleanup)
        else:
            page.guid = "partial-page"
            page.dispose = cleanup
        raise RuntimeError("construction failed")

    if stage == "install":
        monkeypatch.setattr(server.InjectedScript, "install",
                            Mock(side_effect=RuntimeError("construction failed")))
    else:
        monkeypatch.setattr(PageDispatcher, "_build", fail)
    with pytest.raises(RuntimeError, match="construction failed"):
        PageDispatcher(browser.server, context, "S1", "T1")
    assert conn._listeners == before
    assert cleanup.call_count == (0 if stage == "install" else 1)


@pytest.mark.unit
@pytest.mark.parametrize("closed", ["page", "context"])
def test_a_target_closed_during_construction_cannot_stay_open(monkeypatch, closed):
    conn = EventListeners()
    conn.send = Mock(return_value={})
    browser = BrowserDispatcher(Server(), None, conn, "151.0")
    context = SimpleNamespace(
        context_id="C1", disposed=False, pages=[], emit=Mock())
    browser.contexts.append(context)
    browser._sessions["T1"] = "S1"
    page = SimpleNamespace(
        session="S1", context=context, channel={"guid": "P1"},
        announce_closed=Mock())

    def construct(*args, **kwargs):
        if closed == "page":
            browser._route_browser_event(
                "Browser.detachedFromTarget",
                {"targetId": "T1", "sessionId": "S1"}, None)
        else:
            context.disposed = True
        return page

    monkeypatch.setattr(server, "PageDispatcher", construct)
    browser._adopt_opened_page(
        {"targetId": "T1", "browserContextId": "C1", "openerId": "T0"}, "S1")
    page.announce_closed.assert_called_once_with()
    assert context.pages == []
    assert conn.handler_errors == []
    if closed == "page":
        context.emit.assert_called_once_with("page", {"page": page.channel})
    else:
        context.emit.assert_not_called()


@pytest.mark.unit
def test_adoption_failure_is_recorded(monkeypatch):
    conn = EventListeners()
    conn.send = Mock(return_value={})
    browser = BrowserDispatcher(Server(), None, conn, "151.0")
    browser.contexts.append(SimpleNamespace(
        context_id="C1", disposed=False, pages=[]))
    monkeypatch.setattr(server, "PageDispatcher",
                        Mock(side_effect=RuntimeError("construction failed")))
    browser._adopt_opened_page(
        {"targetId": "T1", "browserContextId": "C1"}, "S1")
    assert conn.handler_errors == ["adopt opened page: construction failed"]
