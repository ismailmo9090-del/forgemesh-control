"""Google Gemini web adapter (gemini.google.com).

The composer and send/stop controls were verified against the live logged-out
page. Response containers use Gemini's custom ``model-response`` elements; the
fallback selectors cover the older ``message-content`` markup.
"""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "gemini"
    display_name = "Gemini"
    home_url = "https://gemini.google.com/app"
    login_url = "https://gemini.google.com/app"

    composer_selectors = (
        "rich-textarea div.ql-editor[contenteditable='true']",
        "div.ql-editor[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
    )
    send_button_selectors = (
        "button[aria-label='Send message']",
        "button[aria-label*='Send' i]",
        "button.send-button",
    )
    stop_button_selectors = (
        "button[aria-label='Stop response']",
        "button[aria-label*='Stop' i]",
    )
    response_selectors = (
        "model-response message-content",
        "message-content.model-response-text",
        "model-response",
        ".model-response-text",
    )
    logged_in_selectors = (
        "button[aria-label*='Google Account' i]",
        "a[aria-label*='Google Account' i]",
        "img[alt*='Profile' i]",
    )
    logged_out_selectors = (
        "button:has-text('Sign in')",
        "a[href*='accounts.google.com']",
    )
    attach_button_selectors = (
        "button[aria-label*='Upload & tools' i]",
        "button[aria-label*='Upload' i]",
    )
    file_input_selectors = ("input[type=file]",)

    # Gemini itself has no password form: "Sign in" hands over to Google's
    # accounts.google.com flow, which is where prefill happens.
    login_start_selectors = (
        "a[href*='accounts.google.com']",
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
    )
    login_email_selectors = (
        "#identifierId",
        "input[type='email']",
        "input[name='identifier']",
    )
    login_email_submit_selectors = ("#identifierNext", "button:has-text('Next')")
    login_password_selectors = (
        "input[type='password']",
        "input[name='Passwd']",
    )
    login_submit_selectors = (
        "#passwordNext",
        "button:has-text('Next')",
        "button[type='submit']",
    )
    mfa_selectors = (
        "input[name='totpPin']",
        "input[name='idvPin']",
        "input[type='tel']",
        "input[autocomplete='one-time-code']",
    )
