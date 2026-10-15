"""Real frame collection and injection, with no request leaving loopback.

These fixtures own their fake widget scripts and callbacks. No solving service
or Google endpoint is contacted, so the integration does not depend on a paid
account, token lifetime or the public demo's current challenge policy.

⛔ NO REQUEST INTERCEPTION. The pages name Google's script URLs exactly as a
real site does, and a Content-Security-Policy keeps the browser from fetching
them: detection reads `script.src`, which a blocked script still carries, and
the API stub is inlined. The first version fulfilled those URLs with
`page.route("**/*")` instead, and failed about one run in five with a `goto`
that never reached load - measured 2026-10-14, the same failure reproduces with
no reCAPTCHA code at all (a route that only calls `continue_()`, 5 of 60
navigations: load timeouts and `NS_BINDING_ABORTED` from `continue_`). This
feature never routes, so its tests do not either. The anchor iframe's URL
cannot survive the policy (a blocked frame reports an empty URL), so the
anchor path is covered by tests/test_recaptcha_detect.py.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
from urllib.parse import urlsplit

import pytest

from invisible_playwright import InvisiblePlaywright
from invisible_playwright.async_api import InvisiblePlaywright as AsyncIP
from invisible_playwright.recaptcha._detect import (
    detect_recaptcha,
    detect_recaptcha_async,
)
from invisible_playwright.recaptcha._page import solve_recaptcha, solve_recaptcha_async
from invisible_playwright.recaptcha._types import (
    RecaptchaAmbiguousError,
    RecaptchaChallenge,
    RecaptchaInjectionError,
    RecaptchaNotFoundError,
    RecaptchaSolution,
    RecaptchaSolver,
)

pytestmark = pytest.mark.e2e

KEY = "6LcLocalFixture_sitekey"
TOKEN = "local-fixture-response"
FIELDS = """
<textarea hidden name="g-recaptcha-response" id="g-recaptcha-response"></textarea>
<textarea hidden id="g-recaptcha-response-1"></textarea>
"""
SETUP = """
window.received = [];
window.submitted = false;
window.onSolved = token => window.received.push(token);
window.handlers = {done(token) { this.called = true; window.received.push(token); }};
"""
API = """
window.grecaptcha = {
    execute: () => Promise.resolve('fixture-only'),
    render: () => 0,
    enterprise: {execute: () => Promise.resolve('fixture-only'), render: () => 0}
};
"""


#: Blocks every load that is not loopback before a request is made.
CSP = "default-src 'self' 'unsafe-inline'"


def _html(body: str, *, fields: bool = True) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f"<script>{API}</script></head><body>"
            f"<script>{SETUP}</script><form onsubmit='window.submitted=true;return false'>"
            f"{FIELDS if fields else ''}{body}<button type='submit'>Submit</button>"
            "</form></body></html>").encode()


def _widget(*, invisible=False, callback="onSolved", extra=""):
    return (f'<div class="g-recaptcha" data-sitekey="{KEY}" data-callback="{callback}" '
            f'data-size="{"invisible" if invisible else "normal"}" {extra}></div>')


def _cfg(*, v3=False, string_callback=False, throwing=False):
    callback = ("'handlers.done'" if string_callback else
                "(token => { throw new Error(token); })" if throwing else
                "(token => window.received.push(token))")
    name = "promise-callback" if v3 else "callback"
    client_id = "10000" if v3 else "0"
    return f"""<script>
window.___grecaptcha_cfg = {{clients: {{
    '{client_id}': {{a: {{b: {{
        sitekey: '{KEY}', size: 'invisible', '{name}': {callback}
    }}}}}}
}}}};
const client = window.___grecaptcha_cfg.clients['{client_id}'];
client.cycle = client;
Object.defineProperty(client, 'mustNotRead', {{enumerable: true, get() {{
    throw new Error('The collector invoked a getter');
}}}});
</script>"""


PAGES = {
    "/checkbox": _html(_widget()),
    "/invisible": _html(_widget(invisible=True, callback="handlers.done")),
    "/v3": _html(
        f'<script src="https://www.google.com/recaptcha/api.js?render={KEY}"></script>'
        f"<script>grecaptcha.execute('{KEY}', {{action:'login'}});</script>"
        + _cfg(v3=True)),
    "/enterprise": _html(
        f'<script src="https://www.google.com/recaptcha/enterprise.js?render={KEY}"></script>'
        f"<script>grecaptcha.enterprise.execute('{KEY}', {{action:'login'}});</script>"
        + _widget(extra='data-s="enterprise-payload"')),
    "/enterprise-net": _html(
        f'<script src="https://www.recaptcha.net/recaptcha/enterprise.js?render={KEY}"></script>'
        f"<script>grecaptcha.enterprise.execute('{KEY}', {{action:'login'}});</script>"
        + _widget(extra='data-s="enterprise-payload"')),
    "/enterprise-v2": _html(
        '<script src="https://www.recaptcha.net/recaptcha/enterprise.js?render=explicit"></script>'
        + _widget(extra='data-s="enterprise-payload"')),
    "/cfg-function": _html(_cfg()),
    "/cfg-string": _html(_cfg(string_callback=True)),
    "/cfg-only": _html(_cfg(), fields=False),
    "/cfg-fallback": _html(_widget(callback="notDefined") + _cfg()),
    "/cfg-precedence": _html(_widget() + _cfg()),
    "/cfg-throws": _html(_cfg(throwing=True)),
    "/empty": _html("", fields=False),
    "/ambiguous": _html(
        _widget() + '<div data-sitekey="OTHER" data-callback="handlers.done"></div>'),
    "/nested": _html('<iframe src="/checkbox"></iframe>', fields=False),
    "/missing-target": _html(f'<div data-sitekey="{KEY}"></div>', fields=False),
}


@pytest.fixture(scope="module")
def recaptcha_server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(urlsplit(self.path).path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _watch_requests(page, base: str) -> list[str]:
    """Every URL the page requests that is not the fixture server's."""
    outside: list[str] = []
    netloc = urlsplit(base).netloc
    page.on("request", lambda request: outside.append(request.url)
            if urlsplit(request.url).netloc != netloc else None)
    return outside


