"""Find, solve and inject a widget; never submit or install solver cookies.

Returned cookies remain in the solution for the caller to inspect. Applying
them implicitly would change browser state beyond the requested token.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ._detect import _detect_widgets, _detect_widgets_async, _Detected, _FrameT
from ._inject import _inject_token, _inject_token_async
from ._types import (
    RecaptchaAmbiguousError,
    RecaptchaChallenge,
    RecaptchaNotFoundError,
    RecaptchaResult,
    RecaptchaSolution,
    RecaptchaSolver,
)

if TYPE_CHECKING:
    from .._pw.async_api import Page as AsyncPage
    from .._pw.sync_api import Page

_LOG = logging.getLogger(__name__)


def _validate_timeout(timeout: float) -> None:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300):
        raise ValueError("reCAPTCHA timeout must be greater than 0 and at most 300 seconds")


def _select_widget(widgets: list[_Detected[_FrameT]],
                   sitekey: str | None) -> _Detected[_FrameT]:
    matches = [widget for widget in widgets
               if sitekey is None or widget.challenge.sitekey == sitekey]
    if not matches:
        raise RecaptchaNotFoundError("No matching reCAPTCHA widget was found")
    if len(matches) != 1:
        raise RecaptchaAmbiguousError(
            "Multiple reCAPTCHA widgets match; pass a unique sitekey or narrow the page")
    return matches[0]


def _solver(solver: RecaptchaSolver | None, api_key: str | None) -> RecaptchaSolver:
    if solver is None:
        from ._capsolver import CapSolver

        return CapSolver(api_key)
    return solver


def _check_user_agent(challenge: RecaptchaChallenge, solution: RecaptchaSolution) -> None:
    if (solution.user_agent and challenge.user_agent
            and solution.user_agent != challenge.user_agent):
        _LOG.debug("reCAPTCHA solver user agent differs from the browser's user agent")


def solve_recaptcha(
    page: Page, *, solver: RecaptchaSolver | None = None, api_key: str | None = None,
    sitekey: str | None = None, action: str | None = None,
    timeout: float = 120.0, invoke_callback: bool = True,
) -> RecaptchaResult:
    """Solve one unambiguous widget and hand its token to its own frame.

    ``timeout`` (0 < seconds <= 300) bounds the solving service. ``action``
    overrides detected action metadata. Identical keys in separate frames are
    still separate widgets; they are not safe to choose between implicitly.
    Submission remains the caller's real click.
    """
    _validate_timeout(timeout)
    widget = _select_widget(_detect_widgets(page), sitekey)
    challenge = (replace(widget.challenge, action=action) if action is not None
                 else widget.challenge)
    solution = _solver(solver, api_key).solve(challenge, timeout=timeout)
    _check_user_agent(challenge, solution)
    injected = _inject_token(widget.frame, sitekey=challenge.sitekey,
                             time_origin=widget.time_origin,
                             website_url=challenge.website_url,
                             token=solution.token, invoke_callback=invoke_callback)
    return RecaptchaResult(challenge, solution, injected["filled"], injected["callback"])


async def solve_recaptcha_async(
    page: AsyncPage, *, solver: RecaptchaSolver | None = None, api_key: str | None = None,
    sitekey: str | None = None, action: str | None = None,
    timeout: float = 120.0, invoke_callback: bool = True,
) -> RecaptchaResult:
    """Async equivalent of :func:`solve_recaptcha`; solving runs off the event loop."""
    _validate_timeout(timeout)
    widget = _select_widget(await _detect_widgets_async(page), sitekey)
    challenge = (replace(widget.challenge, action=action) if action is not None
                 else widget.challenge)
    solution = await asyncio.to_thread(_solver(solver, api_key).solve,
                                       challenge, timeout=timeout)
    _check_user_agent(challenge, solution)
    injected = await _inject_token_async(widget.frame, sitekey=challenge.sitekey,
                                         time_origin=widget.time_origin,
                                         website_url=challenge.website_url,
                                         token=solution.token,
                                         invoke_callback=invoke_callback)
    return RecaptchaResult(challenge, solution, injected["filled"], injected["callback"])
