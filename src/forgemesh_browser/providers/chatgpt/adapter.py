"""ChatGPT web adapter (chatgpt.com). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "chatgpt"
    display_name = "ChatGPT"
    home_url = "https://chatgpt.com/"
    login_url = "https://chatgpt.com/auth/login"

    composer_selectors = (
        "#prompt-textarea",
        "div[contenteditable='true']#prompt-textarea",
        "[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
        "textarea[name='prompt-textarea']",
    )
    send_button_selectors = (
        "[data-testid='send-button']",
        "button[aria-label='Send']",
        "button[aria-label='Send prompt']",
        "button[aria-label*='Send' i]",
    )
    stop_button_selectors = (
        "[data-testid='stop-button']",
        "button[aria-label='Stop']",
        "button[aria-label*='Stop' i]",
    )
    # chatgpt.com dropped #prompt-textarea, data-testid='send-button' and
    # data-message-author-role; the old selectors stay first for older rollouts.
    response_selectors = (
        "[data-message-author-role='assistant']",
        "[data-markdown-text-style='assistant-message']",
        "[data-chatgpt-search-unit-key$='assistant']",
    )
    logged_in_selectors = (
        "[data-testid='profile-button']",
        "button[aria-label*='profile' i]",
        "nav [data-testid='accounts-profile-button']",
    )
    logged_out_selectors = (
        "[data-testid='login-button']",
        "a[href*='/auth/login']",
        "button:has-text('Log in')",
    )
    attach_button_selectors = (
        "button[aria-label*='Attach' i]",
        "button[aria-label*='Add files' i]",
    )
    file_input_selectors = ("input[type=file]",)

    login_start_selectors = (
        "[data-testid='login-button']",
        "a[href*='/auth/login']",
        "button:has-text('Log in')",
    )
    login_email_selectors = (
        "input[type='email']",
        "#email-input",
        "input[name='email']",
        "input[autocomplete='email']",
    )
    login_email_submit_selectors = (
        "button:has-text('Continue')",
        "button[type='submit']",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