@pytest.fixture
def recaptcha_page(firefox_binary, recaptcha_server):
    with InvisiblePlaywright(seed=4243, binary_path=firefox_binary,
                             headless=True) as browser:
        page = browser.new_context().new_page()
        outside = _watch_requests(page, recaptcha_server)
        yield page
        assert outside == [], "a fixture page reached beyond loopback"


class FakeSolver:
    name = "fixture"

    def __init__(self):
        self.calls: list[tuple[RecaptchaChallenge, float]] = []
        self.thread_id: int | None = None

    def solve(self, challenge: RecaptchaChallenge, *, timeout: float) -> RecaptchaSolution:
        self.thread_id = threading.get_ident()
        self.calls.append((challenge, timeout))
        return RecaptchaSolution(
            TOKEN, self.name, task_id="fixture-task",
            user_agent=challenge.user_agent,
            cookies={"recaptcha-ca-t": "fixture-cookie"},
        )


@pytest.mark.parametrize("path,kind,enterprise,action,data_s,domain,callback", [
    ("/checkbox", "v2", False, None, None, None, "onSolved"),
    ("/invisible", "v2-invisible", False, None, None, None, "handlers.done"),
    ("/v3", "v3", False, "login", None, None,
     '___grecaptcha_cfg["clients"]["10000"]["a"]["b"]["promise-callback"]'),
    ("/enterprise", "v3", True, "login", "enterprise-payload", None, "onSolved"),
    ("/enterprise-net", "v3", True, "login", "enterprise-payload",
     "www.recaptcha.net", "onSolved"),
    ("/enterprise-v2", "v2", True, None, "enterprise-payload",
     "www.recaptcha.net", "onSolved"),
])
def test_detect_and_solve_variants(
    recaptcha_page, recaptcha_server, path, kind, enterprise, action, data_s, domain, callback,
):
    page = recaptcha_page
    url = recaptcha_server + path
    page.goto(url, wait_until="load")
    challenges = detect_recaptcha(page)
    assert challenges == [RecaptchaChallenge(
        url, KEY, kind, enterprise=enterprise, action=action, data_s=data_s,
        api_domain=domain, callback=callback,
        user_agent=page.evaluate("navigator.userAgent"),
    )]
    solver = FakeSolver()
    assert isinstance(solver, RecaptchaSolver)
    result = solve_recaptcha(page, solver=solver, timeout=17)
    assert result.challenge == challenges[0]
    assert result.solution.provider == solver.name
    assert solver.calls == [(challenges[0], 17)]
    assert result.fields_filled == 2
    assert result.callback_called == callback
    assert page.locator("textarea").evaluate_all(
        "fields => fields.map(el => [el.value, el.textContent])") == [[TOKEN, TOKEN]] * 2
    assert page.evaluate("window.received") == [TOKEN]
    assert page.evaluate("window.submitted") is False
    assert page.context.cookies() == []
    if callback == "handlers.done":
        assert page.evaluate("window.handlers.called") is True


@pytest.mark.parametrize("path,callback,filled", [
    ("/cfg-function", '___grecaptcha_cfg["clients"]["0"]["a"]["b"]["callback"]', 2),
    ("/cfg-string", "handlers.done", 2),
    ("/cfg-only", '___grecaptcha_cfg["clients"]["0"]["a"]["b"]["callback"]', 0),
    ("/cfg-fallback", '___grecaptcha_cfg["clients"]["0"]["a"]["b"]["callback"]', 2),
    ("/cfg-precedence", "onSolved", 2),
])
def test_cfg_callback_resolution_and_precedence(recaptcha_page, recaptcha_server,
                                                path, callback, filled):
    page = recaptcha_page
    page.goto(recaptcha_server + path, wait_until="load")
    challenge, = detect_recaptcha(page)
    assert challenge.kind == "v2-invisible"
    assert challenge.sitekey == KEY
    result = solve_recaptcha(page, solver=FakeSolver())
    assert result.callback_called == callback
    assert result.fields_filled == filled
    assert page.evaluate("window.received") == [TOKEN]


