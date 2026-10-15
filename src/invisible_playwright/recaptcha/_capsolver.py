"""A blocking CapSolver client, independent of pages and browser state."""
from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from http.client import HTTPException
from typing import Any

from ._types import (
    RecaptchaChallenge,
    RecaptchaSolution,
    SolverAuthError,
    SolverBalanceError,
    SolverError,
    SolverRequestError,
    SolverTimeoutError,
    SolverUnavailableError,
    SolverUnsolvableError,
)

_LOG = logging.getLogger(__name__)
_ERRORS: dict[str, type[SolverError]] = {
    **dict.fromkeys((
        "ERROR_KEY_DENIED_ACCESS", "ERROR_KEY_TEMP_BLOCKED", "ERROR_IP_BANNED",
    ), SolverAuthError),
    **dict.fromkeys((
        "ERROR_ZERO_BALANCE", "ERROR_SETTLEMENT_FAILED",
    ), SolverBalanceError),
    **dict.fromkeys((
        "ERROR_CAPTCHA_UNSOLVABLE", "ERROR_TASK_TIMEOUT", "ERROR_UNKNOWN_QUESTION",
    ), SolverUnsolvableError),
    **dict.fromkeys((
        "ERROR_INVALID_TASK_DATA", "ERROR_BAD_REQUEST", "ERROR_TASK_NOT_SUPPORTED",
        "ERROR_TASKID_INVALID", "ERROR_INVALID_IMAGE", "ERROR_PARSE_IMAGE_FAIL",
        "ERROR_PROXY_BANNED",
    ), SolverRequestError),
    **dict.fromkeys((
        "ERROR_SERVICE_UNAVALIABLE", "ERROR_RATE_LIMIT",
    ), SolverUnavailableError),
}

_Transport = Callable[[str, dict[str, Any], float], dict[str, Any]]


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            if 400 <= error.code < 500:
                response = json.load(error)
                if isinstance(response, dict) and response.get("errorCode"):
                    # A rejected HTTP request can still carry the useful API error.
                    response["errorId"] = 1
                    return response
                raise SolverRequestError(
                    f"HTTP {error.code}: request rejected", provider="capsolver",
                    code=f"HTTP_{error.code}",
                ) from None
            raise SolverUnavailableError(
                f"HTTP {error.code}: service unavailable", provider="capsolver",
                code=f"HTTP_{error.code}",
            ) from None


