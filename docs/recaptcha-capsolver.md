---
title: "Solving reCAPTCHA with CapSolver"
description: "solve_recaptcha(page): find a page's reCAPTCHA (v2 checkbox, invisible v2, v3, Enterprise), have CapSolver solve it, and hand the token to the page the way its own widget would."
parent: "Documentation"
nav_order: 7
---


# Solving reCAPTCHA with CapSolver

A coherent browser lowers how often reCAPTCHA challenges you; it does not make the
challenge go away. When a flow has to get past one, `invisible_playwright.recaptcha`
sends the widget to a paid solving service and gives the page the token it would
have received from Google. [CapSolver](https://www.capsolver.com/) is the service
implemented today; the solver is an interface, so another can be added without
touching the page side.

```python
from invisible_playwright import InvisiblePlaywright
from invisible_playwright.recaptcha import solve_recaptcha

with InvisiblePlaywright() as browser:
    page = browser.new_page()
    page.goto("https://www.google.com/recaptcha/api2/demo")
    result = solve_recaptcha(page)               # key from CAPSOLVER_API_KEY
    page.locator("#recaptcha-demo-submit").click()  # the real pointer submits
```

The async API is the same call with `await solve_recaptcha_async(page)`.

## The API key

The key is read from `CAPSOLVER_API_KEY`, or passed explicitly:

```python
from invisible_playwright.recaptcha import CapSolver, solve_recaptcha

solve_recaptcha(page, solver=CapSolver(api_key=key))
# or, for the default solver:
solve_recaptcha(page, api_key=key)
```

The key never appears in a log, a `repr` or an exception message, and a solved
token is shown only as its length and first characters. A token is a credential
for the form it was solved for until it expires, so treat it like one.

`CapSolver(...).balance()` returns the account balance in USD, which is a cheap way
to check the key works before a run.

## What it does on the page

1. **Finds the widget**, in the page and every frame: `data-sitekey` elements and
   their `data-size`, `data-callback`, `data-action` and `data-s`; the
   `api.js` / `enterprise.js` script and its `render=` key; the page's
   `___grecaptcha_cfg` clients; and the anchor iframe's URL (`k`, `size`, `sa`,
   `s`). From those it decides v2 checkbox, invisible v2 or v3, Enterprise or not,
   whether the script came from `www.recaptcha.net`, and the v3 action where the
   page's own `grecaptcha.execute(...)` call names it.
2. **Sends a task** to CapSolver: `ReCaptchaV2TaskProxyLess`,
   `ReCaptchaV2EnterpriseTaskProxyLess`, `ReCaptchaV3TaskProxyLess` or
   `ReCaptchaV3EnterpriseTaskProxyLess`, with `pageAction`, `isInvisible`,
   `enterprisePayload` / `recaptchaDataSValue` and `apiDomain` only when the page
   supplied them. It then polls `getTaskResult` every 3 to 10 seconds (CapSolver
   asks for 3 between queries, and allows 120 queries and 5 minutes per task)
   until the token is ready or `timeout` passes.
3. **Hands the token over**: writes it into every `g-recaptcha-response` field of
   the widget's frame and, unless `invoke_callback=False`, calls the widget's
   `data-callback` or the callback the page registered with `grecaptcha.render`,
   exactly as Google's script would after a solve.

A widget is only sent for solving when the page has it live: a `data-sitekey`
element, a `render=` script, a registered client or an anchor frame. A
`grecaptcha.execute(...)` that appears only as script text (it may be a comment)
adds the action to a live widget but never creates one, so it cannot cost a
task. Before the token is written, the helper checks the frame still holds the
same document it detected; a navigation or a reload during the solve raises
`RecaptchaInjectionError` instead of handing the token to the new page.

It does not submit the form. Submitting is a click, and clicks in this library go
through the real pointer.

### Why the token is written by a script

Everything else this library does to a page goes through the real pointer and
keyboard. A reCAPTCHA token cannot: it is not something a person types, it is a
value Google's own script writes into a hidden field and passes to a callback.
Writing it is therefore the one script action this feature takes, in the page's
own world, confined to the `g-recaptcha-response` fields and the page's callback.
The helper reads the page to detect the widget and does nothing else.

## Choosing, overriding and checking

```python
from invisible_playwright.recaptcha import detect_recaptcha, solve_recaptcha

for challenge in detect_recaptcha(page):
    print(challenge.kind, challenge.sitekey, challenge.enterprise, challenge.action)

result = solve_recaptcha(
    page,
    sitekey="6Lc...",      # when a page carries more than one widget
    action="login",        # when the v3 action is not visible in the page
    timeout=120,           # seconds; at most 300, CapSolver's task lifetime
)
print(result.fields_filled, result.callback_called, result.solution)
```

`result.solution.user_agent` is the user agent CapSolver solved with. Its
reCAPTCHA tasks take no user agent as input (the documented task fields are the
site key, URL, action, the `s` payload, invisible, session and API domain), so a
site that binds a token tightly to the browser's user agent may still refuse it.

## Errors

Everything raised once the call starts is a `RecaptchaError` (an out-of-range
`timeout` is a `ValueError`, before anything is detected or paid for):

| Error | When |
|---|---|
| `RecaptchaNotFoundError` | No widget on the page or its frames |
| `RecaptchaAmbiguousError` | Several site keys and no `sitekey=` |
| `RecaptchaInjectionError` | Solved, but the page had no field or callback to take it |
| `SolverAuthError` | No key, a wrong key, or a temporarily blocked one |
| `SolverBalanceError` | The account cannot pay for the task |
| `SolverUnsolvableError` | CapSolver tried and gave up |
| `SolverRequestError` | CapSolver rejected the task as malformed or unsupported |
| `SolverUnavailableError` | CapSolver unreachable or rate limiting, after retries |
| `SolverTimeoutError` | `timeout` passed before a token was ready |

A `SolverError` that CapSolver itself reported carries CapSolver's code in
`.code`, for example `ERROR_ZERO_BALANCE`; a missing key, a network failure or
our own timeout has `.code` set to `None`.

### What a call costs

One solve is one CapSolver task, charged by CapSolver. When the reply to
`createTask` is lost in transit the call retries, at most three attempts, and
CapSolver may already have accepted (and will charge for) the task whose reply
was lost: a call costs one task in the usual case and at most three.

## Another solving service

A solver is any object with a `name` and
`solve(challenge, *, timeout) -> RecaptchaSolution`; pass it as `solver=`. The
`RecaptchaChallenge` it receives already holds everything the page side found, so
a 2Captcha implementation, for instance, only maps those fields onto its own task
and its error codes onto the classes above.

## Tests

The CapSolver client is tested against a mocked HTTP API, and detection against
local pages for each widget type, with no request leaving the machine. One live
test runs against Google's demo page and checks Google accepts the token; it is
skipped unless `CAPSOLVER_API_KEY` is set, and each run is a paid CapSolver task:

```bash
CAPSOLVER_API_KEY=... pytest -m e2e tests/test_recaptcha_live_e2e.py
```

## Related

- [Does Playwright trigger reCAPTCHA more often?](does-playwright-trigger-recaptcha.md)
- [reCAPTCHA v3 score: why a fresh browser scores badly](recaptcha-v3-score.md)
- [reCAPTCHA Enterprise vs reCAPTCHA v3](recaptcha-enterprise-vs-v3.md)
