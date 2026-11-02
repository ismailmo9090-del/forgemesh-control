"""Grok web adapter (grok.com). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "grok"
    display_name = "Grok"
    home_url = "https://grok.com/"
    login_url = "https://grok.com/"

    composer_selectors = (
        "textarea[aria-label*='Ask' i]",
        "div[contenteditable='true']",
        "textarea",
    )
    send_button_selectors = (
        "button[aria-label*='Submit' i]",
        "button[type='submit']",
    )
    stop_button_selectors = ("button[aria-label*='Stop' i]",)
    response_selectors = (
        ".message-bubble",
        "[class*='response-content']",
        "[class*='prose']",
    )
    logged_in_selectors = (
        "button[aria-label*='Account' i]",
        "[class*='avatar']",
    )
    logged_out_selectors = (
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
        "button:has-text('Log in')",
    )
    attach_button_selectors = ("button[aria-label*='Attach' i]",)
    file_input_selectors = ("input[type=file]",)

    login_start_selectors = (
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
    )
    login_email_selectors = (
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='email']",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
