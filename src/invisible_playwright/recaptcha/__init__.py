"""Solve a page's reCAPTCHA through a paid solving service.

    from invisible_playwright.recaptcha import solve_recaptcha

    result = solve_recaptcha(page)          # CapSolver, key from CAPSOLVER_API_KEY

The helper finds the widget on the page and its frames, sends it to the
service, and hands the token to the page the way the page expects. See
docs/recaptcha-capsolver.md.
"""
import base64

from ._capsolver import CapSolver
from ._detect import detect_recaptcha, detect_recaptcha_async
from ._page import solve_recaptcha, solve_recaptcha_async
from ._types import (
    ImageTextSolver,
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
    "ImageTextSolver",
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
    "solve_image_text",
]


def solve_image_text(
    image: bytes, *, solver: ImageTextSolver | None = None,
    module: str | None = None, case_sensitive: bool = False,
) -> str:
    """Recognise text in an image, using CAPSOLVER_API_KEY by default."""
    provider = CapSolver() if solver is None else solver
    body = base64.b64encode(image).decode("ascii")
    text = provider.solve_image_to_text(body, module=module, case_sensitive=case_sensitive)
    if not isinstance(text, str) or not text.strip():
        raise SolverError("Image solver returned no text")
    return text.strip()
