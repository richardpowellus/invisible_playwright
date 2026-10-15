"""Provider behaviour uses fake time; only the marked wire test opens a socket."""
from __future__ import annotations

import io
import json
import logging
import threading
import traceback
import urllib.error
from dataclasses import replace
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from invisible_playwright.recaptcha import _capsolver
from invisible_playwright.recaptcha._capsolver import CapSolver
from invisible_playwright.recaptcha._types import (
    RecaptchaChallenge,
    RecaptchaSolver,
    SolverAuthError,
    SolverBalanceError,
    SolverError,
    SolverRequestError,
    SolverTimeoutError,
    SolverUnavailableError,
    SolverUnsolvableError,
)

pytestmark = pytest.mark.unit

KEY = "test-api-key-not-for-logs"
TOKEN = "test-recaptcha-token-not-for-logs"
CHALLENGE = RecaptchaChallenge("https://example.test/form", "site-key", "v2")
CREATED = {"errorId": 0, "taskId": "task-123"}
READY = {
    "errorId": 0, "status": "ready",
    "solution": {
        "gRecaptchaResponse": TOKEN, "userAgent": "service-agent",
        "secChUa": '"Chromium";v="130"', "createTime": 1700000000000,
        "recaptcha-ca-t": "session-cookie", "recaptcha-ca-e": "enterprise-cookie",
        "unrelated-cookie": "not-returned",
    },
}


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


class Transport:
    def __init__(self, *responses, repeat=None):
        self.responses = iter(responses)
        self.repeat = repeat
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload, timeout))
        response = next(self.responses, self.repeat)
        assert response is not None, "unexpected extra provider request"
        if isinstance(response, Exception):
            raise response
        return response


def solver_for(*responses, repeat=None, **kwargs):
    clock = FakeTime()
    transport = Transport(*responses, repeat=repeat)
    solver = CapSolver(KEY, transport=transport, clock=clock.clock,
                       sleep=clock.sleep, **kwargs)
    return solver, transport, clock


def error_response(code, description="provider refused"):
    return {"errorId": 1, "errorCode": code, "errorDescription": description}


@pytest.mark.parametrize("status", ["processing", "idle"])
def test_create_poll_ready(status):
    solver, transport, clock = solver_for(
        CREATED, {"errorId": 0, "status": status}, READY,
        app_id="our-app", base_url="https://solver.example.test/",
    )
    assert isinstance(solver, RecaptchaSolver)
    result = solver.solve(CHALLENGE, timeout=120)
    assert result.token == TOKEN
    assert result.provider == "capsolver"
    assert result.task_id == "task-123"
    assert result.user_agent == "service-agent"
    assert result.sec_ch_ua == '"Chromium";v="130"'
    assert result.created_at_ms == 1700000000000
    assert result.cookies == {
        "recaptcha-ca-t": "session-cookie", "recaptcha-ca-e": "enterprise-cookie",
    }
    assert result.elapsed_s == 6
    assert clock.sleeps == [3, 3]
    assert transport.calls == [
        ("https://solver.example.test/createTask",
         {"clientKey": KEY, "task": solver.task_for(CHALLENGE), "appId": "our-app"}, 30),
        ("https://solver.example.test/getTaskResult",
         {"clientKey": KEY, "taskId": "task-123"}, 30),
        ("https://solver.example.test/getTaskResult",
         {"clientKey": KEY, "taskId": "task-123"}, 30),
    ]


@pytest.mark.parametrize("task_id", [None, "instant-task"])
def test_create_can_return_a_ready_solution(task_id):
    response = {**READY}
    if task_id is not None:
        response["taskId"] = task_id
    solver, transport, clock = solver_for(response)
    result = solver.solve(CHALLENGE, timeout=20)
    assert result.token == TOKEN
    assert result.task_id == task_id
    assert result.elapsed_s == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][2] == 20
    assert "appId" not in transport.calls[0][1]
    assert clock.sleeps == []


def test_optional_solution_metadata_can_be_absent():
    solver, _, _ = solver_for({
        "status": "ready", "solution": {"gRecaptchaResponse": TOKEN},
    })
    result = solver.solve(CHALLENGE, timeout=20)
    assert result.user_agent is None
    assert result.sec_ch_ua is None
    assert result.created_at_ms is None
    assert result.cookies == {}


