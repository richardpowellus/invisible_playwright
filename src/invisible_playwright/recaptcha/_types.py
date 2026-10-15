"""The contract between the page helper and a solving service.

Everything a provider needs to know about a widget is a `RecaptchaChallenge`;
everything it hands back is a `RecaptchaSolution`. A provider is anything with
a `name` and a blocking `solve()`: the page helper never sees HTTP, and a
provider never sees a page, so a second service (2Captcha, say) is one new
class and no change here.

Two values must never reach a log, a repr or an exception message: the
service's API key, and a full token. A token is a bearer credential for the
form it was solved for until it expires, so `RecaptchaSolution.__repr__` shows
only its length and a short prefix.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

#: How a widget asks for a token. `v2` is the "I'm not a robot" checkbox,
#: `v2-invisible` is v2 with `size=invisible` (no checkbox, a challenge may
#: still appear), `v3` is score-based and never shows anything.
RecaptchaKind = Literal["v2", "v2-invisible", "v3"]


@dataclass(frozen=True)
class RecaptchaChallenge:
    """One reCAPTCHA widget found on a page, described for a solving service."""

    #: The URL of the document that rendered the widget (the frame's own URL
    #: when the widget lives in a child frame), not the anchor iframe's URL.
    website_url: str
    sitekey: str
    kind: RecaptchaKind
    #: Loaded through `enterprise.js` / an `/enterprise/` anchor.
    enterprise: bool = False
    #: v3: the `action` passed to `execute`. v2: the anchor's `sa` parameter.
    action: str | None = None
    #: The anchor's `s` parameter (`data-s` on the widget). Sent as
    #: `enterprisePayload.s` for enterprise, `recaptchaDataSValue` otherwise.
    data_s: str | None = None
    #: The host that served the script when it is not www.google.com, e.g.
    #: "www.recaptcha.net". None means google.com, the provider default.
    api_domain: str | None = None
    #: The page's success callback, as found: a global function name from
    #: `data-callback`, or a `___grecaptcha_cfg.clients[...]` path. Informative
    #: only; the helper resolves it again when it injects.
    callback: str | None = None
    #: The browser's own `navigator.userAgent`. Providers whose task accepts a
    #: user agent send it; CapSolver's reCAPTCHA tasks document no such input
    #: field, so it is used there only to compare with the solution's.
    user_agent: str | None = None

    @property
    def is_invisible(self) -> bool:
        return self.kind == "v2-invisible"

    @property
    def version(self) -> Literal["v2", "v3"]:
        return "v3" if self.kind == "v3" else "v2"


@dataclass(frozen=True)
class RecaptchaSolution:
    """A solved token and what the service reported alongside it."""

    token: str = field(repr=False)
    provider: str
    task_id: str | None = None
    #: The user agent the service solved with, when it says. A site that binds
    #: the token to the UA may reject it when this differs from the browser's.
    user_agent: str | None = None
    sec_ch_ua: str | None = None
    #: Cookies the service returned for the page's origin (CapSolver:
    #: `recaptcha-ca-t`, `recaptcha-ca-e`). Values are credentials too.
    cookies: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: When the service created the token, epoch milliseconds, if reported.
    created_at_ms: int | None = None
    #: Wall-clock seconds from task creation to a ready result.
    elapsed_s: float = 0.0

    def __repr__(self) -> str:
        return (f"RecaptchaSolution(provider={self.provider!r}, "
                f"task_id={self.task_id!r}, token={_mask(self.token)}, "
                f"cookies={sorted(self.cookies)!r}, "
                f"elapsed_s={self.elapsed_s:.1f})")


@runtime_checkable
class RecaptchaSolver(Protocol):
    """A solving service. `solve` blocks until a token, an error or `timeout`."""

    name: str

    def solve(self, challenge: RecaptchaChallenge, *,
              timeout: float) -> RecaptchaSolution:
        ...


@dataclass(frozen=True)
class RecaptchaResult:
    """What `solve_recaptcha` did on the page."""

    challenge: RecaptchaChallenge
    solution: RecaptchaSolution
    #: How many `g-recaptcha-response` fields received the token.
    fields_filled: int
    #: The page callback that was called with the token, or None.
    callback_called: str | None = None


def _mask(token: str | None) -> str:
    """A token as it may appear in a repr or a message: length and a prefix."""
    if not token:
        return "<empty>"
    return f"<{len(token)} chars, {token[:6]}...>"


# ── Errors ───────────────────────────────────────────────────────────────────
# One base, so a caller can catch everything this feature raises; a provider
# maps its own codes onto the SolverError subclasses, so a caller's handling
# does not change with the service.


class RecaptchaError(Exception):
    """Base for every error the reCAPTCHA helper raises."""


class RecaptchaNotFoundError(RecaptchaError):
    """No widget on the page or its frames matched."""


class RecaptchaAmbiguousError(RecaptchaError):
    """More than one distinct widget matched; pass `sitekey=` to choose."""


class RecaptchaInjectionError(RecaptchaError):
    """The token was solved but could not be handed to the page."""


class SolverError(RecaptchaError):
    """The solving service refused or failed. Never carries the API key."""

    def __init__(self, message: str, *, provider: str = "",
                 code: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        #: The service's own error code, e.g. "ERROR_ZERO_BALANCE".
        self.code = code


class SolverAuthError(SolverError):
    """Missing, wrong or temporarily blocked API key."""


class SolverBalanceError(SolverError):
    """The account cannot pay for the task."""


class SolverUnsolvableError(SolverError):
    """The service tried and gave up on this challenge."""


class SolverRequestError(SolverError):
    """The service rejected the task as malformed or unsupported."""


class SolverUnavailableError(SolverError):
    """The service could not be reached, or asked us to back off."""


class SolverTimeoutError(SolverError):
    """Our own deadline passed before the service had a result."""
