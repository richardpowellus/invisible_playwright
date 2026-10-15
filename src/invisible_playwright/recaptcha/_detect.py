"""Read each frame once, then interpret and merge the evidence in Python.

The browser returns only JSON-safe facts, never a client object or callback.
Keeping interpretation here makes URL formats, precedence and frame ownership
testable without a browser or a request to Google.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypedDict, TypeVar
from urllib.parse import parse_qs, urlsplit

from .._pw._impl._errors import Error
from ._types import RecaptchaChallenge, RecaptchaKind

if TYPE_CHECKING:
    from .._pw.async_api import Frame as AsyncFrame
    from .._pw.async_api import Page as AsyncPage
    from .._pw.sync_api import Frame, Page

_LOG = logging.getLogger(__name__)
_FrameT = TypeVar("_FrameT")


class _Widget(TypedDict):
    sitekey: str
    size: str | None
    action: str | None
    s: str | None
    callback: str | None


class _Client(TypedDict):
    id: str
    sitekey: str
    size: str | None
    callback: str | None
    callback_path: list[str] | None


class _Collected(TypedDict):
    widgets: list[_Widget]
    scripts: list[str]
    inline_scripts: list[str]
    clients: list[_Client]
    user_agent: str
    #: `performance.timeOrigin`: set once per document, so injection can tell
    #: the document it detected from a reload at the same URL.
    time_origin: float


@dataclass(frozen=True)
class _Facts:
    sitekey: str | None = None
    kind: RecaptchaKind | None = None
    enterprise: bool = False
    action: str | None = None
    data_s: str | None = None
    api_domain: str | None = None
    callback: str | None = None
    #: Where the fact came from. An anchor URL is Google's own statement about
    #: one widget; inline script text is a hint that proves nothing on its own.
    source: str = field(default="page", compare=False)


@dataclass(frozen=True)
class _Snapshot(Generic[_FrameT]):
    frame: _FrameT
    parent: _FrameT | None
    url: str
    data: _Collected | None


@dataclass(frozen=True)
class _Detected(Generic[_FrameT]):
    challenge: RecaptchaChallenge
    frame: _FrameT
    time_origin: float | None = None


# Shared with injection so the callback's discovery and resolution cannot
# disagree. Own data properties avoid invoking getters or following DOM graphs.
_CLIENTS_JS = r"""() => {
    const result = [];
    const clients = window.___grecaptcha_cfg?.clients;
    if (!clients || typeof clients !== 'object') return result;
    let remaining = 2048;
    for (const [id, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(clients))) {
        const seen = new WeakSet();
        const walk = (value, path, depth) => {
            if (!value || typeof value !== 'object' || seen.has(value) || remaining-- <= 0)
                return;
            seen.add(value);
            const props = Object.getOwnPropertyDescriptors(value);
            const sitekey = props.sitekey?.value;
            if (typeof sitekey === 'string' && sitekey) {
                const name = Number(id) >= 10000 ? 'promise-callback' : 'callback';
                const callback = props[name]?.value;
                const valid = typeof callback === 'function' || typeof callback === 'string';
                result.push({
                    id, sitekey,
                    size: typeof props.size?.value === 'string' ? props.size.value : null,
                    callback: typeof callback === 'function' ? '<function>' :
                        (typeof callback === 'string' ? callback : null),
                    callback_path: valid ? [...path, name] : null
                });
            }
            if (depth >= 2) return;
            for (const [key, prop] of Object.entries(props))
                if ('value' in prop) walk(prop.value, [...path, key], depth + 1);
        };
        walk(descriptor.value, ['___grecaptcha_cfg', 'clients', id], 0);
    }
    return result;
}"""

_COLLECT_JS = r"""() => {
    const widgets = Array.from(document.querySelectorAll('[data-sitekey]'), el => ({
        sitekey: el.getAttribute('data-sitekey'),
        size: el.getAttribute('data-size'),
        action: el.getAttribute('data-action'),
        s: el.getAttribute('data-s'),
        callback: el.getAttribute('data-callback')
    }));
    const scripts = [];
    const inline_scripts = [];
    let remaining = 65536;
    for (const script of document.scripts) {
        if (script.src) {
            scripts.push(script.src);
        } else if (remaining > 0) {
            const text = script.textContent.slice(0, 262144);
            const calls = text.matchAll(
                /grecaptcha\s*(?:\.\s*enterprise\s*)?\.\s*(?:execute|render)\s*\([^;]{0,2048}/g
            );
            for (const call of calls) {
                const excerpt = call[0].slice(0, remaining);
                inline_scripts.push(excerpt);
                remaining -= excerpt.length;
                if (remaining <= 0) break;
            }
        }
    }
    return {
        widgets, scripts, inline_scripts,
        clients: (__CLIENTS__)(),
        user_agent: navigator.userAgent,
        time_origin: performance.timeOrigin
    };
}""".replace("__CLIENTS__", _CLIENTS_JS)

_HOSTS = {"www.google.com", "google.com", "www.recaptcha.net", "recaptcha.net"}
_CALL = re.compile(
    r"\bgrecaptcha\s*(?P<enterprise>\.\s*enterprise\s*)?\.\s*"
    r"(?P<method>execute|render)\s*\(\s*"
    r"""(?P<quote>['"])(?P<argument>[^'"\r\n]+)(?P=quote)\s*,\s*"""
    r"\{(?P<options>[^{}]{0,2048})\}", re.DOTALL,
)
_OPTION = re.compile(
    r"""(?:^|,)\s*['"]?(sitekey|action|size|callback)['"]?\s*:\s*"""
    r"""(['"])([^'"\r\n]*)\2""",
)


def _recaptcha_url(url: str) -> tuple[str, str, dict[str, list[str]]] | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme not in {"http", "https"} or host is None or host not in _HOSTS:
        return None
    return host, parts.path, parse_qs(parts.query)


def _query_value(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    return values[0] if values else None


def _parse_anchor_url(url: str) -> _Facts | None:
    parsed = _recaptcha_url(url)
    if parsed is None:
        return None
    host, path, params = parsed
    if path not in {"/recaptcha/api2/anchor", "/recaptcha/enterprise/anchor"}:
        return None
    key = _query_value(params, "k")
    if not key:
        return None
    return _Facts(
        sitekey=key,
        kind="v2-invisible" if params.get("size") == ["invisible"] else "v2",
        enterprise="/enterprise/" in path,
        action=_query_value(params, "sa"),
        data_s=_query_value(params, "s"),
        api_domain=host if host.endswith("recaptcha.net") else None,
        source="anchor",
    )


def _parse_script_src(url: str) -> _Facts | None:
    parsed = _recaptcha_url(url)
    if parsed is None:
        return None
    host, path, params = parsed
    if path not in {"/recaptcha/api.js", "/recaptcha/enterprise.js"}:
        return None
    key = _query_value(params, "render")
    if key == "explicit":
        key = None
    return _Facts(
        sitekey=key, kind="v3" if key else None,
        enterprise=path.endswith("/enterprise.js"),
        api_domain=host if host.endswith("recaptcha.net") else None,
    )


def _parse_inline_scripts(scripts: list[str]) -> list[_Facts]:
    """Recognise literal calls, not arbitrary JavaScript or variable values."""
    facts = []
    remaining = 65536
    for script in scripts:
        excerpt = script[:remaining]
        remaining -= len(excerpt)
        for match in _CALL.finditer(excerpt):
            options = {key: value for key, _, value in
                       _OPTION.findall(match["options"])}
            execute = match["method"] == "execute"
            key = match["argument"] if execute else options.get("sitekey")
            if not key:
                continue
            facts.append(_Facts(
                sitekey=key,
                kind=("v3" if execute else
                      "v2-invisible" if options.get("size") == "invisible" else "v2"),
                enterprise=bool(match["enterprise"]),
                action=options.get("action"),
                callback=options.get("callback") if not execute else None,
                source="inline",
            ))
        if remaining <= 0:
            break
    return facts


def _callback_path(parts: list[str]) -> str:
    return parts[0] + "".join(f"[{json.dumps(part)}]" for part in parts[1:])


def _frame_facts(data: _Collected) -> list[_Facts]:
    facts = [fact for src in data["scripts"]
             if (fact := _parse_script_src(src)) is not None]
    for client in data["clients"]:
        callback = client["callback"]
        if callback == "<function>" and client["callback_path"]:
            callback = _callback_path(client["callback_path"])
        facts.append(_Facts(
            sitekey=client["sitekey"],
            kind=("v3" if client["id"].isdigit() and int(client["id"]) >= 10000
                  else "v2-invisible" if client["size"] == "invisible" else "v2"),
            callback=callback,
        ))
    facts.extend(_parse_inline_scripts(data["inline_scripts"]))
    for widget in data["widgets"]:
        if widget["sitekey"]:
            facts.append(_Facts(
                sitekey=widget["sitekey"],
                kind="v2-invisible" if widget["size"] == "invisible" else "v2",
                action=widget["action"] or None, data_s=widget["s"] or None,
                callback=widget["callback"] or None,
            ))
    return facts


def _merge_facts(facts: list[_Facts], *, website_url: str,
                 user_agent: str | None) -> list[RecaptchaChallenge]:
    """Merge by key; v3 evidence beats an invisible anchor's v2-like shape.

    Unkeyed script metadata applies to widgets in that document, while a
    keyed script must not turn a different widget into an enterprise widget.
    Later nonempty fields win; collection orders DOM callbacks after clients.
    """
    grouped: dict[str, list[_Facts]] = {}
    common = [fact for fact in facts if fact.sitekey is None]
    for fact in facts:
        if fact.sitekey:
            grouped.setdefault(fact.sitekey, []).append(fact)
    challenges = []
    for key, specific in grouped.items():
        # Text in a script (possibly a comment, possibly dead code) is not a
        # widget. Charging a paid task for it needs something live as well.
        if all(fact.source == "inline" for fact in specific):
            continue
        sources = common + specific
        kinds = {fact.kind for fact in sources}
        # The anchor is Google's own description of THIS widget. A script shared
        # by the page (an enterprise.js?render=explicit, a recaptcha.net copy)
        # must not override it for a widget whose anchor says otherwise.
        anchor = next((fact for fact in reversed(specific) if fact.source == "anchor"), None)
        challenges.append(RecaptchaChallenge(
            website_url=website_url, sitekey=key,
            kind="v3" if "v3" in kinds else
                 "v2-invisible" if "v2-invisible" in kinds else "v2",
            enterprise=(anchor.enterprise if anchor is not None
                        else any(fact.enterprise for fact in sources)),
            action=_last([fact.action for fact in sources]),
            data_s=_last([fact.data_s for fact in sources]),
            api_domain=(anchor.api_domain if anchor is not None
                        else _last([fact.api_domain for fact in sources])),
            callback=_last([fact.callback for fact in sources]),
            user_agent=user_agent,
        ))
    return challenges


def _last(values: list[str | None]) -> str | None:
    return next((value for value in reversed(values) if value is not None), None)


def _assemble(snapshots: list[_Snapshot[_FrameT]]) -> list[_Detected[_FrameT]]:
    by_frame = {id(item.frame): item for item in snapshots}
    snapshots = list(by_frame.values())
    facts = {id(item.frame): _frame_facts(item.data) if item.data else []
             for item in snapshots}
    user_agent = next((item.data["user_agent"] for item in snapshots
                       if item.parent is None and item.data), None)
    for item in snapshots:
        anchor = _parse_anchor_url(item.url)
        if anchor and item.parent is not None and id(item.parent) in by_frame:
            facts[id(item.parent)].append(anchor)
    found = []
    for item in snapshots:
        # An anchor's own document is Google's, not the customer's form.
        if _parse_anchor_url(item.url):
            continue
        ua = user_agent or (item.data["user_agent"] if item.data else None)
        origin = item.data["time_origin"] if item.data else None
        found.extend(_Detected(challenge, item.frame, origin) for challenge in
                     _merge_facts(facts[id(item.frame)], website_url=item.url,
                                  user_agent=ua))
    return found


#: reCAPTCHA's own widget documents: the checkbox (anchor) and the challenge
#: (bframe). Named exactly, because Google serves customer-facing pages under
#: the same prefix - the public demo is /recaptcha/api2/demo.
_GOOGLE_FRAMES = {f"/recaptcha/{api}/{doc}" for api in ("api2", "enterprise")
                  for doc in ("anchor", "bframe")}


def _is_google_frame(url: str) -> bool:
    """reCAPTCHA's own anchor and challenge documents.

    Their URL already carries everything detection needs (`k`, `size`, `sa`,
    `s`), and they are the documents reCAPTCHA itself watches, so no script is
    evaluated inside them: the scan reads them by URL only.
    """
    parsed = _recaptcha_url(url)
    return parsed is not None and parsed[1] in _GOOGLE_FRAMES


def _detect_widgets(page: Page) -> list[_Detected[Frame]]:
    snapshots = []
    for frame in dict.fromkeys(page.frames):
        if _is_google_frame(frame.url):
            snapshots.append(_Snapshot(frame, frame.parent_frame, frame.url, None))
            continue
        try:
            data = frame.evaluate(_COLLECT_JS)
        except Error:
            # Navigation and detachment are ordinary during a frame scan.
            _LOG.debug("Skipping unreadable reCAPTCHA frame during detection")
            data = None
        snapshots.append(_Snapshot(frame, frame.parent_frame, frame.url, data))
    return _assemble(snapshots)


async def _detect_widgets_async(page: AsyncPage) -> list[_Detected[AsyncFrame]]:
    snapshots = []
    for frame in dict.fromkeys(page.frames):
        if _is_google_frame(frame.url):
            snapshots.append(_Snapshot(frame, frame.parent_frame, frame.url, None))
            continue
        try:
            data = await frame.evaluate(_COLLECT_JS)
        except Error:
            _LOG.debug("Skipping unreadable reCAPTCHA frame during detection")
            data = None
        snapshots.append(_Snapshot(frame, frame.parent_frame, frame.url, data))
    return _assemble(snapshots)


def detect_recaptcha(page: Page) -> list[RecaptchaChallenge]:
    """Describe widgets across the page's frames without changing the page."""
    return [widget.challenge for widget in _detect_widgets(page)]


async def detect_recaptcha_async(page: AsyncPage) -> list[RecaptchaChallenge]:
    """Async equivalent of :func:`detect_recaptcha`."""
    return [widget.challenge for widget in await _detect_widgets_async(page)]
