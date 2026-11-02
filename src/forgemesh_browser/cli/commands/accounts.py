"""Provider accounts: manual login, health, prompts and response capture."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import typer

from forgemesh_browser.cli.client import APIError, ServiceClient, ensure_daemon, run_with_client
from forgemesh_browser.cli.interactive import export_file
from forgemesh_browser.cli.output import Output, get_context, resolve_profile


def register(app: typer.Typer) -> None:
    account_app = typer.Typer(
        name="account",
        no_args_is_help=True,
        help="Manual AI-platform logins, session health and account prompts.",
    )
    app.add_typer(account_app, name="account")

    @account_app.command("login")
    def login(
        ctx: typer.Context,
        provider: str = typer.Argument(..., help="e.g. chatgpt, claude, gemini, deepseek, grok, kimi, perplexity, sarvam"),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        headless: bool = typer.Option(
            False, "--headless", help="Use a hidden browser (no manual sign-in possible)."
        ),
        wait: bool = typer.Option(
            True, "--wait/--no-wait", help="Wait for you to finish signing in."
        ),
        login_timeout: float = typer.Option(
            300.0,
            "--login-timeout",
            help="Seconds to keep polling for a completed sign-in.",
        ),
    ) -> None:
        """Open the provider site for a manual, trusted login."""
        _run_login(ctx, provider, profile, headless, wait, login_timeout)

    @account_app.command("reauthenticate")
    def reauthenticate(
        ctx: typer.Context,
        provider: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        headless: bool = typer.Option(False, "--headless"),
        wait: bool = typer.Option(True, "--wait/--no-wait"),
        login_timeout: float = typer.Option(300.0, "--login-timeout"),
    ) -> None:
        """Repeat the manual login flow after a session expired."""
        _run_login(ctx, provider, profile, headless, wait, login_timeout)

    @account_app.command("complete")
    def complete(
        ctx: typer.Context,
        provider: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Verify that the manual login succeeded and record the session."""
        cli, prof = _profile(ctx, profile)
        payload = {"profile": prof}
        data = run_with_client(
            ctx,
            lambda client: client.post(
                f"/accounts/{provider}/login/complete", payload
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(
                f"{data.get('provider_name', provider)} is available in profile "
                f"'{data.get('profile_id')}'"
            )

    @account_app.command("list")
    def list_accounts(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        provider: str | None = typer.Option(None, "--provider"),
    ) -> None:
        """List registered accounts."""
        cli, prof = _profile(ctx, profile)
        params: dict[str, Any] = {}
        if prof:
            params["profile"] = prof
        if provider:
            params["provider"] = provider
        data = run_with_client(ctx, lambda client: client.get("/accounts", params=params))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        accounts = data.get("accounts", [])
        if not accounts:
            out.info(
                "No accounts registered. Run `fmb account login <provider> --profile <id>`."
            )
            return
        out.table(
            "Accounts",
            ["Profile", "Provider", "Status", "Last OK", "Last error"],
            [
                [
                    item.get("profile_id"),
                    item.get("provider_id"),
                    item.get("status"),
                    item.get("last_ok_at") or "",
                    (item.get("last_error") or "")[:80],
                ]
                for item in accounts
            ],
        )

    @account_app.command("status")
    def status(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Summarise availability per provider."""
        cli, prof = _profile(ctx, profile)
        params = {"profile": prof} if prof else None
        data = run_with_client(ctx, lambda client: client.get("/accounts/status", params=params))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.key_values(
            "Account availability",
            {
                "Total": data.get("total", 0),
                "Available": data.get("available", 0),
                "Needs reauthentication": data.get("needs_reauth", 0),
                "Rate limited": data.get("rate_limited", 0),
                "Errors": data.get("errors", 0),
                "Unknown": data.get("unknown", 0),
            },
        )
        providers = data.get("providers", [])
        if providers:
            out.table(
                "Per provider",
                ["Provider", "Available", "Reauth", "Limited", "Errors"],
                [
                    [
                        item.get("provider_name") or item.get("provider_id"),
                        item.get("available", 0),
                        item.get("needs_reauth", 0),
                        item.get("rate_limited", 0),
                        item.get("errors", 0),
                    ]
                    for item in providers
                ],
            )

    @account_app.command("inspect")
    def inspect(
        ctx: typer.Context,
        provider: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Show one account's recorded state."""
        cli, prof = _profile(ctx, profile)
        params: dict[str, Any] = {}
        if prof:
            params["profile"] = prof
        data = run_with_client(
            ctx,
            lambda client: client.get(f"/accounts/{provider}", params=params),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.key_values(
                f"{data.get('provider_name', provider)} / {data.get('profile_id')}",
                {
                    "Status": data.get("status"),
                    "Last checked": data.get("last_checked_at"),
                    "Last success": data.get("last_ok_at"),
                    "Last error": data.get("last_error"),
                    "Retry after": data.get("retry_after"),
                },
            )

    @account_app.command("check")
    def check(
        ctx: typer.Context,
        provider: str | None = typer.Argument(None, help="Check one provider (default: all)."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Verify stored sessions against the live websites."""
        cli, prof = _profile(ctx, profile)
        payload = {"profile": prof}
        if provider:
            data = run_with_client(
                ctx,
                lambda client: client.post(
                    f"/accounts/{provider}/check", payload
                ),
            )
            accounts = [data]
        else:
            data = run_with_client(
                ctx, lambda client: client.post("/accounts/check-all", payload)
            )
            accounts = data.get("accounts", [])
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.table(
            "Session check",
            ["Profile", "Provider", "Status", "Detail"],
            [
                [
                    item.get("profile_id"),
                    item.get("provider_id"),
                    item.get("status"),
                    (item.get("last_error") or "")[:80],
                ]
                for item in accounts
            ],
        )

    @account_app.command("ask")
    def ask(
        ctx: typer.Context,
        provider: str = typer.Argument(...),
        prompt: str = typer.Argument(..., help="Prompt text."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        files: list[str] = typer.Option(
            [], "--file", "-f", help="Attach a local file (repeatable)."
        ),
        timeout: int = typer.Option(180, "--timeout", help="Response timeout in seconds."),
    ) -> None:
        """Submit a prompt through the provider's web UI and collect the answer."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {
            "profile": prof,
            "prompt": prompt,
            "files": files,
            "timeout_ms": timeout * 1000,
        }
        data = run_with_client(
            ctx,
            lambda client: client.post(f"/accounts/{provider}/ask", payload),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        text = (data.get("text") or "").strip()
        if text:
            out.text(text)
        for image in data.get("images", []):
            out.info(f"[dim]image: {image}[/dim]")
        out.info(
            f"[dim]{data.get('provider_name', provider)} • "
            f"{data.get('chars', len(text))} chars • "
            f"task {data.get('task_id')} • {data.get('duration_ms', 0):.0f} ms[/dim]"
        )

    @account_app.command("capture")
    def capture(
        ctx: typer.Context,
        provider: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        output: str | None = typer.Option(None, "--output", "-o", help="Copy the screenshot here."),
    ) -> None:
        """Screenshot the provider's latest response."""
        cli, prof = _profile(ctx, profile)
        payload = {"profile": prof}
        data = run_with_client(
            ctx,
            lambda client: client.post(f"/accounts/{provider}/capture", payload),
        )
        if output:
            copied = export_file(data["path"], output)
            data = {**data, "path": str(copied)}
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Response capture saved: {data['path']}")


def _profile(ctx: typer.Context, profile: str | None):
    cli = get_context(ctx)
    return cli, resolve_profile(cli, profile)


def _run_login(
    ctx: typer.Context,
    provider: str,
    profile: str | None,
    headless: bool,
    wait: bool,
    login_timeout: float = 300.0,
) -> None:
    cli, prof = _profile(ctx, profile)
    payload: dict[str, Any] = {"headless": headless}
    if prof:
        payload["profile"] = prof
    begin = run_with_client(
        ctx,
        lambda client: client.post(f"/accounts/{provider}/login/begin", payload),
    )
    out = Output(cli)
    if cli.json_mode:
        out.result(begin)
        if wait:
            out.info(
                "JSON mode does not wait. Finish signing in, then run "
                f"`fmb account complete {provider}`."
            )
        return
    out.key_values(
        f"Login: {begin.get('provider_name', provider)}",
        {
            "Profile": begin.get("profile_id"),
            "URL": begin.get("url"),
            "Browser": "headed" if begin.get("headless") is False else "headless",
            "Detected state": begin.get("login_state"),
        },
    )
    if begin.get("logged_in"):
        out.success(f"{begin.get('provider_name', provider)} is already signed in.")
        return
    if begin.get("login_state") == "blocked":
        out.warning(
            "The identity provider refused this automated browser. ForgeMesh will not "
            "bypass that check. Use the site's own email/phone login form in the "
            "browser window, or the provider's official API with an API key."
        )
    if begin.get("headless"):
        out.warning(
            "The browser is headless, so manual sign-in is not possible here. "
            "Re-run without --headless, or finish sign-in elsewhere and run "
            f"`fmb account complete {provider}`."
        )
        return
    if not wait:
        out.info(f"Finish signing in, then run: fmb account complete {provider}")
        return
    data = _wait_for_login(
        ctx,
        provider,
        begin.get("profile_id") or prof,
        timeout=login_timeout,
    )
    out.success(
        f"{data.get('provider_name', provider)} is available in profile "
        f"'{data.get('profile_id')}'"
    )


#: The provider page is re-checked at this interval while waiting for a login.
LOGIN_POLL_INTERVAL = 2.0


def _wait_for_login(
    ctx: typer.Context,
    provider: str,
    profile: str | None,
    *,
    timeout: float = 300.0,
    interval: float = LOGIN_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll ``login/complete`` until the session is detected.

    Works without a TTY, so ``fmb account login --wait`` can run in the
    background or from a script. The daemon does the page inspection; the CLI
    only asks it again every couple of seconds.
    """

    cli = get_context(ctx)
    payload = {"profile": profile} if profile else {}
    typer.echo()
    typer.echo(f"Waiting for {provider} sign-in to be detected (Ctrl+C to stop)…")

    async def poll() -> dict[str, Any]:
        ensure_daemon(cli.settings)
        deadline = time.monotonic() + timeout
        last_error = "no login detected yet"
        async with ServiceClient(cli.settings) as client:
            while time.monotonic() < deadline:
                try:
                    return await client.post(
                        f"/accounts/{provider}/login/complete", payload
                    )
                except APIError as exc:
                    if exc.http_status not in {400, 401, 403, 409, 428}:
                        raise
                    last_error = exc.message
                await asyncio.sleep(interval)
        typer.echo(
            f"Sign-in was not detected within {timeout:.0f}s ({last_error}). "
            f"The browser stays open; finish signing in and run "
            f"`fmb account complete {provider}` afterwards.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        return asyncio.run(poll())
    except KeyboardInterrupt:
        typer.echo(
            f"Stopped waiting. The browser stays open; finish signing in and run "
            f"`fmb account complete {provider}`.",
            err=True,
        )
        raise typer.Exit(1) from None
