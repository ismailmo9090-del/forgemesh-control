"""DeepSeek web adapter (chat.deepseek.com). Validate selectors after UI changes."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "deepseek"
    display_name = "DeepSeek"
    home_url = "https://chat.deepseek.com/"
    login_url = "https://chat.deepseek.com/sign_in"

    composer_selectors = (
        "textarea#chat-input",
        "textarea",
    )
    send_button_selectors = (
        "button[aria-label*='Send' i]",
        "div[role='button'][aria-label*='Send' i]",
    )
    # The send button turns into an unlabelled button whose icon gets
    # .ds-loading while the answer streams. Without it a two-second pause in
    # the stream was read as a finished answer (the transcript got a lone
    # "- 4" list marker instead of the JSON reply).
    stop_button_selectors = ("button[aria-label*='Stop' i]", ".ds-loading")
    # DeepSeek regularly pauses for over two seconds between token bursts while
    # the answer element still shows only a list marker, so give it a longer
    # quiet window before the text counts as final.
    stability_ms = 4_000
    response_selectors = (
        ".ds-markdown",
        "[class*='markdown']",
    )
    logged_in_selectors = (
        "[class*='avatar']",
        "[class*='user-info']",
    )
    logged_out_selectors = (
        "a:has-text('Log in')",
        "button:has-text('Log in')",
        "div:has-text('Sign in')",
    )
    attach_button_selectors = ("button[aria-label*='upload' i]",)
    file_input_selectors = ("input[type=file]",)

    login_email_selectors = (
        "input[type='email']",
        "input[name='email']",
        "input[placeholder*='email' i]",
        "input[placeholder*='phone' i]",
    )
    login_password_selectors = ("input[type='password']", "input[name='password']")
