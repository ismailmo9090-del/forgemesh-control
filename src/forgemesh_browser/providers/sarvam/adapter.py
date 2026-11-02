"""Sarvam Indus web adapter (indus.sarvam.ai).

Indus is Sarvam AI's consumer chat product. Login is handled by Sarvam itself
(Google, Microsoft, Apple, phone or email); the adapter only automates the chat
UI after the human has signed in.

The post-login chat layout is behind a login wall, so the selectors below are
deliberately broad and must be re-verified after the first real login or any UI
change. The login page itself is exercised by ``detect_login_state`` tests.
"""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "sarvam"
    display_name = "Sarvam Indus"
    home_url = "https://indus.sarvam.ai/"
    login_url = "https://indus.sarvam.ai/login"

    composer_selectors = (
        "textarea[placeholder*='Ask' i]",
        "textarea[placeholder*='message' i]",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
        "textarea",
    )
    send_button_selectors = (
        "button[aria-label*='Send' i]",
        "button[data-testid*='send' i]",
        "button[type='submit']",
    )
    stop_button_selectors = (
        "button[aria-label*='Stop' i]",
        "button[aria-label*='Cancel' i]",
    )
    response_selectors = (
        "[data-role='assistant']",
        "[data-testid*='assistant' i]",
        "[class*='markdown']",
        "[class*='prose']",
        "[class*='message']",
    )
    logged_in_selectors = (
        "button[aria-label*='Account' i]",
        "button[aria-label*='Profile' i]",
        "[data-testid*='avatar' i]",
        "[class*='avatar']",
    )
    logged_out_selectors = (
        "button:has-text('Continue with Google')",
        "button:has-text('Continue with Microsoft')",
        "input[placeholder*='name@company.com']",
        "button:has-text('Continue')",
    )
    attach_button_selectors = (
        "button[aria-label*='upload' i]",
        "button[aria-label*='attach' i]",
    )
    file_input_selectors = ("input[type=file]",)

    login_email_selectors = (
        "input[type='email']",
        "input[placeholder*='name@company.com']",
        "input[name='email']",
    )
    login_submit_selectors = (
        "button[type='submit']",
        "button:has-text('Continue')",
        "button:has-text('Sign in')",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
