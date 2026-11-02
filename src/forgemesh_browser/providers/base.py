"""Common browser-provider adapter.

Adapters only automate ordinary visible UI workflows (open chat, type, attach,
send, read the rendered answer). They do not bypass CAPTCHA, MFA, quotas or any
access control. When a layout changes the adapter raises AdapterError instead
of returning a wrong answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from forgemesh_browser.core.exceptions import AdapterError, NotAuthenticated, RateLimited


def same_site(url: str, home_url: str) -> bool:
    """True when ``url`` belongs to the provider's home host (or a subdomain).

    Comparing prefixes of ``home_url`` breaks for host-only homes such as
    ``https://chatgpt.com/``: the derived prefix is ``https:/`` and matches
    every https page in the tab.
    """
    try:
        current = urlparse(url)
        home = urlparse(home_url)
    except ValueError:
        return False
    current_host = (current.hostname or "").lower()
    home_host = (home.hostname or "").lower()
    if not current_host or not home_host:
        return False
    return current_host == home_host or current_host.endswith("." + home_host)

RESTRICTION_PHRASES = (
    "you've reached your limit",
    "you have reached your limit",
    "usage limit reached",
    "rate limit exceeded",
    "too many requests",
    "try again in",
    "limit will reset",
    "limit resets",
    "temporarily unavailable",
)


@dataclass(slots=True)
class LoginState:
    state: str
    detail: str
    url: str

    @property
    def logged_in(self) -> bool:
        return self.state == "logged_in"


@dataclass(slots=True)
class LoginObstacle:
    """A step the bot must not automate: CAPTCHA, MFA/OTP or a bad password."""

    #: captcha | mfa | invalid_credentials
    kind: str
    detail: str
    url: str

    @property
    def needs_human(self) -> bool:
        return self.kind in {"captcha", "mfa"}


def parse_retry_after(text: str) -> Any:
    """Parse provider-stated retry delays. Never invents a reset time."""

    from datetime import datetime, timedelta

    match = re.search(
        r"try again in\s+(\d+)\s*(second|minute|hour|day)s?", text, re.IGNORECASE
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = {
        "second": timedelta(seconds=amount),
        "minute": timedelta(minutes=amount),
        "hour": timedelta(hours=amount),
        "day": timedelta(days=amount),
    }[unit]
    return datetime.now(UTC) + delta


async def first_visible(
    page: Page, selectors: tuple[str, ...], *, timeout_ms: int = 0, poll_ms: int = 200
) -> tuple[Locator | None, str | None]:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible():
                    return locator, selector
            except PlaywrightError:
                continue
        if time.monotonic() >= deadline:
            return None, None
        await asyncio.sleep(poll_ms / 1000)


async def any_visible(page: Page, selectors: tuple[str, ...]) -> tuple[bool, str | None]:
    locator, selector = await first_visible(page, selectors, timeout_ms=0)
    return locator is not None, selector


def looks_unfinished_json(text: str) -> bool:
    """Whether a JSON answer stopped short of being a complete document.

    A site that pauses for more than ``stability_ms`` mid-answer (a long
    reasoning step, a slow token burst) looks "stable" long before it is done,
    and the collector then returned a truncated object that no parser could
    read. Only a reply that *is* JSON and does not parse is treated as
    unfinished, so ordinary prose is never held back.
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return True
    return False


_STREAM_FRAGMENT = re.compile(r"^[\s\-*•+>#|~`]*\d{0,3}[\s\-*•+>#|~`]*$")


