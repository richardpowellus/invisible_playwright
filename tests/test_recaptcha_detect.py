"""Page evidence is interpreted without a browser or a solving service."""
from __future__ import annotations

import asyncio
import logging
import traceback

import pytest

from invisible_playwright._pw._impl._errors import Error
from invisible_playwright.recaptcha._detect import (
    _assemble,
    _callback_path,
    _Collected,
    _Detected,
    _Facts,
    _frame_facts,
    _merge_facts,
    _parse_anchor_url,
    _parse_inline_scripts,
    _parse_script_src,
    _Snapshot,
    detect_recaptcha,
    detect_recaptcha_async,
)
from invisible_playwright.recaptcha._inject import (
    _check_injection,
    _inject_token,
    _inject_token_async,
)
from invisible_playwright.recaptcha._page import (
    _check_user_agent,
    _select_widget,
    _validate_timeout,
    solve_recaptcha,
    solve_recaptcha_async,
)
from invisible_playwright.recaptcha._types import (
    RecaptchaAmbiguousError,
    RecaptchaChallenge,
    RecaptchaInjectionError,
    RecaptchaNotFoundError,
    RecaptchaSolution,
)

pytestmark = pytest.mark.unit

KEY = "6LcExample_site-key"
URL = "https://shop.example/login"


def _empty() -> _Collected:
    return {"widgets": [], "scripts": [], "inline_scripts": [],
            "clients": [], "user_agent": "Browser UA"}


@pytest.mark.parametrize(
    "host,path,size,enterprise,domain",
    [
        ("www.google.com", "api2", "normal", False, None),
        ("www.google.com", "api2", "invisible", False, None),
        ("www.google.com", "enterprise", "normal", True, None),
        ("www.recaptcha.net", "api2", "invisible", False, "www.recaptcha.net"),
        ("www.recaptcha.net", "enterprise", "invisible", True, "www.recaptcha.net"),
    ],
)
def test_anchor_fields(host, path, size, enterprise, domain):
    fact = _parse_anchor_url(
        f"https://{host}/recaptcha/{path}/anchor?ar=1&k={KEY}&co=aHR0cHM6Ly9zaG9w"
        f"&hl=en&v=release-code&size={size}&sa=log%2Fin&s=opaque%2Bdata%3D")
    assert fact == _Facts(
        sitekey=KEY, kind="v2-invisible" if size == "invisible" else "v2",
        enterprise=enterprise, action="log/in", data_s="opaque+data=",
        api_domain=domain,
    )


@pytest.mark.parametrize("url", [
    "https://www.google.com/recaptcha/api2/anchor?size=normal",
    "https://www.google.com/recaptcha/api2/anchor?k=",
    "https://www.google.com/recaptcha/api2/bframe?k=key",
    "https://www.google.com.evil.example/recaptcha/api2/anchor?k=key",
    "https://evil.example/recaptcha/api2/anchor?k=key",
    "javascript://www.google.com/recaptcha/api2/anchor?k=key",
    "https://[broken/recaptcha/api2/anchor?k=key",
    "about:blank",
])
def test_non_anchor_urls_do_not_create_widgets(url):
    assert _parse_anchor_url(url) is None


@pytest.mark.parametrize("src,key,enterprise,domain", [
    ("https://www.google.com/recaptcha/api.js?render=explicit", None, False, None),
    ("https://www.google.com/recaptcha/api.js", None, False, None),
    (f"https://www.google.com/recaptcha/api.js?render={KEY}", KEY, False, None),
    (f"https://www.google.com/recaptcha/enterprise.js?render={KEY}", KEY, True, None),
    (f"https://www.recaptcha.net/recaptcha/api.js?render={KEY}",
     KEY, False, "www.recaptcha.net"),
    ("https://www.recaptcha.net/recaptcha/enterprise.js?render=explicit",
     None, True, "www.recaptcha.net"),
])
def test_script_metadata(src, key, enterprise, domain):
    assert _parse_script_src(src) == _Facts(
        sitekey=key, kind="v3" if key else None,
        enterprise=enterprise, api_domain=domain)


