"""Target resolution with a strict priority order.

Supported targets:
  ref=e12                       element reference from `fmb inspect`
  role=button[name="Sign in"]   accessible role + name
  label=Email / placeholder=... label / placeholder
  testid=submit                 data-testid
  css=#email / #email / .btn    CSS selector
  xpath=//button                XPath
  text=Sign in / Sign in        visible text (button, link, textbox tried first)

Resolution fails loudly when nothing matches or when more than one element
matches — it never silently clicks a different element.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from playwright.async_api import Locator, Page

from forgemesh_browser.core.exceptions import AmbiguousTarget, ElementNotFound

_ROLE_RE = re.compile(r"^role=([a-zA-Z]+)(?:\[name=(?:\"([^\"]*)\"|'([^']*)'|([^\]]+))\])?$")
_CSS_HINT = re.compile(r"^[#.\[]|^[a-zA-Z][a-zA-Z0-9_-]*\s*[>+~]\s*")


@dataclass(slots=True)
class ResolvedTarget:
    locator: Locator
    strategy: str
    description: str


def _candidates(page: Page, target: str) -> list[tuple[Locator, str]]:
    raw = target.strip()
    if not raw:
        raise ElementNotFound("Element target cannot be empty")

    lower = raw.lower()
    if lower.startswith("ref="):
        ref = raw.split("=", 1)[1].strip().strip('"').strip("'")
        return [(page.locator(f'[data-fmb-ref="{ref}"]'), f"ref={ref}")]
    if lower.startswith("css="):
        return [(page.locator(raw.split("=", 1)[1]), raw)]
    if lower.startswith("xpath="):
        return [(page.locator(raw), raw)]
    if lower.startswith("text="):
        value = raw.split("=", 1)[1]
        return [
            (page.get_by_role("button", name=value), f"role button name={value!r}"),
            (page.get_by_role("link", name=value), f"role link name={value!r}"),
            (page.get_by_text(value), f"text={value}"),
        ]
    if lower.startswith("label="):
        value = raw.split("=", 1)[1]
        return [(page.get_by_label(value), raw)]
    if lower.startswith("placeholder="):
        value = raw.split("=", 1)[1]
        return [(page.get_by_placeholder(value), raw)]
    if lower.startswith("testid="):
        value = raw.split("=", 1)[1]
        return [(page.get_by_test_id(value), raw)]
    role_match = _ROLE_RE.match(raw)
    if role_match:
        role = role_match.group(1)
        name = role_match.group(2) or role_match.group(3) or role_match.group(4)
        if name:
            return [
                (page.get_by_role(role, name=name), raw),
                (page.get_by_role(role, name=name, exact=False), raw + " (loose)"),
            ]
        return [(page.get_by_role(role), raw)]
    if raw.startswith("//"):
        return [(page.locator(f"xpath={raw}"), f"xpath={raw}")]
    if _CSS_HINT.match(raw):
        return [(page.locator(raw), f"css={raw}")]

    # Plain text: prefer accessible roles and labels before raw text.
    return [
        (page.get_by_role("button", name=raw), f"role button name={raw!r}"),
        (page.get_by_role("link", name=raw), f"role link name={raw!r}"),
        (page.get_by_role("textbox", name=raw), f"role textbox name={raw!r}"),
        (page.get_by_role("tab", name=raw), f"role tab name={raw!r}"),
        (page.get_by_role("menuitem", name=raw), f"role menuitem name={raw!r}"),
        (page.get_by_label(raw), f"label={raw}"),
        (page.get_by_placeholder(raw), f"placeholder={raw}"),
        (page.get_by_text(raw, exact=True), f"text={raw} (exact)"),
        (page.get_by_text(raw), f"text={raw}"),
    ]


async def resolve(
    page: Page,
    target: str,
    *,
    timeout_ms: int = 15_000,
    require_single: bool = True,
) -> ResolvedTarget:
    deadline = time.monotonic() + timeout_ms / 1000
    candidates = _candidates(page, target)
    ambiguous: list[str] = []

    while True:
        for locator, strategy in candidates:
            try:
                count = await locator.count()
            except Exception:  # page navigating
                count = 0
            if count == 1:
                return ResolvedTarget(locator=locator, strategy=strategy, description=target)
            if count > 1 and require_single:
                ambiguous.append(f"{strategy} ({count} matches)")
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.1)

    if ambiguous:
        raise AmbiguousTarget(
            f"Target {target!r} is ambiguous: {', '.join(ambiguous)}. "
            "Use a ref=, a more specific selector, or css=... >> nth=0."
        )
    raise ElementNotFound(
        f"No element matched {target!r}. Run `fmb inspect` to list stable element refs."
    )


async def resolve_optional(
    page: Page, targets: list[str], *, timeout_ms: int = 0
) -> ResolvedTarget | None:
    for target in targets:
        try:
            return await resolve(page, target, timeout_ms=timeout_ms)
        except (ElementNotFound, AmbiguousTarget):
            continue
    return None