def looks_like_stream_fragment(text: str) -> bool:
    """Whether a "stable" answer is only the start of a markdown list.

    DeepSeek renders a lone "-" (or "- 4") while the answer is still being
    generated, then pauses long enough to pass the stability window. That
    fragment is not an answer. Pure digits ("42") and any text with letters
    are accepted normally.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 8:
        return False
    if any(ch.isalpha() for ch in stripped):
        return False
    return bool(_STREAM_FRAGMENT.match(stripped)) and any(
        ch in "-*•+>#|~`" for ch in stripped
    )


class GenericChatProvider:
    provider_id: ClassVar[str] = "generic"
    display_name: ClassVar[str] = "Generic"
    home_url: ClassVar[str] = "about:blank"
    login_url: ClassVar[str | None] = None

    composer_selectors: ClassVar[tuple[str, ...]] = (
        "div[contenteditable='true']",
        "textarea",
    )
    send_button_selectors: ClassVar[tuple[str, ...]] = (
        "button[aria-label*='Send' i]",
        "button[type='submit']",
    )
    stop_button_selectors: ClassVar[tuple[str, ...]] = (
        "button[aria-label*='Stop' i]",
    )
    response_selectors: ClassVar[tuple[str, ...]] = (
        "[data-message-author-role='assistant']",
    )
    logged_in_selectors: ClassVar[tuple[str, ...]] = ()
    logged_out_selectors: ClassVar[tuple[str, ...]] = ()
    file_input_selectors: ClassVar[tuple[str, ...]] = ("input[type=file]",)
    attach_button_selectors: ClassVar[tuple[str, ...]] = ()
    send_with_enter: ClassVar[bool] = True
    response_timeout_ms: ClassVar[int] = 180_000
    stability_ms: ClassVar[int] = 2_000
    #: Extra wait allowed when a stable answer is still truncated JSON.
    unfinished_grace_ms: ClassVar[int] = 20_000
    #: Some sites swap the send control for a stop button whose icon has no
    #: accessible label, so neither control list matches while the answer
    #: streams. When the send control existed before submitting, its absence is
    #: treated as "generation may still be running" for this long before a
    #: stable answer is accepted.
    completion_grace_ms: ClassVar[int] = 5_000
    #: How long to wait for any login/composer marker on a slow SPA.
    state_wait_ms: ClassVar[int] = 5_000
    #: Third-party identity providers refuse automated browsers. These markers
    #: identify that refusal so the tool can report it instead of retrying.
    blocked_url_markers: ClassVar[tuple[str, ...]] = (
        "signin/rejected",
        "signin/challenge",
        "/signin/captcha",
    )
    blocked_text_markers: ClassVar[tuple[str, ...]] = (
        "This browser or app may not be secure",
        "Couldn't sign you in",
        "Verify it's you",
        "Security check blocked",
    )

    # ------------------------------------------------------- login form prefill
    #: Optional control that opens the email/password form (Gemini and Grok
    #: show a guest page first).
    login_start_selectors: ClassVar[tuple[str, ...]] = ()
    login_email_selectors: ClassVar[tuple[str, ...]] = (
        "input[type='email']",
        "input[autocomplete='email']",
        "input[autocomplete='username']",
        "input[name='email']",
        "input[name='username']",
        "input[name='identifier']",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
        "input[placeholder*='name@' i]",
    )
    login_password_selectors: ClassVar[tuple[str, ...]] = (
        "input[type='password']",
        "input[autocomplete='current-password']",
        "input[name='password']",
    )
    #: Tried between the email and password steps (Google uses a dedicated
    #: "Next" button). Falls back to ``login_submit_selectors``.
    login_email_submit_selectors: ClassVar[tuple[str, ...]] = ()
    login_submit_selectors: ClassVar[tuple[str, ...]] = (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
    )
    #: A field the human must fill (one-time code, PIN, device approval).
    mfa_selectors: ClassVar[tuple[str, ...]] = (
        "input[autocomplete='one-time-code']",
        "input[name*='otp' i]",
        "input[id*='otp' i]",
        "input[inputmode='numeric'][maxlength='6']",
        "input[aria-label*='verification code' i]",
    )
    mfa_text_markers: ClassVar[tuple[str, ...]] = (
        "2-step verification",
        "two-step verification",
        "verification code",
        "enter the code",
        "check your phone",
        "we sent a code",
        "confirm your identity",
        "verify it's you",
        "authenticator app",
        "approve the sign-in",
    )
    captcha_selectors: ClassVar[tuple[str, ...]] = (
        "iframe[src*='recaptcha' i]",
        "iframe[title*='captcha' i]",
        "iframe[src*='hcaptcha' i]",
        "iframe[src*='challenges.cloudflare.com' i]",
        "div.g-recaptcha",
        "div[data-sitekey]",
    )
    captcha_text_markers: ClassVar[tuple[str, ...]] = (
        "verify you are human",
        "i'm not a robot",
        "complete the captcha",
        "security check",
    )
    invalid_credential_markers: ClassVar[tuple[str, ...]] = (
        "wrong password",
        "incorrect password",
        "invalid password",
        "password is incorrect",
        "invalid email or password",
        "invalid login credentials",
        "couldn't find your google account",
        "account not found",
        "no account found",
    )
    #: How long to wait for the password step after submitting the email.
    login_step_wait_ms: ClassVar[int] = 10_000

    @property
    def entry_url(self) -> str:
        return self.login_url or self.home_url

    # ------------------------------------------------------------ session state
    async def _blocked_by_identity_provider(self, page: Page) -> bool:
        """True when the current page is a refused sign-in, not a login form."""

        url = page.url.lower()
        if any(marker in url for marker in self.blocked_url_markers):
            return True
        home_host = urlparse(self.home_url).hostname or ""
        if urlparse(url).hostname == home_host or not home_host:
            return False  # own-site pages are judged by their selectors
        for phrase in self.blocked_text_markers:
            try:
                if await page.get_by_text(phrase, exact=False).first.is_visible():
                    return True
            except PlaywrightError:
                continue
        return False

    async def detect_login_state(self, page: Page) -> LoginState:
        url = page.url
        if await self._blocked_by_identity_provider(page):
            return LoginState(
                "blocked", "identity provider refused this automated browser", url
            )
        # Give slow single-page apps a moment to render any marker at all.
        markers = (
            self.logged_out_selectors + self.logged_in_selectors + self.composer_selectors
        )
        if markers:
            await first_visible(page, markers, timeout_ms=self.state_wait_ms)
        # DeepSeek and others route "/" to their sign-in page only after the
        # bundle boots, so the URL seen on entry can be stale by now. The
        # login-URL check and the reported state must use the settled page.
        url = page.url
        if self.logged_out_selectors:
            logged_out, _ = await any_visible(page, self.logged_out_selectors)
        else:
            logged_out = False
        if logged_in_selectors := self.logged_in_selectors:
            logged_in, _ = await any_visible(page, logged_in_selectors)
        else:
            logged_in = False
        composer, _ = await first_visible(page, self.composer_selectors)
        # An explicit sign-in CTA wins over a generic composer: Gemini and
        # Perplexity show a chat box to anonymous visitors while still
        # requiring an account to actually send a prompt.
        if logged_out:
            return LoginState("logged_out", "login action visible", url)
        if composer is not None:
            return LoginState("logged_in", "chat composer visible", url)
        if logged_in:
            return LoginState("logged_in", "account indicator visible", url)
        lowered = url.lower()
        if any(
            token in lowered
            for token in ("/login", "/auth", "/signin", "/sign-in", "/sign_in")
        ):
            return LoginState("logged_out", "login URL", url)
        return LoginState("unknown", "no recognizable login or composer elements", url)

    async def require_login(self, page: Page) -> LoginState:
        state = await self.detect_login_state(page)
        if state.state == "blocked":
            raise AdapterError(
                f"{self.display_name}'s identity provider refused this automated "
                f"browser ({state.detail}: {state.url}). ForgeMesh does not bypass "
                "security checks. Either sign in with the site's own email/phone "
                "form in the visible browser window, or use the provider's official "
                "API with an API key."
            )
        if state.state == "logged_out":
            raise NotAuthenticated(
                f"{self.display_name} requires authentication. Run: "
                f"fmb account login {self.provider_id} --profile <profile>"
            )
        if state.state == "unknown":
            raise AdapterError(
                f"Could not determine {self.display_name} login state at {state.url}. "
                "The layout may have changed; check the browser window."
            )
        return state

    # ------------------------------------------------------- login form prefill
    async def _page_text(self, page: Page) -> str:
        try:
            return (await page.locator("body").inner_text(timeout=3_000)).lower()
        except PlaywrightError:
            return ""

    async def detect_login_obstacle(self, page: Page) -> LoginObstacle | None:
        """Report CAPTCHA/MFA/bad-password instead of trying to get past them.

        ForgeMesh deliberately stops here: solving a challenge, approving a push
        or guessing a password is the human's job, and the site's own rules say
        so.
        """

        url = page.url
        captcha, selector = await first_visible(page, self.captcha_selectors)
        if captcha is not None:
            return LoginObstacle("captcha", f"CAPTCHA widget visible ({selector})", url)
        text = await self._page_text(page)
        for marker in self.captcha_text_markers:
            if marker in text:
                return LoginObstacle("captcha", f"page asks: {marker!r}", url)
        code_field, selector = await first_visible(page, self.mfa_selectors)
        if code_field is not None:
            return LoginObstacle("mfa", f"one-time code field visible ({selector})", url)
        for marker in self.mfa_text_markers:
            if marker in text:
                return LoginObstacle("mfa", f"page asks: {marker!r}", url)
        for marker in self.invalid_credential_markers:
            if marker in text:
                return LoginObstacle(
                    "invalid_credentials", f"site rejected the credentials: {marker!r}", url
                )
        return None

    async def open_login_form(self, page: Page) -> str | None:
        """Click the site's sign-in control when the form is not already open."""

        if not self.login_start_selectors:
            return None
        field, _ = await first_visible(
            page, self.login_email_selectors + self.login_password_selectors
        )
        if field is not None:
            return None
        button, selector = await first_visible(
            page, self.login_start_selectors, timeout_ms=self.state_wait_ms
        )
        if button is None:
            return None
        try:
            await button.click(timeout=10_000)
        except PlaywrightError:
            return None
        await first_visible(
            page,
            self.login_email_selectors + self.login_password_selectors,
            timeout_ms=self.login_step_wait_ms,
        )
        return selector

    async def _click_first(
        self, page: Page, selectors: tuple[str, ...], *, timeout_ms: int = 0
    ) -> str | None:
        button, selector = await first_visible(page, selectors, timeout_ms=timeout_ms)
        if button is None:
            return None
        try:
            if not await button.is_enabled():
                return None
            await button.click(timeout=10_000)
        except PlaywrightError:
            return None
        return selector

    async def _fill_login_field(self, page: Page, locator: Locator, value: str) -> None:
        try:
            await locator.fill(value, timeout=10_000)
            return
        except PlaywrightError:
            pass
        try:
            await locator.click(timeout=5_000)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(value)
        except PlaywrightError as exc:
            raise AdapterError(
                f"{self.display_name}: could not fill a login field ({exc})."
            ) from exc

    async def prefill_login(
        self, page: Page, email: str, password: str
    ) -> dict[str, Any]:
        """Fill the site's own email/password form. Never submits a challenge.

        Returns the steps that were completed. Raises ``NeedsHuman`` when the
        flow needs a person (unknown email, SSO-only, OTP, CAPTCHA), so callers
        can report it instead of retrying with a different password.
        """

        from forgemesh_browser.core.exceptions import NeedsHuman

        filled: list[str] = []
        email_field, _ = await first_visible(
            page, self.login_email_selectors, timeout_ms=self.state_wait_ms
        )
        if email_field is None:
            raise NeedsHuman(
                f"{self.display_name}: no email field found at {page.url}. This "
                "provider may only offer Google/Apple/phone sign-in, or the layout "
                "changed."
            )
        await self._fill_login_field(page, email_field, email)
        filled.append("email")

        password_field, _ = await first_visible(page, self.login_password_selectors)
        if password_field is None:
            clicked = await self._click_first(
                page,
                self.login_email_submit_selectors or self.login_submit_selectors,
                timeout_ms=self.state_wait_ms,
            )
            if clicked is None:
                raise NeedsHuman(
                    f"{self.display_name}: no continue button after the email step."
                )
            filled.append("email_submitted")
            password_field, _ = await first_visible(
                page, self.login_password_selectors, timeout_ms=self.login_step_wait_ms
            )
            if password_field is None:
                raise NeedsHuman(
                    f"{self.display_name}: the password step did not appear. The "
                    "site may not recognise this email, or it wants SSO/2FA."
                )

        await self._fill_login_field(page, password_field, password)
        filled.append("password")
        submitted = await self._click_first(
            page, self.login_submit_selectors, timeout_ms=self.state_wait_ms
        )
        if submitted is None:
            try:
                await password_field.press("Enter")
                submitted = "enter"
            except PlaywrightError:
                submitted = None
        if submitted is None:
            raise AdapterError(
                f"{self.display_name}: could not submit the login form."
            )
        filled.append("submitted")
        return {
            "provider": self.provider_id,
            "url": page.url,
            "filled": filled,
            "submit_via": submitted,
        }

    # ---------------------------------------------------------------- navigation
    async def open_chat(self, page: Page) -> dict[str, Any]:
        if not same_site(page.url, self.home_url):
            await page.goto(self.home_url, wait_until="domcontentloaded")
        return {"url": page.url, "provider": self.provider_id}

    # ------------------------------------------------------------------ compose
    async def _attach_files(self, page: Page, files: list[str | Path]) -> list[str]:
        attached: list[str] = []
        for item in files:
            path = Path(item).resolve()
            file_input, _ = await first_visible(page, self.file_input_selectors)
            if file_input is not None:
                await file_input.set_input_files(str(path))
                attached.append(str(path))
                continue
            attach_button, _ = await first_visible(page, self.attach_button_selectors)
            if attach_button is not None:
                async with page.expect_file_chooser() as chooser_info:
                    await attach_button.click()
                chooser = await chooser_info.value
                await chooser.set_files(str(path))
                attached.append(str(path))
                continue
            raise AdapterError(
                f"{self.display_name}: no file input or attach button found. "
                "The upload UI may have changed."
            )
        return attached

    async def response_count(self, page: Page) -> tuple[int, str | None]:
        for selector in self.response_selectors:
            try:
                count = await page.locator(selector).count()
            except PlaywrightError:
                continue
            if count:
                return count, selector
        return 0, None

    async def submit_prompt(
        self,
        page: Page,
        prompt: str,
        *,
        files: list[str | Path] | None = None,
    ) -> dict[str, Any]:
        await self.require_login(page)
        composer, composer_selector = await first_visible(page, self.composer_selectors)
        if composer is None:
            raise AdapterError(
                f"{self.display_name}: composer not found. The site layout may have changed."
            )
        baseline, response_selector = await self.response_count(page)
        attached = await self._attach_files(page, files) if files else []

        try:
            await composer.click(timeout=10_000)
            await composer.fill(prompt)
        except PlaywrightError:
            try:
                await page.keyboard.insert_text(prompt)
            except PlaywrightError as exc:
                raise AdapterError(
                    f"{self.display_name}: could not enter the prompt ({exc})"
                ) from exc

        sent_via = None
        send_button, _ = await first_visible(page, self.send_button_selectors)
        if send_button is not None:
            try:
                if await send_button.is_enabled():
                    await send_button.click(timeout=10_000)
                    sent_via = "button"
            except PlaywrightError:
                pass
        if sent_via is None and self.send_with_enter:
            await composer.press("Enter")
            sent_via = "enter"
        if sent_via is None:
            raise AdapterError(
                f"{self.display_name}: no enabled send control found. The prompt was not submitted."
            )
        return {
            "submitted": True,
            "sent_via": sent_via,
            "composer_selector": composer_selector,
            "response_baseline": baseline,
            "response_selector": response_selector,
            "send_control": send_button is not None,
            "attached_files": attached,
        }

    # ---------------------------------------------------------------- responses
    async def detect_restriction(self, page: Page) -> RateLimited | None:
        try:
            body = await page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError:
            return None
        lowered = body.lower()
        for phrase in RESTRICTION_PHRASES:
            index = lowered.find(phrase)
            if index >= 0:
                snippet = body[max(0, index - 80) : index + 160].strip()
                return RateLimited(
                    f"{self.display_name}: {snippet}",
                    retry_after=parse_retry_after(snippet),
                    details={"provider": self.provider_id, "message": snippet},
                )
        return None

    async def collect_response(
        self,
        page: Page,
        *,
        baseline: int = 0,
        timeout_ms: int | None = None,
        idle_expected: bool = False,
    ) -> dict[str, Any]:
        timeout = (timeout_ms or self.response_timeout_ms) / 1000
        deadline = time.monotonic() + timeout
        locator: Locator | None = None
        selector: str | None = None

        while time.monotonic() < deadline:
            for candidate in self.response_selectors:
                try:
                    count = await page.locator(candidate).count()
                except PlaywrightError:
                    count = 0
                if count > baseline:
                    locator = page.locator(candidate)
                    selector = candidate
                    break
            if locator is not None:
                break
            await asyncio.sleep(0.4)

        if locator is None:
            raise AdapterError(
                f"{self.display_name}: no assistant response appeared within {timeout:.0f}s. "
                "The site layout may have changed, or the account may be limited."
            )

        last_text = ""
        stable_since = time.monotonic()
        unfinished_since: float | None = None
        silent_since: float | None = None
        stop_seen = False
        while time.monotonic() < deadline:
            try:
                messages = await locator.all()
                if not messages:
                    await asyncio.sleep(0.3)
                    continue
                text = (await messages[-1].inner_text()).strip()
            except PlaywrightError:
                await asyncio.sleep(0.3)
                continue
            streaming, _ = await any_visible(page, self.stop_button_selectors)
            idle, _ = await any_visible(page, self.send_button_selectors)
            stop_seen = stop_seen or streaming
            now = time.monotonic()
            if text != last_text or streaming:
                last_text = text
                stable_since = now
                unfinished_since = None
                silent_since = None
            elif text and (now - stable_since) * 1000 >= self.stability_ms:
                if looks_like_stream_fragment(text):
                    # The site is still between token bursts; a list marker is
                    # not the answer, so keep waiting instead of returning it.
                    await asyncio.sleep(0.3)
                    continue
                if looks_unfinished_json(text):
                    unfinished_since = unfinished_since or now
                    waited_ms = (now - unfinished_since) * 1000
                    if waited_ms < self.unfinished_grace_ms:
                        # The site is between token bursts, not finished.
                        await asyncio.sleep(0.3)
                        continue
                if idle_expected and not idle and not stop_seen and not streaming:
                    # The send control was visible before submitting and has
                    # not come back, while no labelled stop control was ever
                    # seen: the site may be streaming behind an unlabelled
                    # icon. Hold the stable text back for a bounded window.
                    silent_since = silent_since or now
                    if (now - silent_since) * 1000 < self.completion_grace_ms:
                        await asyncio.sleep(0.3)
                        continue
                images: list[str] = []
                try:
                    images = await messages[-1].evaluate(
                        "el => Array.from(el.querySelectorAll('img'))"
                        ".map(img => img.currentSrc || img.src).filter(Boolean)"
                    )
                except PlaywrightError:
                    pass
                return {
                    "text": text,
                    "images": images,
                    "selector": selector,
                    "url": page.url,
                    "chars": len(text),
                }
            await asyncio.sleep(0.3)

        if last_text and looks_like_stream_fragment(last_text):
            raise AdapterError(
                f"{self.display_name}: only a UI fragment ({last_text!r}) appeared "
                f"within {timeout:.0f}s. The answer never rendered."
            )
        raise AdapterError(
            f"{self.display_name}: response did not stabilise within {timeout:.0f}s"
            + (f"; partial text captured ({len(last_text)} chars)" if last_text else "")
        )

    async def capture_response(self, page: Page, output: Path) -> dict[str, Any]:
        locator: Locator | None = None
        for selector in self.response_selectors:
            try:
                count = await page.locator(selector).count()
            except PlaywrightError:
                continue
            if count:
                locator = page.locator(selector).last
                break
        if locator is None:
            raise AdapterError(
                f"{self.display_name}: no response element to capture"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        await locator.screenshot(path=str(output))
        output.chmod(0o600)
        return {"path": str(output), "provider": self.provider_id, "url": page.url}

    async def close_session(self, page: Page) -> None:
        """Close the chat tab. Browser storage and cookies are preserved."""

        await page.close()