@pytest.mark.parametrize("src", [
    "https://www.google.com/recaptcha/not-api.js?render=key",
    "https://www.google.com.evil.example/recaptcha/api.js?render=key",
    "https://cdn.example/api.js?render=key",
    "data:text/javascript,grecaptcha.execute('key', {action:'login'})",
    "https://[bad",
])
def test_unrelated_scripts_are_not_recaptcha(src):
    assert _parse_script_src(src) is None


@pytest.mark.parametrize("script,enterprise", [
    ("grecaptcha.execute('KEY', {action: 'login'});", False),
    ('grecaptcha.execute("KEY", {"action": "login", other: 1})', False),
    ("grecaptcha . enterprise . execute (\n'KEY', { 'action': 'login' })", True),
    ("grecaptcha.ready(() => grecaptcha.execute('KEY', {action:'login'}))", False),
])
def test_literal_execute_actions(script, enterprise):
    assert _parse_inline_scripts([script]) == [
        _Facts(sitekey="KEY", kind="v3", enterprise=enterprise, action="login")]


def test_literal_render_options_and_multiple_calls():
    assert _parse_inline_scripts([
        ("grecaptcha.render('container', {sitekey:'A', size:'invisible', "
        "callback:'handlers.solved', action:'checkout'});"
        "grecaptcha.enterprise.execute('B', {action:'login'});")
    ]) == [
        _Facts(sitekey="A", kind="v2-invisible", callback="handlers.solved",
               action="checkout"),
        _Facts(sitekey="B", kind="v3", enterprise=True, action="login"),
    ]


def test_inline_parser_does_not_invent_variable_values_or_scan_unbounded_text():
    assert _parse_inline_scripts([
        ("grecaptcha.execute(variable, {action: 'login'});"
        "grecaptcha.render('container', {sitekey: variable});")
    ]) == []
    assert _parse_inline_scripts([
        " " * 65536, "grecaptcha.execute('KEY', {action:'login'})",
    ]) == []


def test_merge_enriches_one_key_and_preserves_v3_over_invisible_anchor():
    facts = [
        _Facts(enterprise=True, api_domain="www.recaptcha.net"),
        _Facts(sitekey=KEY, kind="v3", action="login"),
        _Facts(sitekey=KEY, kind="v2", callback="onSolved"),
        _Facts(sitekey=KEY, kind="v2-invisible", data_s="opaque", action="anchor-action"),
    ]
    assert _merge_facts(facts, website_url=URL, user_agent="UA") == [
        RecaptchaChallenge(
            URL, KEY, "v3", enterprise=True, action="anchor-action", data_s="opaque",
            api_domain="www.recaptcha.net", callback="onSolved", user_agent="UA",
        )]


def test_keyed_script_metadata_does_not_leak_to_other_widgets():
    challenges = _merge_facts([
        _Facts(sitekey="A", kind="v3", enterprise=True, api_domain="www.recaptcha.net"),
        _Facts(sitekey="B", kind="v2"),
    ], website_url=URL, user_agent=None)
    assert [c.sitekey for c in challenges] == ["A", "B"]
    assert challenges[1] == RecaptchaChallenge(URL, "B", "v2")
    assert _merge_facts([_Facts(enterprise=True)], website_url=URL,
                        user_agent=None) == []


def test_client_versions_callback_paths_and_dom_precedence():
    data = _empty()
    path = ["___grecaptcha_cfg", "clients", "10000", "a", "b", "promise-callback"]
    data["clients"] = [
        {"id": "10000", "sitekey": "A", "size": "invisible", "callback": "<function>",
         "callback_path": path},
        {"id": "1", "sitekey": "B", "size": "invisible", "callback": "global.done",
         "callback_path": ["___grecaptcha_cfg", "clients", "1", "callback"]},
    ]
    data["widgets"] = [
        {"sitekey": "B", "size": "normal", "action": "checkout",
         "s": "data-s", "callback": "preferred.callback"},
        {"sitekey": "", "size": None, "action": None, "s": None, "callback": None},
    ]
    a, b = _merge_facts(_frame_facts(data), website_url=URL, user_agent="UA")
    assert a.kind == "v3"
    assert a.callback == _callback_path(path)
    assert b.kind == "v2-invisible"
    assert b.callback == "preferred.callback"
    assert (b.action, b.data_s) == ("checkout", "data-s")


