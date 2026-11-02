"""Browser provider adapters: registry, config and restriction parsing."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from forgemesh_browser.core.exceptions import AdapterError, NotAuthenticated
from forgemesh_browser.providers.base import first_visible, parse_retry_after, same_site
from forgemesh_browser.providers.chatgpt.adapter import Adapter as ChatGPT
from forgemesh_browser.providers.deepseek.adapter import Adapter as DeepSeek
from forgemesh_browser.providers.gemini.adapter import Adapter as Gemini
from forgemesh_browser.providers.registry import ProviderRegistry
from forgemesh_browser.providers.sarvam.adapter import Adapter as Sarvam
from forgemesh_browser.settings import Settings

EXPECTED = {
    "chatgpt",
    "claude",
    "kimi",
    "deepseek",
    "grok",
    "perplexity",
    "gemini",
    "sarvam",
}


class FakeLocator:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    @property
    def first(self) -> FakeLocator:
        return self

    async def is_visible(self) -> bool:
        return self.visible


class FakePage:
    def __init__(self, url: str, visible: set[str] | None = None) -> None:
        self.url = url
        self.visible = visible or set()

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector in self.visible)

    def get_by_text(self, text: str, *, exact: bool = True) -> FakeLocator:
        return FakeLocator(text in self.visible)


async def test_login_state_detection() -> None:
    adapter = ChatGPT()
    adapter.state_wait_ms = 0

    composer = FakePage("https://chatgpt.com/", {adapter.composer_selectors[0]})
    state = await adapter.detect_login_state(composer)
    assert state.state == "logged_in"
    assert state.logged_in is True

    login = FakePage("https://chatgpt.com/auth/login")
    assert (await adapter.detect_login_state(login)).state == "logged_out"
    with pytest.raises(NotAuthenticated):
        await adapter.require_login(login)

    logout_button = FakePage("https://chatgpt.com/", {adapter.logged_out_selectors[0]})
    assert (await adapter.detect_login_state(logout_button)).state == "logged_out"

    unknown = FakePage("https://chatgpt.com/")
    assert (await adapter.detect_login_state(unknown)).state == "unknown"
    with pytest.raises(AdapterError):
        await adapter.require_login(unknown)


async def test_chatgpt_controls_of_the_current_ui_are_found() -> None:
    """The rollout without #prompt-textarea and data-message-author-role must work."""
    adapter = ChatGPT()
    page = FakePage(
        "https://chatgpt.com/",
        {"[contenteditable='true'][role='textbox']", "button[aria-label='Send']"},
    )
    _composer, composer_selector = await first_visible(page, adapter.composer_selectors)
    assert composer_selector == "[contenteditable='true'][role='textbox']"
    _send, send_selector = await first_visible(page, adapter.send_button_selectors)
    assert send_selector == "button[aria-label='Send']"
    assert "[data-markdown-text-style='assistant-message']" in adapter.response_selectors


async def test_identity_provider_refusal_is_reported_not_bypassed() -> None:
    """Google's "this browser may not be secure" page must surface, not retry."""

    adapter = DeepSeek()
    adapter.state_wait_ms = 0

    rejected = FakePage("https://accounts.google.com/v3/signin/rejected?client_id=1")
    state = await adapter.detect_login_state(rejected)
    assert state.state == "blocked"
    assert state.logged_in is False
    with pytest.raises(AdapterError, match="does not bypass"):
        await adapter.require_login(rejected)

    challenged = FakePage(
        "https://accounts.google.com/signin", {"Couldn't sign you in"}
    )
    assert (await adapter.detect_login_state(challenged)).state == "blocked"

    # The provider's own sign-in form is still an ordinary logged-out page.
    form = FakePage("https://chat.deepseek.com/sign_in")
    assert (await adapter.detect_login_state(form)).state == "logged_out"


async def test_deepseek_sign_in_form_is_logged_out_without_url_help() -> None:
    """Live DOM (2026-11-02) uses role=button divs, not <button> or <a>."""

    adapter = DeepSeek()
    adapter.state_wait_ms = 0

    landing = FakePage("https://chat.deepseek.com/", {"input[type='password']"})
    state = await adapter.detect_login_state(landing)
    assert state.state == "logged_out"
    with pytest.raises(NotAuthenticated):
        await adapter.require_login(landing)

    cta = FakePage("https://chat.deepseek.com/", {"div[role='button']:has-text('Log in')"})
    assert (await adapter.detect_login_state(cta)).state == "logged_out"

    chat = FakePage("https://chat.deepseek.com/a/chat/s/1", {adapter.composer_selectors[0]})
    assert (await adapter.detect_login_state(chat)).state == "logged_in"


class RedirectingPage(FakePage):
    """An SPA that boots at "/" and only routes to /sign_in once hydrated."""

    def __init__(self, start: str, settled: str, visible: set[str] | None = None) -> None:
        super().__init__(start, visible)
        self.settled = settled

    def locator(self, selector: str) -> FakeLocator:
        self.url = self.settled
        return super().locator(selector)


