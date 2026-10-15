"""The deliberate script-action exception to real pointer/keyboard input.

A service's response token belongs in hidden fields and JavaScript callbacks;
there is no user gesture that can type it into that interface. Token injection
is therefore the one script action here, confined to the widget's frame and
the detected URL. A navigation during the paid solve must not hand the token
to another page. It does not submit a form: the caller still submits with a
real pointer click.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from .._pw._impl._errors import Error
from ._detect import _CLIENTS_JS
from ._types import RecaptchaInjectionError

if TYPE_CHECKING:
    from .._pw.async_api import Frame as AsyncFrame
    from .._pw.sync_api import Frame


class _InjectionResult(TypedDict):
    filled: int
    callback: str | None


# One action for both APIs: fill all response fields, then call one callback.
# Resolve names as properties, never eval: a callback name is data, not code.
_INJECT_JS = r"""({token, sitekey, websiteURL, invokeCallback}) => {
    if (window.location.href !== websiteURL)
        throw new Error('The reCAPTCHA document navigated during solving');
    const fields = document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], [id^="g-recaptcha-response"]'
    );
    for (const field of fields) {
        field.value = token;
        field.textContent = token;
    }
    const resolve = parts => {
        let owner = window;
        for (const part of parts.slice(0, -1)) {
            owner = owner?.[part];
            if (owner == null) return null;
        }
        const fn = owner?.[parts[parts.length - 1]];
        return typeof fn === 'function' ? {owner, fn} : null;
    };
    let callback = null;
    if (invokeCallback) {
        for (const widget of document.querySelectorAll('[data-sitekey]')) {
            if (widget.getAttribute('data-sitekey') !== sitekey) continue;
            const name = widget.getAttribute('data-callback');
            const target = name ? resolve(name.split('.')) : null;
            if (target) {
                target.fn.call(target.owner, token);
                callback = name;
                break;
            }
        }
        if (callback === null) {
            for (const client of (__CLIENTS__)()) {
                if (client.sitekey !== sitekey || !client.callback_path) continue;
                const path = client.callback === '<function>' ? client.callback_path :
                    client.callback.split('.');
                const target = resolve(path);
                if (target) {
                    target.fn.call(target.owner, token);
                    callback = client.callback === '<function>' ?
                        path[0] + path.slice(1).map(p => '[' + JSON.stringify(p) + ']').join('') :
                        client.callback;
                    break;
                }
            }
        }
    }
    return {filled: fields.length, callback};
}""".replace("__CLIENTS__", _CLIENTS_JS)


def _check_injection(result: _InjectionResult) -> _InjectionResult:
    if result["filled"] == 0 and result["callback"] is None:
        raise RecaptchaInjectionError(
            "No reCAPTCHA response field or callable callback was found")
    return result


def _inject_token(frame: Frame, *, sitekey: str, website_url: str, token: str,
                  invoke_callback: bool) -> _InjectionResult:
    if not token:
        raise RecaptchaInjectionError("The solver returned an empty response token")
    try:
        result = frame.evaluate(_INJECT_JS, {
            "token": token, "sitekey": sitekey, "invokeCallback": invoke_callback,
            "websiteURL": website_url,
        })
    except Error:
        # A site's callback can throw the token itself. Do not chain that error.
        raise RecaptchaInjectionError(
            "The reCAPTCHA frame could not accept the response token") from None
    return _check_injection(result)


async def _inject_token_async(frame: AsyncFrame, *, sitekey: str, website_url: str, token: str,
                              invoke_callback: bool) -> _InjectionResult:
    if not token:
        raise RecaptchaInjectionError("The solver returned an empty response token")
    try:
        result = await frame.evaluate(_INJECT_JS, {
            "token": token, "sitekey": sitekey, "invokeCallback": invoke_callback,
            "websiteURL": website_url,
        })
    except Error:
        raise RecaptchaInjectionError(
            "The reCAPTCHA frame could not accept the response token") from None
    return _check_injection(result)