def test_anchors_belong_to_parent_and_same_key_in_other_frames_is_not_deduped():
    main, child, anchor, other = object(), object(), object(), object()
    child_data = _empty()
    child_data["scripts"] = [f"https://www.google.com/recaptcha/api.js?render={KEY}"]
    other_data = _empty()
    other_data["scripts"] = child_data["scripts"]
    found = _assemble([
        _Snapshot(main, None, URL, _empty()),
        _Snapshot(child, main, URL + "/embedded", child_data),
        _Snapshot(anchor, child,
                  f"https://www.google.com/recaptcha/api2/anchor?k={KEY}&s=secret", None),
        _Snapshot(other, main, URL + "/other", other_data),
    ])
    assert len(found) == 2
    assert found[0].frame is child
    assert found[0].challenge.website_url == URL + "/embedded"
    assert found[0].challenge.data_s == "secret"
    assert found[0].challenge.kind == "v3"
    assert found[0].challenge.user_agent == "Browser UA"
    assert found[1].frame is other
    assert found[1].challenge.data_s is None


def test_orphan_anchor_does_not_claim_its_google_url_is_a_customer_website():
    assert _assemble([_Snapshot(
        object(), None, f"https://www.google.com/recaptcha/api2/anchor?k={KEY}", _empty(),
    )]) == []


def test_duplicate_frame_snapshots_still_describe_one_widget():
    data = _empty()
    data["scripts"] = [f"https://www.google.com/recaptcha/api.js?render={KEY}"]
    snapshot = _Snapshot(object(), None, URL, data)
    assert len(_assemble([snapshot, snapshot])) == 1


def test_selection_is_explicit_and_rejects_multiple_frames_with_the_same_key():
    first = _Detected(RecaptchaChallenge(URL, "A", "v2"), object())
    second = _Detected(RecaptchaChallenge(URL, "B", "v3"), object())
    assert _select_widget([first], None) is first
    assert _select_widget([first, second], "B") is second
    with pytest.raises(RecaptchaAmbiguousError):
        _select_widget([first, second], None)
    with pytest.raises(RecaptchaAmbiguousError):
        _select_widget([first, _Detected(first.challenge, object())], "A")
    with pytest.raises(RecaptchaNotFoundError):
        _select_widget([], None)
    with pytest.raises(RecaptchaNotFoundError):
        _select_widget([first], "unknown")


@pytest.mark.parametrize("timeout", [0, -1, 301, float("inf"), float("nan"), True, "120", None])
def test_invalid_timeouts_fail_before_detection(timeout):
    with pytest.raises(ValueError, match="greater than 0 and at most 300"):
        solve_recaptcha(None, timeout=timeout)
    with pytest.raises(ValueError, match="greater than 0 and at most 300"):
        asyncio.run(solve_recaptcha_async(None, timeout=timeout))


@pytest.mark.parametrize("timeout", [0.001, 120, 300])
def test_valid_timeout_boundaries(timeout):
    _validate_timeout(timeout)


def test_user_agent_mismatch_log_contains_no_credentials(caplog):
    solution = RecaptchaSolution(
        "private-response", "fake", user_agent="solver-UA",
        cookies={"recaptcha-ca-t": "private-cookie"},
    )
    with caplog.at_level(logging.DEBUG, logger="invisible_playwright.recaptcha._page"):
        _check_user_agent(RecaptchaChallenge(URL, KEY, "v2", user_agent="browser-UA"), solution)
    assert "differs from the browser" in caplog.text
    assert solution.token not in caplog.text
    assert "private-cookie" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="invisible_playwright.recaptcha._page"):
        _check_user_agent(RecaptchaChallenge(URL, KEY, "v2", user_agent="solver-UA"), solution)
    assert not caplog.records