async def test_login_state_uses_the_settled_url() -> None:
    """The pre-redirect URL must not be reported, nor hide the login path."""

    adapter = DeepSeek()
    adapter.state_wait_ms = 0
    page = RedirectingPage(
        "https://chat.deepseek.com/", "https://chat.deepseek.com/sign_in"
    )
    state = await adapter.detect_login_state(page)
    assert state.url == "https://chat.deepseek.com/sign_in"
    assert state.state == "logged_out"


async def test_gemini_and_sarvam_login_detection() -> None:
    gemini = Gemini()
    gemini.state_wait_ms = 0
    composer = FakePage("https://gemini.google.com/app", {gemini.composer_selectors[0]})
    assert (await gemini.detect_login_state(composer)).state == "logged_in"
    signed_out = FakePage("https://gemini.google.com/app", {gemini.logged_out_selectors[0]})
    assert (await gemini.detect_login_state(signed_out)).state == "logged_out"

    # Google may still refuse the browser on the accounts.google.com hop.
    blocked = FakePage(
        "https://accounts.google.com/v3/signin/rejected",
        {"This browser or app may not be secure"},
    )
    assert (await gemini.detect_login_state(blocked)).state == "blocked"

    sarvam = Sarvam()
    sarvam.state_wait_ms = 0
    sarvam_in = FakePage("https://indus.sarvam.ai/", {sarvam.composer_selectors[1]})
    assert (await sarvam.detect_login_state(sarvam_in)).state == "logged_in"
    sarvam_out = FakePage(
        "https://indus.sarvam.ai/", {sarvam.logged_out_selectors[0]}
    )
    assert (await sarvam.detect_login_state(sarvam_out)).state == "logged_out"

    # A guest composer must not override an explicit sign-in call to action.
    guest = FakePage(
        "https://gemini.google.com/app",
        {gemini.composer_selectors[0], gemini.logged_out_selectors[0]},
    )
    assert (await gemini.detect_login_state(guest)).state == "logged_out"


def test_builtin_providers(settings: Settings) -> None:
    registry = ProviderRegistry(settings)
    assert set(registry.ids()) == EXPECTED
    infos = {info.id: info for info in registry.list()}
    assert len(infos) == 8
    for provider_id, info in infos.items():
        assert info.enabled is True
        assert info.url.startswith("https://")
        adapter = registry.get(provider_id)
        assert adapter.provider_id == provider_id
        assert adapter.entry_url.startswith("https://")


def test_unknown_provider(settings: Settings) -> None:
    registry = ProviderRegistry(settings)
    with pytest.raises(AdapterError):
        registry.get("unknown")