@pytest.mark.parametrize("solution", [
    None, {}, {"gRecaptchaResponse": ""}, {"gRecaptchaResponse": "  "},
    {"gRecaptchaResponse": None}, {"gRecaptchaResponse": 123},
])
@pytest.mark.parametrize("direct", [False, True])
def test_ready_without_a_token_is_an_error(solution, direct):
    ready = {"status": "ready", "solution": solution}
    responses = [ready] if direct else [CREATED, ready]
    solver, _, _ = solver_for(*responses)
    with pytest.raises(SolverError, match="no (solution|token)"):
        solver.solve(CHALLENGE, timeout=20)


ERROR_CASES = [
    ("ERROR_KEY_DENIED_ACCESS", SolverAuthError),
    ("ERROR_KEY_TEMP_BLOCKED", SolverAuthError),
    ("ERROR_IP_BANNED", SolverAuthError),
    ("ERROR_ZERO_BALANCE", SolverBalanceError),
    ("ERROR_SETTLEMENT_FAILED", SolverBalanceError),
    ("ERROR_CAPTCHA_UNSOLVABLE", SolverUnsolvableError),
    ("ERROR_TASK_TIMEOUT", SolverUnsolvableError),
    ("ERROR_UNKNOWN_QUESTION", SolverUnsolvableError),
    ("ERROR_INVALID_TASK_DATA", SolverRequestError),
    ("ERROR_BAD_REQUEST", SolverRequestError),
    ("ERROR_TASK_NOT_SUPPORTED", SolverRequestError),
    ("ERROR_TASKID_INVALID", SolverRequestError),
    ("ERROR_INVALID_IMAGE", SolverRequestError),
    ("ERROR_PARSE_IMAGE_FAIL", SolverRequestError),
    ("ERROR_PROXY_BANNED", SolverRequestError),
    ("ERROR_SERVICE_UNAVALIABLE", SolverUnavailableError),
    ("ERROR_RATE_LIMIT", SolverUnavailableError),
    ("ERROR_NEW_UNKNOWN_CODE", SolverError),
]


@pytest.mark.parametrize("code,expected", ERROR_CASES)
@pytest.mark.parametrize("operation", ["solve", "balance"])
def test_error_codes(code, expected, operation):
    solver, transport, _ = solver_for(repeat=error_response(code))
    with pytest.raises(expected) as caught:
        if operation == "solve":
            solver.solve(CHALLENGE, timeout=30)
        else:
            solver.balance()
    assert type(caught.value) is expected
    assert caught.value.provider == "capsolver"
    assert caught.value.code == code
    assert str(caught.value) == f"{code}: provider refused"
    attempts = 3 if operation == "solve" and expected is SolverUnavailableError else 1
    assert len(transport.calls) == attempts


@pytest.mark.parametrize("code,expected", [
    case for case in ERROR_CASES if case[1] is not SolverUnavailableError
])
def test_poll_errors_are_not_retried(code, expected):
    solver, transport, _ = solver_for(CREATED, error_response(code))
    with pytest.raises(expected) as caught:
        solver.solve(CHALLENGE, timeout=30)
    assert caught.value.code == code
    assert caught.value.provider == "capsolver"
    assert len(transport.calls) == 2


@pytest.mark.parametrize("error", [
    error_response("ERROR_SERVICE_UNAVALIABLE"),
    error_response("ERROR_RATE_LIMIT"),
    urllib.error.URLError("network unavailable"),
    TimeoutError("socket timed out"),
    ConnectionResetError("connection lost"),
    RemoteDisconnected("peer disconnected"),
    json.JSONDecodeError("invalid JSON", "not JSON", 0),
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    SolverUnavailableError("temporary transport error"),
    [],
])
@pytest.mark.parametrize("during", ["create", "poll"])
def test_transient_retry_then_success(error, during):
    responses = [error, CREATED, READY] if during == "create" else [CREATED, error, READY]
    solver, transport, clock = solver_for(*responses)
    assert solver.solve(CHALLENGE, timeout=30).token == TOKEN
    assert clock.sleeps == ([3, 3] if during == "create" else [3, 6])
    assert len(transport.calls) == 3