def test_injection_requires_a_field_or_callback():
    assert _check_injection({"filled": 0, "callback": "onSolved"})["filled"] == 0
    assert _check_injection({"filled": 2, "callback": None})["callback"] is None
    with pytest.raises(RecaptchaInjectionError, match="No reCAPTCHA"):
        _check_injection({"filled": 0, "callback": None})


def test_injection_errors_never_expose_page_exceptions_or_empty_tokens():
    token = "private-response-that-a-callback-threw"

    class Failing:
        def evaluate(self, *args):
            raise Error(token)

    class AsyncFailing:
        async def evaluate(self, *args):
            raise Error(token)

    for call in (
        lambda: _inject_token(Failing(), sitekey=KEY, website_url=URL,
                              token=token, invoke_callback=True),
        lambda: asyncio.run(_inject_token_async(
            AsyncFailing(), sitekey=KEY, website_url=URL, token=token, invoke_callback=True)),
    ):
        with pytest.raises(RecaptchaInjectionError) as caught:
            call()
        assert token not in "".join(traceback.format_exception(caught.value))
    with pytest.raises(RecaptchaInjectionError, match="empty"):
        _inject_token(None, sitekey=KEY, website_url=URL, token="", invoke_callback=True)
    with pytest.raises(RecaptchaInjectionError, match="empty"):
        asyncio.run(_inject_token_async(None, sitekey=KEY, website_url=URL,
                                        token="", invoke_callback=True))


def test_both_collectors_read_once_and_skip_playwright_frame_errors(caplog):
    data = _empty()
    data["scripts"] = [f"https://www.google.com/recaptcha/api.js?render={KEY}"]

    class Frame:
        parent_frame = None
        url = URL

        def __init__(self, failing=False):
            self.failing = failing
            self.reads = 0

        def evaluate(self, script):
            self.reads += 1
            if self.failing:
                raise Error("Execution context was destroyed")
            return data

    class AsyncFrame(Frame):
        async def evaluate(self, script):
            return super().evaluate(script)

    class Page:
        def __init__(self, frame_type):
            good = frame_type()
            self.frames = [frame_type(True), good, good]

    sync_page, async_page = Page(Frame), Page(AsyncFrame)
    with caplog.at_level(logging.DEBUG, logger="invisible_playwright.recaptcha._detect"):
        sync = detect_recaptcha(sync_page)
        asyn = asyncio.run(detect_recaptcha_async(async_page))
    assert sync == asyn == [RecaptchaChallenge(URL, KEY, "v3", user_agent="Browser UA")]
    assert [f.reads for f in sync_page.frames + async_page.frames] == [1] * 6
    assert len(caplog.records) == 2


def test_no_script_runs_inside_recaptchas_own_frames():
    """The anchor is read from its URL; reCAPTCHA's documents are not evaluated in."""
    data = _empty()
    data["widgets"] = [{"sitekey": KEY, "size": None, "action": None, "s": None,
                        "callback": None}]
    anchor_url = (f"https://www.google.com/recaptcha/api2/anchor?ar=1&k={KEY}"
                  "&size=invisible&sa=submit")

    class Frame:
        def __init__(self, url, parent=None):
            self.url, self.parent_frame, self.reads = url, parent, 0

        def evaluate(self, script):
            self.reads += 1
            return data

    class Page:
        def __init__(self):
            main = Frame(URL)
            self.frames = [main, Frame(anchor_url, main),
                           Frame("https://www.google.com/recaptcha/api2/bframe?k=" + KEY, main)]

    page = Page()
    [challenge] = detect_recaptcha(page)
    assert [f.reads for f in page.frames] == [1, 0, 0]
    assert (challenge.kind, challenge.action) == ("v2-invisible", "submit")