def test_disabled_provider_from_user_config(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    config: Path = settings.config_dir / "providers.yaml"
    config.write_text(
        yaml.safe_dump({"providers": {"claude": {"enabled": False, "name": "Claude (off)"}}}),
        encoding="utf-8",
    )
    registry = ProviderRegistry(settings)
    infos = {info.id: info for info in registry.list()}
    assert infos["claude"].enabled is False
    assert infos["claude"].name == "Claude (off)"
    with pytest.raises(AdapterError):
        registry.get("claude")


def test_parse_retry_after_only_when_stated() -> None:
    assert parse_retry_after("no timing information here") is None
    parsed = parse_retry_after("Please try again in 5 minutes.")
    assert isinstance(parsed, datetime)
    assert parsed > datetime.now(UTC)


def test_looks_unfinished_json_only_flags_broken_json() -> None:
    from forgemesh_browser.providers.base import looks_unfinished_json

    # A pause between token bursts used to hand back a cut-off object.
    assert looks_unfinished_json('{"statements": [{"title": "half a sent') is True
    assert looks_unfinished_json('{"statements": []}') is False
    assert looks_unfinished_json("plain prose answer") is False
    assert looks_unfinished_json("```json\n{\"a\": 1}\n```") is False


def test_a_lone_list_marker_is_not_an_answer() -> None:
    from forgemesh_browser.providers.base import looks_like_stream_fragment

    # DeepSeek renders the start of a markdown list before the real answer.
    assert looks_like_stream_fragment("-") is True
    assert looks_like_stream_fragment("- 4") is True
    assert looks_like_stream_fragment("  * 12 ") is True
    assert looks_like_stream_fragment("|") is True
    # Real content is never held back.
    assert looks_like_stream_fragment("42") is False
    assert looks_like_stream_fragment('{"statements": []}') is False
    assert looks_like_stream_fragment("Vidyarcly is available") is False


class _StreamingLocator:
    """A response element that first renders "-" and later the real answer."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.reads = 0
        self.visible = False

    @property
    def first(self) -> _StreamingLocator:
        return self

    async def count(self) -> int:
        return 1

    async def all(self) -> list[_StreamingLocator]:
        return [self]

    async def inner_text(self) -> str:
        text = self.texts[min(self.reads, len(self.texts) - 1)]
        self.reads += 1
        return text

    async def is_visible(self) -> bool:
        return self.visible

    async def evaluate(self, script: str) -> list[str]:
        return []


class _StreamingPage:
    url = "https://chat.deepseek.com/a/chat/s/test"

    def __init__(self, response: _StreamingLocator) -> None:
        self.response = response

    def locator(self, selector: str) -> _StreamingLocator:
        if "markdown" in selector:
            return self.response
        return _StreamingLocator([])


async def test_collect_response_waits_past_a_markdown_fragment() -> None:
    """A stable "-" used to be returned as the DeepSeek critique answer."""
    from forgemesh_browser.core.exceptions import AdapterError

    adapter = DeepSeek()
    adapter.stability_ms = 0
    page = _StreamingPage(
        _StreamingLocator(["-", "-", "- 4", '{"assessments": []}'])
    )
    result = await adapter.collect_response(page, timeout_ms=3_000)
    assert result["text"] == '{"assessments": []}'

    only_fragments = DeepSeek()
    only_fragments.stability_ms = 0
    stuck = _StreamingPage(_StreamingLocator(["-", "-"]))
    with pytest.raises(AdapterError, match="only a UI fragment"):
        await only_fragments.collect_response(stuck, timeout_ms=400)


class _TimedLocator:
    """A response element whose text changes on a wall-clock timeline."""

    def __init__(self, timeline: list[tuple[float, str]]) -> None:
        self.timeline = timeline
        self.started = time.monotonic()

    @property
    def first(self) -> _TimedLocator:
        return self

    async def count(self) -> int:
        return 1

    async def all(self) -> list[_TimedLocator]:
        return [self]

    async def inner_text(self) -> str:
        elapsed = time.monotonic() - self.started
        text = self.timeline[0][1]
        for at, value in self.timeline:
            if elapsed >= at:
                text = value
        return text

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, script: str) -> list[str]:
        return []


class _TimedPage:
    """A page whose send control only returns after a delay (or never)."""

    url = "https://chat.deepseek.com/a/chat/s/test"

    def __init__(
        self,
        response: _TimedLocator,
        adapter: DeepSeek,
        *,
        send_after: float | None,
    ) -> None:
        self.response = response
        self.send_selectors = adapter.send_button_selectors
        self.send_after = send_after
        self.started = time.monotonic()

    def locator(self, selector: str) -> FakeLocator | _TimedLocator:
        if "markdown" in selector:
            return self.response
        if selector in self.send_selectors:
            visible = (
                self.send_after is not None
                and time.monotonic() - self.started >= self.send_after
            )
            return FakeLocator(visible)
        return FakeLocator(False)


async def test_collect_response_holds_back_text_until_the_idle_control_returns() -> None:
    """An unlabelled stop icon must not make a mid-stream pause look final."""

    adapter = DeepSeek()
    adapter.stability_ms = 0
    adapter.completion_grace_ms = 3_000
    page = _TimedPage(
        _TimedLocator(
            [(0.0, "Half an answer"), (0.7, "Half an answer with the rest of it.")]
        ),
        adapter,
        send_after=0.7,
    )
    started = time.monotonic()
    result = await adapter.collect_response(page, timeout_ms=5_000, idle_expected=True)
    elapsed = time.monotonic() - started
    assert result["text"] == "Half an answer with the rest of it."
    assert elapsed >= 0.5


async def test_collect_response_caps_the_wait_for_a_missing_idle_control() -> None:
    """If the send control never comes back the reply is still delivered."""

    adapter = DeepSeek()
    adapter.stability_ms = 0
    adapter.completion_grace_ms = 400
    page = _TimedPage(
        _TimedLocator([(0.0, "A complete answer.")]), adapter, send_after=None
    )
    started = time.monotonic()
    result = await adapter.collect_response(page, timeout_ms=5_000, idle_expected=True)
    elapsed = time.monotonic() - started
    assert result["text"] == "A complete answer."
    assert 0.3 <= elapsed < 2.5


async def test_collect_response_accepts_immediately_when_the_send_control_is_idle() -> None:
    """A visible send control is a positive completion signal; no extra wait."""

    adapter = DeepSeek()
    adapter.stability_ms = 0
    adapter.completion_grace_ms = 3_000
    page = _TimedPage(
        _TimedLocator([(0.0, "Answer.")]), adapter, send_after=0.0
    )
    started = time.monotonic()
    result = await adapter.collect_response(page, timeout_ms=5_000, idle_expected=True)
    elapsed = time.monotonic() - started
    assert result["text"] == "Answer."
    assert elapsed < 1.5


@pytest.mark.parametrize(
    ("url", "home", "expected"),
    [
        ("https://chatgpt.com/c/123", "https://chatgpt.com/", True),
        ("https://chatgpt.com", "https://chatgpt.com/", True),
        ("https://chatgpt.com.evil.test/", "https://chatgpt.com/", False),
        ("https://usercontent.example/", "https://chatgpt.com/", False),
        ("https://chat.deepseek.com/a/chat", "https://chat.deepseek.com/", True),
        ("https://deepseek.com/", "https://chat.deepseek.com/", False),
        ("about:blank", "https://chatgpt.com/", False),
        ("", "https://chatgpt.com/", False),
    ],
)
def test_same_site_matches_host_or_subdomain_only(
    url: str, home: str, expected: bool
) -> None:
    assert same_site(url, home) is expected