def test_action_override_and_callback_opt_out(recaptcha_page, recaptcha_server):
    page = recaptcha_page
    page.goto(recaptcha_server + "/v3", wait_until="load")
    solver = FakeSolver()
    result = solve_recaptcha(page, solver=solver, action="checkout", invoke_callback=False)
    assert result.challenge.action == solver.calls[0][0].action == "checkout"
    assert result.fields_filled == 2
    assert result.callback_called is None
    assert page.evaluate("window.received") == []
    assert page.locator("textarea").first.input_value() == TOKEN


def test_child_widget_stays_in_its_own_document(recaptcha_page, recaptcha_server):
    page = recaptcha_page
    page.goto(recaptcha_server + "/nested", wait_until="load")
    challenge, = detect_recaptcha(page)
    assert challenge.website_url == recaptcha_server + "/checkbox"
    result = solve_recaptcha(page, solver=FakeSolver())
    assert result.fields_filled == 2
    assert result.callback_called == "onSolved"
    child = page.frames[1]
    assert child.locator("textarea").first.input_value() == TOKEN
    assert child.evaluate("window.received") == [TOKEN]
    assert page.evaluate("window.received") == []
    assert page.locator("textarea").count() == 0


def test_missing_and_ambiguous_do_not_charge_solver(recaptcha_page, recaptcha_server):
    page, solver = recaptcha_page, FakeSolver()
    page.goto(recaptcha_server + "/empty", wait_until="load")
    assert detect_recaptcha(page) == []
    with pytest.raises(RecaptchaNotFoundError):
        solve_recaptcha(page, solver=solver)
    page.goto(recaptcha_server + "/ambiguous", wait_until="load")
    assert len(detect_recaptcha(page)) == 2
    with pytest.raises(RecaptchaAmbiguousError):
        solve_recaptcha(page, solver=solver)
    with pytest.raises(RecaptchaNotFoundError):
        solve_recaptcha(page, solver=solver, sitekey="not-present")
    assert solver.calls == []
    result = solve_recaptcha(page, solver=solver, sitekey="OTHER")
    assert result.challenge.sitekey == "OTHER"
    assert result.callback_called == "handlers.done"
    assert page.evaluate("window.received") == [TOKEN]


@pytest.mark.parametrize("path,invoke", [
    ("/missing-target", True),
    ("/cfg-only", False),
    ("/cfg-throws", True),
])
def test_injection_failures_are_explicit_and_token_safe(
    recaptcha_page, recaptcha_server, path, invoke,
):
    page = recaptcha_page
    page.goto(recaptcha_server + path, wait_until="load")
    with pytest.raises(RecaptchaInjectionError) as caught:
        solve_recaptcha(page, solver=FakeSolver(), invoke_callback=invoke)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_navigation_during_solving_does_not_disclose_token_to_new_page(
    recaptcha_page, recaptcha_server,
):
    page = recaptcha_page
    page.goto(recaptcha_server + "/checkbox", wait_until="load")

    class NavigatingSolver(FakeSolver):
        def solve(self, challenge, *, timeout):
            page.goto(recaptcha_server + "/checkbox?different-document", wait_until="load")
            return super().solve(challenge, timeout=timeout)

    with pytest.raises(RecaptchaInjectionError):
        solve_recaptcha(page, solver=NavigatingSolver())
    assert page.locator("textarea").first.input_value() == ""
    assert page.evaluate("window.received") == []


def test_async_detect_solve_and_error_parity(firefox_binary, recaptcha_server):
    async def run():
        async with AsyncIP(seed=4243, binary_path=firefox_binary,
                           headless=True) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            outside = _watch_requests(page, recaptcha_server)
            await page.goto(recaptcha_server + "/v3", wait_until="load")
            challenge, = await detect_recaptcha_async(page)
            assert challenge.kind == "v3"
            assert challenge.action == "login"
            solver = FakeSolver()
            result = await solve_recaptcha_async(page, solver=solver, action="async-login",
                                                 timeout=23)
            assert result.challenge.action == "async-login"
            assert solver.calls == [(result.challenge, 23)]
            assert solver.thread_id != threading.get_ident()
            assert result.fields_filled == 2
            assert result.callback_called == challenge.callback
            assert await page.locator("textarea").first.input_value() == TOKEN
            assert await page.evaluate("window.received") == [TOKEN]
            assert await page.evaluate("window.submitted") is False
            assert await context.cookies() == []
            await page.goto(recaptcha_server + "/empty", wait_until="load")
            with pytest.raises(RecaptchaNotFoundError):
                await solve_recaptcha_async(page, solver=solver)
            await page.goto(recaptcha_server + "/ambiguous", wait_until="load")
            with pytest.raises(RecaptchaAmbiguousError):
                await solve_recaptcha_async(page, solver=solver)
            await page.goto(recaptcha_server + "/cfg-throws", wait_until="load")
            with pytest.raises(RecaptchaInjectionError) as caught:
                await solve_recaptcha_async(page, solver=solver)
            assert TOKEN not in str(caught.value)
            assert outside == []

    asyncio.run(run())
