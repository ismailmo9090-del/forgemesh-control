# Providers and Adapters

ForgeMesh has two distinct provider families, and the word "provider" means a
different thing in each:

| Family | Package | What it is | Configured in |
| --- | --- | --- | --- |
| **Browser providers** | `forgemesh_browser/providers/` | Web chat sites you automate with a logged-in profile (ChatGPT, Claude, Kimi, DeepSeek, Grok, Perplexity, Gemini, Sarvam Indus) | `config/providers.yaml` + `providers/registry.py` |
| **AI providers** | `forgemesh_browser/ai/` | Text/vision model backends used for page understanding (Ollama, OpenAI-compatible) | `settings.yaml` / `FMB_*`, registered in `ai/router.py` |

Both are adapter-pattern based: adding one is a new module plus one registration
line, with no changes to the CLI surface, the API or the browser layer.

---

## Part 1 — Adding a browser provider adapter

A provider adapter automates only the site's ordinary visible UI: open the chat,
type into the composer, attach a file, press send, read the rendered reply. It
never bypasses login, CAPTCHA or MFA, never rotates accounts, and raises
`AdapterError` when the layout changes instead of guessing.

### 1. Create the adapter

```
src/forgemesh_browser/providers/<provider_id>/
├── __init__.py
└── adapter.py
```

```python
"""Example provider adapter. Re-verify selectors after any UI change."""

from __future__ import annotations

from forgemesh_browser.providers.base import GenericChatProvider


class Adapter(GenericChatProvider):
    provider_id = "example"
    display_name = "Example AI"
    home_url = "https://chat.example.com/"
    login_url = "https://chat.example.com/login"

    composer_selectors = (
        "div[contenteditable='true']",
        "textarea[name='prompt']",
    )
    send_button_selectors = (
        "button[aria-label='Send']",
        "button[data-testid='send']",
    )
    stop_button_selectors = ("button[aria-label='Stop generating']",)
    response_selectors = ("[data-role='assistant']",)
    logged_in_selectors = ("[data-testid='account-menu']",)
    logged_out_selectors = ("a[href*='/login']", "button:has-text('Sign in')")
    attach_button_selectors = ("button[aria-label*='Attach' i]",)
    file_input_selectors = ("input[type=file]",)
    send_with_enter = True
    response_timeout_ms = 180_000
    stability_ms = 2_000
```

Each selector list is tried **in order**, so put the most specific first.
Ordering of the class-level attributes is the whole adapter contract:

| Attribute | Used by | Behaviour |
| --- | --- | --- |
| `home_url` / `login_url` | `open_chat`, `begin_login` | `entry_url` prefers `login_url` |
| `composer_selectors` | login detection + `submit_prompt` | a visible composer means `logged_in`, unless an explicit sign-in CTA is also visible (guest chat pages) |
| `send_button_selectors` | `submit_prompt` | clicked only when enabled; otherwise falls back to Enter |
| `stop_button_selectors` | `collect_response` | visible stop button means "still streaming", resets the stability timer |
| `response_selectors` | `response_count`, `collect_response`, `capture_response` | reply is accepted once the count exceeds the pre-submit baseline **and** text stops changing for `stability_ms` |
| `logged_in_selectors` / `logged_out_selectors` | `detect_login_state` | drives `available` / `needs_reauth` |
| `file_input_selectors` / `attach_button_selectors` | `_attach_files` | direct `set_input_files`, else a file chooser via the attach button |
| `send_with_enter` | `submit_prompt` | `False` on sites where Enter inserts a newline |
| `login_start_selectors` | `open_login_form` | optional control that opens the form (guest landing pages) |
| `login_email_selectors` / `login_password_selectors` | `prefill_login` | the site's own fields; prefill raises `NeedsHuman` instead of guessing |
| `login_email_submit_selectors` | `prefill_login` | optional "Next" between email and password steps |
| `login_submit_selectors` | `prefill_login` | final submit; falls back to Enter in the password field |
| `mfa_selectors` / `mfa_text_markers` | `detect_login_obstacle` | OTP/PIN/device prompts stop the flow as `needs_human` |
| `captcha_selectors` / `captcha_text_markers` | `detect_login_obstacle` | CAPTCHA is reported, never solved |
| `invalid_credential_markers` | `detect_login_obstacle` | rejected password → `needs_credential`, no retry |