def test_create_retries_stop_after_three_attempts():
    solver, transport, clock = solver_for(repeat=error_response("ERROR_RATE_LIMIT"))
    with pytest.raises(SolverUnavailableError):
        solver.solve(CHALLENGE, timeout=300)
    assert len(transport.calls) == 3
    assert clock.sleeps == [3, 6]


def test_transient_polling_forever_reaches_deadline():
    solver, transport, clock = solver_for(
        CREATED, repeat=error_response("ERROR_SERVICE_UNAVALIABLE"),
    )
    with pytest.raises(SolverTimeoutError, match=r"task-123.*30\.0s"):
        solver.solve(CHALLENGE, timeout=30)
    assert clock.sleeps == [3, 6, 10, 10, 1]
    assert len(transport.calls) == 5
    assert clock.now == 130


def test_processing_resets_the_retry_backoff():
    solver, _, clock = solver_for(
        CREATED, error_response("ERROR_RATE_LIMIT"), {"status": "processing"}, READY,
    )
    assert solver.solve(CHALLENGE, timeout=30).token == TOKEN
    assert clock.sleeps == [3, 6, 3]


def test_deadline_bounds_every_request_and_sleep():
    solver, transport, clock = solver_for(CREATED, repeat={"status": "processing"})
    with pytest.raises(SolverTimeoutError, match=r"task-123.*8\.0s"):
        solver.solve(CHALLENGE, timeout=8)
    assert [call[2] for call in transport.calls] == [8, 5, 2]
    assert clock.sleeps == [3, 3, 2]
    assert clock.now == 108


def test_creation_retry_also_obeys_the_deadline():
    solver, transport, clock = solver_for(repeat=error_response("ERROR_RATE_LIMIT"))
    with pytest.raises(SolverTimeoutError, match=r"not created.*2\.0s"):
        solver.solve(CHALLENGE, timeout=2)
    assert len(transport.calls) == 1
    assert clock.sleeps == [2]


@pytest.mark.parametrize("response", [READY, error_response("ERROR_RATE_LIMIT")])
@pytest.mark.parametrize("during", ["create", "poll"])
def test_time_spent_in_requests_counts_toward_the_deadline(response, during):
    clock = FakeTime()
    calls = []

    def transport(url, payload, timeout):
        calls.append(timeout)
        if during == "poll" and len(calls) == 1:
            clock.now += 2
            return CREATED
        clock.now += timeout
        return response

    solver = CapSolver(KEY, transport=transport, sleep=clock.sleep, clock=clock.clock)
    if response is READY:
        # The token arrived exactly on the deadline. It has been paid for, so
        # it is returned rather than thrown away as a timeout.
        assert solver.solve(CHALLENGE, timeout=10).token == TOKEN
    else:
        with pytest.raises(SolverTimeoutError, match="10.0s"):
            solver.solve(CHALLENGE, timeout=10)
    assert calls == ([10] if during == "create" else [10, 5])
    assert clock.now == 110


@pytest.mark.parametrize("response", [
    {"status": "idle"}, error_response("ERROR_RATE_LIMIT"),
])
def test_no_more_than_120_result_queries(response):
    # A non-advancing clock isolates the query budget from the time budget.
    transport = Transport(CREATED, repeat=response)
    sleeps = []
    solver = CapSolver(KEY, transport=transport, sleep=sleeps.append, clock=lambda: 0)
    with pytest.raises(SolverTimeoutError, match=r"task-123.*120.*0\.0s"):
        solver.solve(CHALLENGE, timeout=300)
    assert len(transport.calls) == 121
    assert len(sleeps) == 120
    assert all(call[0].endswith("/getTaskResult") for call in transport.calls[1:])


def test_the_120th_query_can_still_succeed():
    solver, transport, clock = solver_for(
        CREATED, *[{"status": "processing"}] * 119, READY, poll_interval=1,
    )
    assert solver.solve(CHALLENGE, timeout=300).token == TOKEN
    assert len(transport.calls) == 121
    assert clock.now == 220


@pytest.mark.parametrize("interval,expected", [
    (-2, 1), (0, 1), (0.5, 1), (1, 1), (3, 3), (10, 10), (100, 10),
    (float("inf"), 10), (-float("inf"), 1),
])
def test_poll_interval_is_clamped(interval, expected):
    solver, _, clock = solver_for(CREATED, READY, poll_interval=interval)
    assert solver.solve(CHALLENGE, timeout=30).elapsed_s == expected
    assert clock.sleeps == [expected]


