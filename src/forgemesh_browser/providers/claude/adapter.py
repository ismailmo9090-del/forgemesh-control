"""Claude web adapter (claude.ai). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "claude"
    display_name = "Claude"
    home_url = "https://claude.ai/new"
    login_url = "https://claude.ai/login"

    composer_selectors = (
        "div[contenteditable='true'].ProseMirror",
        "div[contenteditable='true'][data-testid='chat-input']",
        "div[contenteditable='true']",
    )
    send_button_selectors = (
        "button[aria-label='Send message']",
        "button[aria-label*='Send' i]",
        "button[type='submit']",
    )
    stop_button_selectors = ("button[aria-label*='Stop' i]",)
    response_selectors = (
        ".font-claude-message",
        "[data-testid='assistant-message']",
        "div[data-is-streaming] .font-claude-message",
    )
    logged_in_selectors = (
        "button[aria-label*='account' i]",
        "button[data-testid='user-menu-button']",
        "[data-testid='user-menu-button']",
    )
    logged_out_selectors = (
        "a[href*='/login']",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
    )
    attach_button_selectors = ("button[aria-label*='Attach' i]",)
    file_input_selectors = ("input[type=file]",)

    login_email_selectors = (
        "input[type='email']",
        "input[name='email']",
        "input[autocomplete='email']",
    )
    login_email_submit_selectors = (
        "button:has-text('Continue with email')",
        "button:has-text('Continue')",
        "button[type='submit']",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
