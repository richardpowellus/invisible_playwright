"""Opt-in paid solve against Google's public demo, never part of the default suite."""
from __future__ import annotations

import os

import pytest

from invisible_playwright import InvisiblePlaywright
from invisible_playwright.recaptcha._page import solve_recaptcha

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("CAPSOLVER_API_KEY"),
                       reason="CAPSOLVER_API_KEY must be set for the paid live reCAPTCHA test"),
]


def test_google_demo_accepts_a_service_token(firefox_binary):
    with InvisiblePlaywright(seed=4243, binary_path=firefox_binary,
                             headless=True) as browser:
        page = browser.new_context().new_page()
        page.goto("https://www.google.com/recaptcha/api2/demo", wait_until="load",
                  timeout=60_000)
        page.locator('iframe[src*="/anchor"]').wait_for(timeout=30_000)
        result = solve_recaptcha(page)
        assert result.fields_filled > 0
        page.locator("#recaptcha-demo-submit").click()
        page.wait_for_function(
            "() => /Verification Success|Hooray/i.test(document.body.innerText)",
            timeout=30_000,
        )