Login prefill is optional. If a provider has no email/password form (X-only or
SSO-only), leave the defaults in place: the runner clicks the site's own
"Continue with …" button, reports `needs_human`, and the human finishes in the
window.

### 2. Register it

`providers/registry.py` — import and append in `_builtin_adapters()`:

```python
from forgemesh_browser.providers.example.adapter import Adapter as Example
adapters = [
        ChatGPT(), Claude(), Kimi(), DeepSeek(), Grok(), Perplexity(),
        Gemini(), Sarvam(), Example(),
    ]
```

`config/providers.yaml` (or `~/.config/forgemesh-browser/providers.yaml`, which
wins because it is merged second):

```yaml
providers:
  example:
    enabled: true
    name: Example AI
    url: https://chat.example.com/
```

`enabled: false` makes `registry.get("example")` raise `AdapterError` rather
than silently using a disabled provider.

### 3. Verify, then declare

```bash
fmb providers                                     # adapter is listed
fmb account login example --profile work          # you log in by hand, visibly
fmb account complete example --profile work       # records status=available
fmb account check example                         # live re-detection
fmb account ask example "What is 2+2?" --timeout 120
fmb account capture example --output reply.png
```

Until `account check` reports `available` against your own account, the provider
is not "connected" — say so rather than assuming the selectors are right.

### 4. Add tests

`tests/unit/test_providers.py` already parametrises registry lookup, disabled
config, `parse_retry_after` and `detect_login_state` over a `FakePage`. Add the
new ID to the registry parametrisation and one `FakePage` case per state
(`logged_in`, `logged_out`, `unknown`) so a selector typo fails at test time
instead of in front of a live account.

### Failure modes you should preserve

- `NotAuthenticated` (401) — login state is `logged_out`; message tells the user
  to run `fmb account login …`, never asks for a password.
- `AdapterError` (502) — composer missing, no enabled send control, no reply
  within `response_timeout_ms`, or a reply that never stabilises. Partial text
  length is reported so you can tell "slow" from "broken selectors".
- `RateLimited` (429) — only when the page itself contains one of
  `RESTRICTION_PHRASES`. `retry_after` comes from `parse_retry_after`, which
  reads "try again in N minutes" style text and **never invents** a reset time.
  The account is marked `rate_limited` and left alone: no switching, no
  rotation, no retry loop against the provider's limit.

---

## Part 2 — Adding an AI provider adapter

This is the extension point for model backends (local or remote). The router is
deliberately conservative: it only exposes explicitly configured providers and
**never falls back** from a local model to a paid one.

### Contract

`ai/base.py` defines three pieces:

```python
@dataclass(slots=True)
class AIRequest:      # prompt, images: list[ImagePayload], system, model,
    ...               # provider, output_format, temperature, max_tokens

@dataclass(slots=True)
class AIResponse:     # text, provider, model, usage, raw
    ...

class AIProvider(ABC):
    provider_id: str = "base"
    supports_vision: bool = False
    is_local: bool = True

    async def complete(self, request: AIRequest) -> AIResponse: ...
    async def health(self) -> dict[str, Any]: ...
    async def list_models(self) -> list[str]: ...   # optional, defaults to []
```

Rules every adapter must follow:

- Translate transport failures into `AIProviderError` (HTTP status + a trimmed
  body snippet, or "not reachable at <host>"). Never leak an `httpx` exception.
- `health()` must be cheap (a few seconds), must not raise, and return
  `{"provider", "available", …}` — the daemon calls it for `fmb ai-health`.
- Set `supports_vision` truthfully. `AIRouter.complete()` raises
  `AIProviderError` if images are attached to a provider that can't accept them.
- Set `is_local=False` for anything that sends data to a third party; that flag
  is surfaced in `/status` and `fmb ai-providers` so users can see it.
- Never log or return prompt text; only character counts reach the event bus.
- Read secrets from `Settings`, never hardcode keys.

### Worked example: LM Studio (`http://127.0.0.1:1234/v1`)

LM Studio speaks the OpenAI wire format, so it is mostly a configuration and
registration exercise.

**1. Settings fields** — `settings.py`:

```python
lmstudio_base_url: str | None = None      # e.g. http://127.0.0.1:1234/v1
lmstudio_api_key: str | None = None       # optional; local servers often skip it
lmstudio_model: str = "local-model"
```