class CapSolver:
    """Solve proxyless reCAPTCHA tasks without sending the browser's user agent.

    Cookies leave the browser only when the caller supplies ``send_cookies``.
    The deadline covers creation, retries, polling and the waits between them;
    a single HTTP read is bounded by a socket timeout taken from what is left
    of it, which stops a silent server but not one that keeps trickling bytes.

    A lost reply to ``createTask`` is retried (three attempts at most), and
    CapSolver may already have accepted - and will charge for - the task whose
    reply was lost. Only the task whose id came back is polled.
    """

    name = "capsolver"

    def __init__(
        self, api_key: str | None = None, *,
        base_url: str = "https://api.capsolver.com",
        poll_interval: float = 3.0,
        transport: _Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        app_id: str | None = None,
        send_cookies: Mapping[str, str] | None = None,
    ) -> None:
        key = os.environ.get("CAPSOLVER_API_KEY") if api_key is None else api_key
        if not key or not key.strip():
            raise SolverAuthError(
                "Set CAPSOLVER_API_KEY or supply a non-blank api_key.",
                provider=self.name,
            )
        if math.isnan(poll_interval):
            raise ValueError("poll_interval must not be NaN")
        self._api_key = key
        self.base_url = base_url.rstrip("/")
        # CapSolver: "if processing, wait 3 seconds". Faster polling only burns
        # the 120 queries a task allows and gives up on a slow one sooner.
        self.poll_interval = min(10.0, max(3.0, float(poll_interval)))
        self._transport = _post_json if transport is None else transport
        self._sleep = sleep
        self._clock = clock
        self._app_id = app_id
        self._cookies = None if send_cookies is None else dict(send_cookies)

    def _redact(self, value: str) -> str:
        return value.replace(self._api_key, "<redacted>")

    def __repr__(self) -> str:
        return (f"CapSolver(provider={self.name!r}, "
                f"base_url={self._redact(self.base_url)!r})")

    def task_for(self, challenge: RecaptchaChallenge) -> dict[str, Any]:
        enterprise = "Enterprise" if challenge.enterprise else ""
        task: dict[str, Any] = {
            "type": f"ReCaptcha{challenge.version.upper()}{enterprise}TaskProxyLess",
            "websiteURL": challenge.website_url,
            "websiteKey": challenge.sitekey,
        }
        if challenge.action:
            task["pageAction"] = challenge.action
        if challenge.is_invisible:
            task["isInvisible"] = True
        if challenge.data_s:
            if challenge.enterprise:
                task["enterprisePayload"] = {"s": challenge.data_s}
            elif challenge.version == "v2":
                task["recaptchaDataSValue"] = challenge.data_s
        if challenge.api_domain and challenge.api_domain not in (
            "www.google.com", "google.com",
        ):
            task["apiDomain"] = challenge.api_domain
        if self._cookies is not None:
            task["cookies"] = [
                {"name": name, "value": value}
                for name, value in self._cookies.items()
            ]
        return task

    def _request(self, endpoint: str, payload: dict[str, Any],
                 timeout: float) -> dict[str, Any]:
        failure: SolverError | None = None
        try:
            response = self._transport(
                self.base_url + endpoint, {"clientKey": self._api_key, **payload},
                min(30.0, timeout),
            )
        except SolverError as error:
            # Raise outside the handler so even __context__ cannot retain a key.
            failure = type(error)(
                self._redact(str(error)), provider=self.name,
                code=self._redact(error.code) if error.code else None,
            )
        except (urllib.error.URLError, OSError, HTTPException):
            failure = SolverUnavailableError(
                "CapSolver network request failed", provider=self.name,
            )
        except (json.JSONDecodeError, UnicodeError):
            failure = SolverUnavailableError(
                "CapSolver returned a non-JSON response", provider=self.name,
            )
        except ValueError:  # after JSONDecodeError, its subclass
            # A malformed base_url; urllib's message quotes the URL, which a
            # caller may have built around the key.
            failure = SolverRequestError(
                "CapSolver base_url is not a valid http(s) URL", provider=self.name,
            )
        if failure is not None:
            raise failure from None
        if not isinstance(response, dict):
            raise SolverUnavailableError(
                "CapSolver returned a non-object response", provider=self.name,
            )
        if response.get("errorId", 0) != 0:
            code = response.get("errorCode")
            code = code if isinstance(code, str) else None
            description = response.get("errorDescription")
            if not isinstance(description, str):
                description = "CapSolver returned an error"
            raise _ERRORS.get(code or "", SolverError)(
                self._redact(f"{code}: {description}" if code else description),
                provider=self.name, code=self._redact(code) if code else None,
            )
        return response

    def _remaining(self, started: float, deadline: float,
                   task_id: str | None) -> float:
        now = self._clock()
        if now >= deadline:
            raise SolverTimeoutError(
                f"CapSolver task {self._redact(task_id or '<not created>')} "
                f"timed out after {now - started:.1f}s", provider=self.name,
            )
        return deadline - now

    def _pause(self, delay: float, started: float, deadline: float,
               task_id: str | None) -> None:
        self._sleep(min(delay, self._remaining(started, deadline, task_id)))
        self._remaining(started, deadline, task_id)

    def _solution(self, response: dict[str, Any], task_id: str | None,
                  started: float) -> RecaptchaSolution:
        solution = response.get("solution")
        if not isinstance(solution, dict):
            raise SolverError("CapSolver ready result has no solution", provider=self.name)
        token = solution.get("gRecaptchaResponse")
        if not isinstance(token, str) or not token.strip():
            raise SolverError("CapSolver ready result has no token", provider=self.name)
        for field in ("userAgent", "secChUa", "recaptcha-ca-t", "recaptcha-ca-e"):
            if field in solution and not isinstance(solution[field], str):
                raise SolverError(
                    f"CapSolver solution has invalid {field}", provider=self.name,
                )
        created = solution.get("createTime")
        if created is not None and (not isinstance(created, int) or isinstance(created, bool)):
            raise SolverError(
                "CapSolver solution has invalid createTime", provider=self.name,
            )
        return RecaptchaSolution(
            token=token, provider=self.name,
            task_id=self._redact(task_id) if task_id is not None else None,
            user_agent=solution.get("userAgent"), sec_ch_ua=solution.get("secChUa"),
            cookies={key: solution[key] for key in ("recaptcha-ca-t", "recaptcha-ca-e")
                     if key in solution},
            created_at_ms=created, elapsed_s=self._clock() - started,
        )

    def solve(self, challenge: RecaptchaChallenge, *, timeout: float) -> RecaptchaSolution:
        if not 0 < timeout <= 300:
            raise ValueError("timeout must be greater than 0 and at most 300 seconds")
        started = self._clock()
        deadline = started + timeout
        payload: dict[str, Any] = {"task": self.task_for(challenge)}
        if self._app_id is not None:
            payload["appId"] = self._app_id

        # Creation retries may create another paid task when the reply was lost.
        # Bound them separately from the poll budget for that reason.
        for attempt in range(3):
            remaining = self._remaining(started, deadline, None)
            try:
                response = self._request("/createTask", payload, remaining)
            except SolverUnavailableError:
                self._remaining(started, deadline, None)
                if attempt == 2:
                    raise
                self._pause(min(10.0, self.poll_interval * 2 ** attempt),
                            started, deadline, None)
            else:
                break
        task_id = response.get("taskId")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise SolverError("CapSolver returned an invalid taskId", provider=self.name)
        # A ready token is returned even when it landed on the deadline: it has
        # been paid for, and the caller's deadline exists to stop waiting, not
        # to discard a result already in hand.
        if response.get("status") == "ready":
            return self._solution(response, task_id, started)
        self._remaining(started, deadline, task_id)
        if task_id is None:
            raise SolverError("CapSolver returned no taskId", provider=self.name)
        _LOG.debug("task_id=%s status=created elapsed=%.1fs",
                   self._redact(task_id), self._clock() - started)

        delay = self.poll_interval
        for _ in range(120):
            self._pause(delay, started, deadline, task_id)
            remaining = self._remaining(started, deadline, task_id)
            try:
                response = self._request("/getTaskResult", {"taskId": task_id}, remaining)
            except SolverUnavailableError:
                self._remaining(started, deadline, task_id)
                delay = min(10.0, delay * 2)
                continue
            status = response.get("status")
            if status not in ("idle", "processing", "ready"):
                raise SolverError("CapSolver returned an invalid task status", provider=self.name)
            _LOG.debug("task_id=%s status=%s elapsed=%.1fs",
                       self._redact(task_id), status, self._clock() - started)
            if status == "ready":
                return self._solution(response, task_id, started)
            delay = self.poll_interval
        raise SolverTimeoutError(
            f"CapSolver task {self._redact(task_id)} exhausted 120 result queries "
            f"after {self._clock() - started:.1f}s", provider=self.name,
        )

    def balance(self) -> float:
        response = self._request("/getBalance", {}, 30.0)
        try:
            balance = float(response["balance"])
        except (KeyError, TypeError, ValueError):
            balance = None
        if balance is None or not math.isfinite(balance):
            raise SolverError("CapSolver returned an invalid balance", provider=self.name) from None
        return balance