def test_nan_poll_interval_is_rejected():
    with pytest.raises(ValueError, match="poll_interval"):
        CapSolver(KEY, poll_interval=float("nan"))


@pytest.mark.parametrize("timeout", [0, -1, 301, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_before_network(timeout):
    solver, transport, clock = solver_for()
    with pytest.raises(ValueError, match="timeout"):
        solver.solve(CHALLENGE, timeout=timeout)
    assert transport.calls == []
    assert clock.sleeps == []


@pytest.mark.parametrize("timeout", [0.1, 300])
def test_timeout_boundaries_are_accepted(timeout):
    solver, transport, _ = solver_for(READY)
    assert solver.solve(CHALLENGE, timeout=timeout).token == TOKEN
    assert transport.calls[0][2] == pytest.approx(min(30, timeout))


@pytest.mark.parametrize("kind,enterprise,task_type", [
    ("v2", False, "ReCaptchaV2TaskProxyLess"),
    ("v2-invisible", False, "ReCaptchaV2TaskProxyLess"),
    ("v2", True, "ReCaptchaV2EnterpriseTaskProxyLess"),
    ("v2-invisible", True, "ReCaptchaV2EnterpriseTaskProxyLess"),
    ("v3", False, "ReCaptchaV3TaskProxyLess"),
    ("v3", True, "ReCaptchaV3EnterpriseTaskProxyLess"),
])
def test_task_types_and_optional_fields(kind, enterprise, task_type):
    solver = CapSolver(KEY)
    challenge = replace(CHALLENGE, kind=kind, enterprise=enterprise,
                        action="checkout", data_s="anchor-s",
                        api_domain="www.recaptcha.net", user_agent="browser-agent")
    expected = {
        "type": task_type, "websiteURL": CHALLENGE.website_url,
        "websiteKey": CHALLENGE.sitekey, "pageAction": "checkout",
        "apiDomain": "www.recaptcha.net",
    }
    if kind == "v2-invisible":
        expected["isInvisible"] = True
    if enterprise:
        expected["enterprisePayload"] = {"s": "anchor-s"}
    elif kind != "v3":
        expected["recaptchaDataSValue"] = "anchor-s"
    assert solver.task_for(challenge) == expected
    assert "userAgent" not in solver.task_for(challenge)


@pytest.mark.parametrize("domain", [None, "", "google.com", "www.google.com"])
def test_unset_fields_and_google_domain_are_omitted(domain):
    solver = CapSolver(KEY)
    assert solver.task_for(replace(CHALLENGE, api_domain=domain)) == {
        "type": "ReCaptchaV2TaskProxyLess",
        "websiteURL": CHALLENGE.website_url, "websiteKey": CHALLENGE.sitekey,
    }


@pytest.mark.parametrize("cookies", [None, {}, {"one": "first", "two": "second"}])
def test_cookies_require_explicit_opt_in(cookies):
    solver = CapSolver(KEY, send_cookies=cookies)
    task = solver.task_for(CHALLENGE)
    if cookies is None:
        assert "cookies" not in task
    else:
        assert task["cookies"] == [
            {"name": name, "value": value} for name, value in cookies.items()
        ]


def test_task_for_does_not_share_mutable_cookie_state():
    cookies = {"one": "first"}
    solver = CapSolver(KEY, send_cookies=cookies)
    cookies["one"] = "changed"
    task = solver.task_for(CHALLENGE)
    task["cookies"][0]["value"] = "also changed"
    assert solver.task_for(CHALLENGE)["cookies"] == [{"name": "one", "value": "first"}]


def test_key_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", KEY)
    transport = Transport({"errorId": 0, "balance": "12.50"})
    assert CapSolver(transport=transport).balance() == 12.5
    assert transport.calls == [
        ("https://api.capsolver.com/getBalance", {"clientKey": KEY}, 30),
    ]


def test_explicit_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", "environment-key")
    transport = Transport({"balance": 1})
    assert CapSolver(KEY, transport=transport).balance() == 1.0
    assert transport.calls[0][1]["clientKey"] == KEY


@pytest.mark.parametrize("key", [None, "", " \t\n"])
@pytest.mark.parametrize("environment", [None, "", " \t\n"])
def test_missing_or_blank_key_names_the_environment_variable(monkeypatch, key, environment):
    if environment is None:
        monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("CAPSOLVER_API_KEY", environment)
    with pytest.raises(SolverAuthError, match="CAPSOLVER_API_KEY") as caught:
        CapSolver(key)
    assert caught.value.provider == "capsolver"


def test_explicit_blank_key_does_not_fall_back_to_environment(monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", KEY)
    with pytest.raises(SolverAuthError):
        CapSolver("")


@pytest.mark.parametrize("response", [
    error_response("ERROR_KEY_DENIED_ACCESS", f"rejected {KEY}"),
    error_response(f"ERROR_{KEY}", f"rejected {KEY}"),
    urllib.error.URLError(f"connection for {KEY} failed"),
    SolverUnavailableError(f"rejected {KEY}", code=f"ERROR_{KEY}"),
    json.JSONDecodeError(f"invalid {KEY}", KEY, 0),
])
def test_key_is_redacted_from_errors_and_debug_logs(response, caplog):
    caplog.set_level(logging.DEBUG)
    solver, _, _ = solver_for(repeat=response)
    with pytest.raises(SolverError) as caught:
        solver.solve(CHALLENGE, timeout=30)
    assert KEY not in repr(solver)
    assert KEY not in str(solver)
    assert KEY not in repr(caught.value)
    assert KEY not in str(caught.value)
    assert KEY not in (caught.value.code or "")
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert KEY not in caplog.text


def test_debug_logging_never_contains_tokens_or_cookies(caplog):
    caplog.set_level(logging.DEBUG)
    solver, _, _ = solver_for(CREATED, {"status": "processing"}, READY)
    result = solver.solve(CHALLENGE, timeout=30)
    assert repr(solver) == (
        "CapSolver(provider='capsolver', base_url='https://api.capsolver.com')"
    )
    assert result.token == TOKEN
    assert "task-123" in caplog.text
    assert "processing" in caplog.text
    assert "ready" in caplog.text
    assert "elapsed=6.0s" in caplog.text
    for secret in (KEY, TOKEN, "session-cookie", "enterprise-cookie"):
        assert secret not in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


def test_an_echoed_key_cannot_leak_through_task_id_or_base_url(caplog):
    caplog.set_level(logging.DEBUG)
    solver, _, _ = solver_for(
        {"taskId": KEY}, READY, base_url=f"https://example.test/{KEY}",
    )
    result = solver.solve(CHALLENGE, timeout=30)
    assert KEY not in repr(solver)
    assert KEY not in repr(result)
    assert KEY not in caplog.text


@pytest.mark.parametrize("response", [
    {}, {"taskId": ""}, {"taskId": 123}, {"taskId": []},
])
def test_malformed_creation_result_is_rejected(response):
    solver, _, _ = solver_for(response)
    with pytest.raises(SolverError, match="taskId"):
        solver.solve(CHALLENGE, timeout=30)


@pytest.mark.parametrize("status", [None, "", "unrecognised", 42, {"value": "ready"}])
def test_unknown_status_is_not_polled_forever(status):
    solver, transport, _ = solver_for(CREATED, {"status": status})
    with pytest.raises(SolverError, match="status"):
        solver.solve(CHALLENGE, timeout=30)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("field,value", [
    ("userAgent", []), ("secChUa", 42), ("recaptcha-ca-t", None),
    ("recaptcha-ca-e", {}), ("createTime", "yesterday"), ("createTime", True),
])
def test_malformed_solution_metadata_is_rejected(field, value):
    response = {**READY, "solution": {**READY["solution"], field: value}}
    solver, _, _ = solver_for(response)
    with pytest.raises(SolverError, match=field):
        solver.solve(CHALLENGE, timeout=30)


@pytest.mark.parametrize("value,expected", [("12.50", 12.5), (0, 0.0), (1.75, 1.75)])
def test_balance_is_a_float(value, expected):
    solver, transport, clock = solver_for({"errorId": 0, "balance": value})
    result = solver.balance()
    assert isinstance(result, float)
    assert result == expected
    assert transport.calls[0][0].endswith("/getBalance")
    assert clock.sleeps == []


@pytest.mark.parametrize("response", [
    {}, {"balance": None}, {"balance": "invalid"}, {"balance": "nan"},
    {"balance": "inf"}, {"balance": []}, {"balance": KEY},
])
def test_invalid_balance_is_not_success(response):
    solver, _, _ = solver_for(response)
    with pytest.raises(SolverError, match="balance") as caught:
        solver.balance()
    assert KEY not in repr(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("status,body,expected,code", [
    (403, error_response("ERROR_KEY_DENIED_ACCESS", f"rejected {KEY}"),
     SolverAuthError, "ERROR_KEY_DENIED_ACCESS"),
    (400, {"errorCode": "ERROR_BAD_REQUEST", "errorDescription": "bad task"},
     SolverRequestError, "ERROR_BAD_REQUEST"),
    (400, {}, SolverRequestError, "HTTP_400"),
    (429, error_response("ERROR_RATE_LIMIT"), SolverUnavailableError, "ERROR_RATE_LIMIT"),
    (500, b"not JSON", SolverUnavailableError, "HTTP_500"),
    (503, error_response("ERROR_KEY_DENIED_ACCESS"), SolverUnavailableError, "HTTP_503"),
    (200, b"not JSON", SolverUnavailableError, None),
    (200, b"\xff", SolverUnavailableError, None),
    (200, [], SolverUnavailableError, None),
    (403, b"not JSON", SolverUnavailableError, None),
])
def test_default_transport_decodes_http_errors_without_echoing_bodies(
    monkeypatch, status, body, expected, code, caplog,
):
    caplog.set_level(logging.DEBUG)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    stream = io.BytesIO(raw)
    calls = []

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, KEY, {}, stream)
        return stream

    monkeypatch.setattr(_capsolver.urllib.request, "urlopen", urlopen)
    with pytest.raises(expected) as caught:
        CapSolver(KEY).balance()
    assert caught.value.code == code
    assert caught.value.provider == "capsolver"
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert KEY not in caplog.text
    assert stream.closed
    assert calls[0][1] == 30
    assert calls[0][0].get_method() == "POST"
    assert calls[0][0].get_header("Content-type") == "application/json"
    assert json.loads(calls[0][0].data) == {"clientKey": KEY}


@pytest.mark.e2e
def test_real_urllib_transport_over_loopback(caplog, monkeypatch):
    """Even loopback is network: the repository reserves it for the e2e lane."""
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    caplog.set_level(logging.DEBUG)
    requests = []
    replies = iter([
        (500, {"errorId": 1, "errorDescription": f"temporary {KEY}"}),
        (200, CREATED),
        (200, {"errorId": 0, "status": "processing"}),
        (200, READY),
        (200, {"errorId": 0, "balance": "7.25"}),
        (403, error_response("ERROR_KEY_DENIED_ACCESS", f"rejected {KEY}")),
    ])

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers["Content-Type"], json.loads(body)))
            status, reply = next(replies)
            encoded = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    clock = FakeTime()
    solver = CapSolver(
        KEY, base_url=f"http://127.0.0.1:{server.server_port}",
        sleep=clock.sleep, clock=clock.clock,
    )
    thread.start()
    try:
        assert thread.is_alive()
        result = solver.solve(CHALLENGE, timeout=30)
        assert result.token == TOKEN
        assert result.task_id == "task-123"
        assert clock.sleeps == [3, 3, 3]
        assert solver.balance() == 7.25
        with pytest.raises(SolverAuthError) as caught:
            solver.balance()
        assert KEY not in "".join(traceback.format_exception(caught.value))
        assert [path for path, _, _ in requests] == [
            "/createTask", "/createTask", "/getTaskResult", "/getTaskResult",
            "/getBalance", "/getBalance",
        ]
        assert all(content_type == "application/json" for _, content_type, _ in requests)
        assert all(payload["clientKey"] == KEY for _, _, payload in requests)
        assert requests[0][2]["task"] == solver.task_for(CHALLENGE)
        assert requests[2][2] == {"clientKey": KEY, "taskId": "task-123"}
        assert KEY not in caplog.text
        assert TOKEN not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
