"""Prefill login: fills the site's own form, stops at CAPTCHA/OTP/rejection."""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

from forgemesh_browser.core.exceptions import AdapterError, NeedsHuman
from forgemesh_browser.providers.base import GenericChatProvider, LoginObstacle

EMAIL = "input[type='email']"
PASSWORD = "input[type='password']"
SUBMIT = "button[type='submit']"
NEXT = "#identifierNext"
START = "button:has-text('Sign in')"
OTP = "input[autocomplete='one-time-code']"
CAPTCHA = "iframe[src*='recaptcha' i]"


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def insert_text(self, text: str) -> None:
        self.page.inserted.append(text)

    async def press(self, key: str) -> None:
        self.page.pressed.append(key)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def is_visible(self) -> bool:
        return self.selector in self.page.visible

    async def is_enabled(self) -> bool:
        return True

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if self.selector not in self.page.visible:
            raise PlaywrightError("not visible")
        self.page.filled[self.selector] = value

    async def click(self, timeout: int | None = None) -> None:
        self.page.clicks.append(self.selector)
        self.page.visible |= self.page.reveal.get(self.selector, set())
        self.page.visible -= self.page.hide.get(self.selector, set())

    async def press(self, key: str, timeout: int | None = None) -> None:
        if self.page.fail_press:
            raise PlaywrightError("keyboard unavailable")
        self.page.pressed.append(key)

    async def inner_text(self, timeout: int | None = None) -> str:
        return self.page.text

    async def count(self) -> int:
        return 1 if self.selector in self.page.visible else 0


class FakePage:
    def __init__(
        self,
        url: str = "https://example.test/login",
        *,
        visible: set[str] | None = None,
        reveal: dict[str, set[str]] | None = None,
        hide: dict[str, set[str]] | None = None,
        text: str = "",
        fail_press: bool = False,
    ) -> None:
        self.url = url
        self.visible = visible or set()
        self.reveal = reveal or {}
        self.hide = hide or {}
        self.text = text
        self.fail_press = fail_press
        self.filled: dict[str, str] = {}
        self.clicks: list[str] = []
        self.pressed: list[str] = []
        self.inserted: list[str] = []
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"text={text}")


class PrefillProvider(GenericChatProvider):
    provider_id = "test"
    display_name = "Test Site"
    home_url = "https://example.test/"
    login_email_selectors = (EMAIL,)
    login_password_selectors = (PASSWORD,)
    login_submit_selectors = (SUBMIT,)


def provider(**overrides) -> PrefillProvider:
    adapter = PrefillProvider()
    adapter.state_wait_ms = 0
    adapter.login_step_wait_ms = 0
    for key, value in overrides.items():
        setattr(adapter, key, value)
    return adapter


async def test_prefill_single_step_form() -> None:
    page = FakePage(visible={EMAIL, PASSWORD, SUBMIT})
    result = await provider().prefill_login(page, "user@example.com", "pw")

    assert page.filled == {EMAIL: "user@example.com", PASSWORD: "pw"}
    assert page.clicks == [SUBMIT]
    assert result["filled"] == ["email", "password", "submitted"]
    assert result["submit_via"] == SUBMIT


async def test_prefill_two_step_form() -> None:
    page = FakePage(
        visible={EMAIL, NEXT},
        reveal={NEXT: {PASSWORD, SUBMIT}},
    )
    result = await provider(
        login_email_submit_selectors=(NEXT,)
    ).prefill_login(page, "user@example.com", "pw")

    assert page.filled == {EMAIL: "user@example.com", PASSWORD: "pw"}
    assert page.clicks == [NEXT, SUBMIT]
    assert result["filled"] == ["email", "email_submitted", "password", "submitted"]


async def test_prefill_without_email_field_needs_human() -> None:
    page = FakePage(visible={SUBMIT})
    with pytest.raises(NeedsHuman) as excinfo:
        await provider().prefill_login(page, "user@example.com", "pw")
    assert "no email field" in excinfo.value.message


async def test_prefill_password_step_missing_needs_human() -> None:
    page = FakePage(visible={EMAIL, NEXT}, reveal={NEXT: {SUBMIT}})
    with pytest.raises(NeedsHuman) as excinfo:
        await provider(login_email_submit_selectors=(NEXT,)).prefill_login(
            page, "user@example.com", "pw"
        )
    assert "password step did not appear" in excinfo.value.message


async def test_prefill_without_submit_control_fails_cleanly() -> None:
    page = FakePage(visible={EMAIL, PASSWORD}, fail_press=True)
    with pytest.raises(AdapterError) as excinfo:
        await provider().prefill_login(page, "user@example.com", "pw")
    assert "could not submit" in excinfo.value.message


async def test_prefill_never_returns_the_password() -> None:
    page = FakePage(visible={EMAIL, PASSWORD, SUBMIT})
    result = await provider().prefill_login(page, "user@example.com", "hunter2")
    assert "hunter2" not in str(result)


async def test_open_login_form_clicks_start_control() -> None:
    page = FakePage(visible={START}, reveal={START: {EMAIL, PASSWORD}})
    adapter = provider(login_start_selectors=(START,))

    selector = await adapter.open_login_form(page)
    assert selector == START
    assert page.clicks == [START]
    assert EMAIL in page.visible


async def test_open_login_form_is_noop_when_form_visible() -> None:
    page = FakePage(visible={EMAIL})
    adapter = provider(login_start_selectors=(START,))
    assert await adapter.open_login_form(page) is None
    assert page.clicks == []


async def test_detect_mfa_obstacle() -> None:
    page = FakePage(visible={OTP})
    obstacle = await provider().detect_login_obstacle(page)
    assert isinstance(obstacle, LoginObstacle)
    assert obstacle.kind == "mfa"
    assert obstacle.needs_human is True


async def test_detect_mfa_from_page_text() -> None:
    page = FakePage(text="Enter the verification code we sent to your phone")
    obstacle = await provider().detect_login_obstacle(page)
    assert obstacle is not None
    assert obstacle.kind == "mfa"


async def test_detect_captcha_obstacle() -> None:
    page = FakePage(visible={CAPTCHA})
    obstacle = await provider().detect_login_obstacle(page)
    assert obstacle is not None
    assert obstacle.kind == "captcha"
    assert obstacle.needs_human is True


async def test_detect_rejected_credentials() -> None:
    page = FakePage(text="Incorrect password. Try again.")
    obstacle = await provider().detect_login_obstacle(page)
    assert obstacle is not None
    assert obstacle.kind == "invalid_credentials"
    assert obstacle.needs_human is False


async def test_clean_page_has_no_obstacle() -> None:
    page = FakePage(visible={EMAIL, PASSWORD, SUBMIT}, text="Welcome back")
    assert await provider().detect_login_obstacle(page) is None