Because settings use `env_prefix="FMB_"`, this automatically gives
`FMB_LMSTUDIO_BASE_URL`, and the bundled `config/settings.yaml` plus
`.env.example` are the places to document the new keys.

**2. Adapter** — `src/forgemesh_browser/ai/lmstudio_adapter.py`.
`OpenAICompatibleProvider` already implements `/chat/completions`, image
`data_url` parts, `response_format={"type":"json_object"}` and error
normalisation, so subclass it:

```python
"""LM Studio adapter — OpenAI-compatible local server, opt-in only."""

from __future__ import annotations

from forgemesh_browser.ai.openai_adapter import OpenAICompatibleProvider
from forgemesh_browser.core.exceptions import AIProviderError
from forgemesh_browser.settings import Settings


class LMStudioProvider(OpenAICompatibleProvider):
    provider_id = "lmstudio"
    supports_vision = True     # only if the loaded model actually accepts images
    is_local = True

    def __init__(self, settings: Settings) -> None:
        if not settings.lmstudio_base_url:
            raise AIProviderError("LM Studio requires FMB_LMSTUDIO_BASE_URL")
        settings.openai_base_url = settings.lmstudio_base_url
        settings.openai_api_key = settings.lmstudio_api_key or "local"
        settings.openai_model = settings.lmstudio_model
        super().__init__(settings)
```

(Mutating a copy of the settings object, or duplicating the ~40 lines of
`OpenAICompatibleProvider.complete()` with its own field names, are both
acceptable; the second is cleaner if the wire format diverges at all.)

**3. Registration** — `ai/router.py`, next to the existing conditional
`openai` block. Registration is **conditional on configuration**, which is what
keeps an unconfigured provider invisible:

```python
self.providers: dict[str, AIProvider] = {"ollama": OllamaProvider(settings)}
if settings.openai_base_url and settings.openai_api_key:
    try:
        self.providers["openai"] = OpenAICompatibleProvider(settings)
    except AIProviderError as exc:
        logger.warning("OpenAI-compatible provider disabled: %s", exc)
if settings.lmstudio_base_url:
    try:
        self.providers["lmstudio"] = LMStudioProvider(settings)
    except AIProviderError as exc:
        logger.warning("LM Studio provider disabled: %s", exc)
```

That is the complete change set. `AIRouter._resolve()` reads
`provider_id or settings.ai_provider`, so `fmb ai --provider lmstudio "..."`,
`POST /ai {"provider":"lmstudio"}`, `fmb ai-models --provider lmstudio` and
`fmb ai-health` all work without touching the CLI or API layers, and
`ai_provider: lmstudio` in `settings.yaml` makes it the default.

**4. Tests** — mirror `tests/unit/test_ai_router.py`:

```python
def test_lmstudio_is_not_registered_without_configuration(settings):
    router = AIRouter(settings, EventBus())
    assert "lmstudio" not in router.providers
    with pytest.raises(AIProviderError, match="not configured"):
        await router.complete("hi", provider="lmstudio")
```

Plus one registration test with the env var set, one asserting the router still
never selects `lmstudio` implicitly when Ollama fails, and (if you implement a
distinct transport) a `httpx.MockTransport` test for the response shape.

### The two invariants to keep

1. **No silent paid fallback.** `AIRouter.complete()` resolves exactly one
   provider and raises `ai_provider_error` when it is missing or unreachable.
   Nothing retries against `openai` because a local model timed out — that would
   spend the user's money and ship their page content to a third party.
2. **Page content is untrusted.** When `include_page` is set, the prompt is
   assembled by `ai/prompt_manager.build_page_prompt()` and sent with
   `PAGE_SYSTEM_PROMPT`, which tells the model to treat extracted text as data,
   not instructions. A new adapter must pass `system` through — don't drop it,
   and don't let a model's answer be executed as a shell command or navigated to
   without a human decision.

### Choosing between the two families

"Use my self-hosted model to answer questions" → AI provider (Part 2).
"Automate the website where I already have an account" → browser provider
(Part 1). They compose: `fmb account ask chatgpt "…" --file ./doc.png` drives
the ChatGPT **site**, while `fmb ai "summarise this page" --page` uses your
**model** backend. No AI adapter ever sees a provider account's cookies, and no
browser adapter needs an AI backend to be configured.
