"""Perplexity web adapter (perplexity.ai). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "perplexity"
    display_name = "Perplexity"
    home_url = "https://www.perplexity.ai/"
    login_url = "https://www.perplexity.ai/"

    composer_selectors = (
        "textarea[placeholder*='Ask' i]",
        "div[contenteditable='true']",
        "textarea",
    )
    send_button_selectors = (
        "button[aria-label*='Submit' i]",
        "button[aria-label*='Send' i]",
        "button[type='submit']",
    )
    stop_button_selectors = ("button[aria-label*='Stop' i]",)
    response_selectors = (
        ".prose",
        "[class*='answer']",
    )
    logged_in_selectors = (
        "button[aria-label*='Account' i]",
        "button[aria-label*='Profile' i]",
        "[class*='avatar']",
    )
    logged_out_selectors = (
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
    )
    attach_button_selectors = ("button[aria-label*='Attach' i]",)
    file_input_selectors = ("input[type=file]",)

    login_start_selectors = (
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
    )
    login_email_selectors = (
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='email']",
    )
    login_email_submit_selectors = (
        "button:has-text('Continue')",
        "button[type='submit']",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
