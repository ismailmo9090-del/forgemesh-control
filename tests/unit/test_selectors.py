"""Target resolution priority and URL normalisation (no browser needed)."""

from __future__ import annotations

import pytest

from forgemesh_browser.automation.selectors import _candidates, resolve
from forgemesh_browser.core.browser_manager import normalize_url
from forgemesh_browser.core.exceptions import (
    AmbiguousTarget,
    ElementNotFound,
    NavigationError,
)


class FakeLocator:
    def __init__(self, page: FakePage, key: tuple) -> None:
        self.page = page
        self.key = key

    async def count(self) -> int:
        return self.page.counts.get(self.key, 0)


class FakePage:
    def __init__(self, counts: dict[tuple, int] | None = None) -> None:
        self.counts = counts or {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, ("locator", selector))

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> FakeLocator:
        return FakeLocator(self, ("role", role, name, exact))

    def get_by_label(self, value: str) -> FakeLocator:
        return FakeLocator(self, ("label", value))

    def get_by_placeholder(self, value: str) -> FakeLocator:
        return FakeLocator(self, ("placeholder", value))

    def get_by_text(self, value: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, ("text", value, exact))

    def get_by_test_id(self, value: str) -> FakeLocator:
        return FakeLocator(self, ("testid", value))


@pytest.mark.parametrize(
    ("target", "expected_strategy"),
    [
        ("ref=e12", "ref=e12"),
        ("css=#email", "css=#email"),
        ("#email", "css=#email"),
        ("//button", "xpath=//button"),
        ('role=button[name="Sign in"]', 'role=button[name="Sign in"]'),
        ("label=Email", "label=Email"),
        ("placeholder=Search", "placeholder=Search"),
        ("testid=submit", "testid=submit"),
        ("Sign in", "role button name='Sign in'"),
    ],
)
def test_candidate_priority(target: str, expected_strategy: str) -> None:
    candidates = _candidates(FakePage(), target)
    assert candidates[0][1] == expected_strategy


async def test_resolve_ref() -> None:
    page = FakePage({("locator", '[data-fmb-ref="e7"]'): 1})
    resolved = await resolve(page, "ref=e7", timeout_ms=0)
    assert resolved.strategy == "ref=e7"


async def test_resolve_role() -> None:
    page = FakePage({("role", "button", "Sign in", True): 1})
    resolved = await resolve(page, 'role=button[name="Sign in"]', timeout_ms=0)
    assert resolved.strategy == 'role=button[name="Sign in"]'


async def test_resolve_label_and_css() -> None:
    page = FakePage({("label", "Email address"): 1})
    assert (await resolve(page, "label=Email address", timeout_ms=0)).strategy == "label=Email address"

    page = FakePage({("locator", "#email"): 1})
    assert (await resolve(page, "#email", timeout_ms=0)).strategy == "css=#email"


async def test_resolve_plain_text_falls_back_loudly() -> None:
    page = FakePage({("text", "Sign in", True): 1})
    resolved = await resolve(page, "Sign in", timeout_ms=0)
    assert resolved.strategy == "text=Sign in (exact)"


async def test_resolve_ambiguous_fails() -> None:
    page = FakePage(
        {
            ("role", "button", "Save", True): 2,
            ("role", "button", "Save", False): 2,
        }
    )
    with pytest.raises(AmbiguousTarget):
        await resolve(page, "Save", timeout_ms=0)


async def test_resolve_missing_fails() -> None:
    with pytest.raises(ElementNotFound):
        await resolve(FakePage(), "#missing", timeout_ms=0)


async def test_resolve_empty_target() -> None:
    with pytest.raises(ElementNotFound):
        await resolve(FakePage(), "   ", timeout_ms=0)


def test_normalize_url() -> None:
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("localhost:3000/app") == "http://localhost:3000/app"
    assert normalize_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert normalize_url("  https://example.com/x  ") == "https://example.com/x"


@pytest.mark.parametrize("value", ["", "file:///etc/passwd", "ftp://host/file", "https://"])
def test_normalize_url_rejects(value: str) -> None:
    with pytest.raises(NavigationError):
        normalize_url(value)
