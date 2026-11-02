"""Kimi web adapter (kimi.com). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "kimi"
    display_name = "Kimi"
    home_url = "https://www.kimi.com/"
    login_url = "https://www.kimi.com/"

    composer_selectors = (
        "div[contenteditable='true']",
        "textarea",
    )
    send_button_selectors = (
        "[data-testid='send-button']",
        "button[aria-label*='Send' i]",
        "button[type='submit']",
    )
    stop_button_selectors = ("button[aria-label*='Stop' i]",)
    response_selectors = (
        ".markdown",
        ".segment-content",
        "[class*='assistant-message']",
    )
    logged_in_selectors = (
        "[class*='avatar']",
        "[data-testid='user-avatar']",
    )
    logged_out_selectors = (
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "a:has-text('Log in')",
    )
    attach_button_selectors = ("button[aria-label*='upload' i]",)
    file_input_selectors = ("input[type=file]",)

    login_start_selectors = (
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "a:has-text('Log in')",
    )
    login_email_selectors = (
        "input[type='email']",
        "input[placeholder*='email' i]",
        "input[placeholder*='phone' i]",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
