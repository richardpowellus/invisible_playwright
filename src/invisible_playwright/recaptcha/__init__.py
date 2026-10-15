"""Solve a page's reCAPTCHA through a paid solving service.

    from invisible_playwright.recaptcha import solve_recaptcha

    result = solve_recaptcha(page)          # CapSolver, key from CAPSOLVER_API_KEY

The helper finds the widget on the page and its frames, sends it to the
service, and hands the token to the page the way the page expects. See
docs/recaptcha-capsolver.md.
"""
from ._capsolver import CapSolver
from ._detect import detect_recaptcha, detect_recaptcha_async
from ._page import solve_recaptcha, solve_recaptcha_async
from ._types import (
    RecaptchaAmbiguousError,
    RecaptchaChallenge,
    RecaptchaError,
    RecaptchaInjectionError,
    RecaptchaKind,
    RecaptchaNotFoundError,
    RecaptchaResult,
    RecaptchaSolution,
    RecaptchaSolver,
    SolverAuthError,
    SolverBalanceError,
    SolverError,
    SolverRequestError,
    SolverTimeoutError,
    SolverUnavailableError,
    SolverUnsolvableError,
)

__all__ = [
    "CapSolver",
    "RecaptchaAmbiguousError",
    "RecaptchaChallenge",
    "RecaptchaError",
    "RecaptchaInjectionError",
    "RecaptchaKind",
    "RecaptchaNotFoundError",
    "RecaptchaResult",
    "RecaptchaSolution",
    "RecaptchaSolver",
    "SolverAuthError",
    "SolverBalanceError",
    "SolverError",
    "SolverRequestError",
    "SolverTimeoutError",
    "SolverUnavailableError",
    "SolverUnsolvableError",
    "detect_recaptcha",
    "detect_recaptcha_async",
    "solve_recaptcha",
    "solve_recaptcha_async",
]
