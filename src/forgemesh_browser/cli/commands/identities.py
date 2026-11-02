"""Identities: one email per profile, encrypted passwords, prefill logins."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer

from forgemesh_browser.cli.client import run_with_client
from forgemesh_browser.cli.interactive import confirm
from forgemesh_browser.cli.output import Output, get_context
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.security.vault import Vault
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database


def _audit_local(settings: Settings, action: str, details: dict[str, Any]) -> None:
    """Record a vault operation without sending the secret to the daemon."""

    async def run() -> None:
        db = Database(settings.database_path)
        await db.init()
        try:
            await AuditLogger(db, settings.logs_dir).record(action, details=details)
        finally:
            await db.close()

    asyncio.run(run())


def register(app: typer.Typer) -> None:
    identity_app = typer.Typer(
        name="identity",
        no_args_is_help=True,
        help=(
            "Map an email to its own profile, store its password in the encrypted "
            "vault, and prefill provider logins."
        ),
    )
    app.add_typer(identity_app, name="identity")

    @identity_app.command("add")
    def add(
        ctx: typer.Context,
        email: str = typer.Argument(..., help="Account email, e.g. you@gmail.com."),
        label: str = typer.Option("", "--label", help="Human label."),
        profile: str | None = typer.Option(
            None, "--profile-id", help="Profile id (default: derived from the email)."
        ),
    ) -> None:
        """Register an email and give it a dedicated browser profile."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx,
            lambda client: client.post(
                "/identities",
                {"email": email, "label": label, "profile": profile},
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(
                f"Identity {data['email']} → profile '{data['profile_id']}'"
            )
            out.info(
                "[dim]Next: fmb identity password "
                f"{data['email']}[/dim]"
            )

    @identity_app.command("list")
    def list_identities(ctx: typer.Context) -> None:
        """List identities, their profile and whether a password is stored."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get("/identities"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        identities = data.get("identities", [])
        if not identities:
            out.info(
                "No identities. Add one with `fmb identity add you@gmail.com`."
            )
            return
        out.table(
            "Identities",
            ["Email", "Profile", "Label", "Password", "Providers", "Last login"],
            [
                [
                    item.get("email"),
                    item.get("profile_id"),
                    item.get("label") or "",
                    "yes" if item.get("has_password") else "no",
                    ", ".join(item.get("password_providers") or []) or "-",
                    item.get("last_login_at") or "never",
                ]
                for item in identities
            ],
        )

    @identity_app.command("remove")
    def remove(
        ctx: typer.Context,
        email: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
        delete_profile: bool = typer.Option(
            False,
            "--delete-profile",
            help="Also delete the profile directory (cookies and sessions are lost).",
        ),
    ) -> None:
        """Unregister an email and drop its stored password."""
        cli = get_context(ctx)
        if not yes:
            confirm(f"Remove identity {email} and its stored password?")
        data = run_with_client(
            ctx,
            lambda client: client.delete(
                f"/identities/{email}",
                params={
                    "confirm": "true",
                    "delete_profile": "true" if delete_profile else "false",
                },
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Removed identity {email}")
            if data.get("password_removed"):
                out.info("[dim]Vault entry deleted.[/dim]")
            if data.get("profile_deleted"):
                out.info("[dim]Profile directory deleted.[/dim]")

    @identity_app.command("password")
    def password(
        ctx: typer.Context,
        email: str = typer.Argument(...),
        provider: str | None = typer.Option(
            None,
            "--provider",
            help="Store a provider-specific password (default: shared password).",
        ),
        stdin: bool = typer.Option(
            False, "--stdin", help="Read the password from stdin (scripts)."
        ),
        clear: bool = typer.Option(
            False, "--clear", help="Delete the stored password instead of setting it."
        ),
    ) -> None:
        """Store an email password in the encrypted vault (never printed).

        The password goes straight into the local vault file; it is not sent to
        the daemon and never appears in logs or task records.
        """
        cli = get_context(ctx)
        out = Output(cli)

        if clear:
            data = run_with_client(
                ctx,
                lambda client: client.delete(
                    f"/identities/{email}/password",
                    params={"provider": provider} if provider else None,
                ),
            )
            if cli.json_mode:
                out.result(data)
            elif data.get("removed"):
                out.success(f"Deleted the stored password for {email}")
            else:
                out.info("No stored password matched.")
            return

        # Verify the identity exists before writing a secret for a typo.
        identities = run_with_client(ctx, lambda client: client.get("/identities"))
        known = {item.get("email") for item in identities.get("identities", [])}
        if email.lower() not in known:
            out.error(
                f"Unknown identity: {email}. Add it first: fmb identity add {email}"
            )
            raise typer.Exit(1)

        if stdin:
            value = sys.stdin.readline().rstrip("\n")
        else:
            value = typer.prompt(
                f"Password for {email}"
                + (f" ({provider})" if provider else ""),
                hide_input=True,
                confirmation_prompt=True,
            )
        if not value:
            out.error("Refusing to store an empty password.")
            raise typer.Exit(1)

        vault = Vault(cli.settings)
        vault.set_password(email, value, provider=provider)
        del value
        _audit_local(
            cli.settings,
            "identity.password_set",
            {"email": email.lower(), "provider": provider or "*"},
        )
        if cli.json_mode:
            out.result(
                {
                    "email": email.lower(),
                    "provider": provider,
                    "stored": True,
                    "path": str(vault.path),
                }
            )
        else:
            scope = f"provider '{provider}'" if provider else "all providers"
            out.success(f"Stored password for {email} ({scope}) in the encrypted vault")

    @identity_app.command("login")
    def login(
        ctx: typer.Context,
        email: str = typer.Argument(...),
        provider: list[str] = typer.Option(
            [], "--provider", help="Provider id (repeatable; default: all)."
        ),
        headed: bool = typer.Option(
            True,
            "--headed/--headless",
            help="Keep the window visible so you can finish 2FA/CAPTCHA.",
        ),
        timeout: int = typer.Option(
            180, "--timeout", help="Seconds to wait per provider."
        ),
    ) -> None:
        """Prefill the email/password form for one identity."""
        cli = get_context(ctx)
        payload: dict[str, Any] = {
            "providers": provider or [],
            "headless": not headed,
            "timeout_s": timeout,
        }
        data = run_with_client(
            ctx,
            lambda client: client.post(f"/identities/{email}/login", payload),
        )
        _render_results(cli, data.get("results", []))

    @identity_app.command("login-all")
    def login_all(
        ctx: typer.Context,
        email: list[str] = typer.Option(
            [], "--email", "-e", help="Limit to these emails (repeatable)."
        ),
        provider: list[str] = typer.Option(
            [], "--provider", help="Provider id (repeatable; default: all)."
        ),
        headed: bool = typer.Option(
            True,
            "--headed/--headless",
            help="Keep the window visible so you can finish 2FA/CAPTCHA.",
        ),
        timeout: int = typer.Option(
            180, "--timeout", help="Seconds to wait per provider."
        ),
    ) -> None:
        """Run the identity × provider prefill matrix."""
        cli = get_context(ctx)
        payload: dict[str, Any] = {
            "emails": email or [],
            "providers": provider or [],
            "headless": not headed,
            "timeout_s": timeout,
        }
        data = run_with_client(
            ctx, lambda client: client.post("/identities/login-all", payload)
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.key_values(
            "Prefill login matrix",
            {
                "Cells": data.get("total", 0),
                "Already signed in": data.get("already_logged_in", 0),
                "Signed in": data.get("logged_in", 0),
                "Needs human": data.get("needs_human", 0),
                "Missing password": data.get("needs_credential", 0),
                "Blocked": data.get("blocked", 0),
                "Errors": data.get("errors", 0),
            },
        )
        _render_results(cli, data.get("results", []))

    @identity_app.command("status")
    def status(ctx: typer.Context) -> None:
        """Show each identity's provider sessions and the vault state."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get("/identities"))
        vault = run_with_client(ctx, lambda client: client.get("/identities/vault"))
        out = Output(cli)
        if cli.json_mode:
            out.result({"identities": data.get("identities", []), "vault": vault})
            return
        out.key_values(
            "Vault",
            {
                "Path": vault.get("path"),
                "Initialised": vault.get("exists"),
                "Key source": vault.get("key_source"),
                "Emails": vault.get("emails", 0),
            },
        )
        identities = data.get("identities", [])
        if not identities:
            out.info("No identities yet.")
            return
        rows = []
        for identity in identities:
            accounts = identity.get("accounts") or []
            if not accounts:
                rows.append(
                    [
                        identity.get("email"),
                        identity.get("profile_id"),
                        "-",
                        "no session yet",
                    ]
                )
                continue
            for account in accounts:
                rows.append(
                    [
                        identity.get("email"),
                        identity.get("profile_id"),
                        account.get("provider_id"),
                        account.get("status")
                        + (
                            f" ({account['last_error'][:60]})"
                            if account.get("last_error")
                            else ""
                        ),
                    ]
                )
        out.table(
            "Identity sessions",
            ["Email", "Profile", "Provider", "Status"],
            rows,
        )


def _render_results(cli, results: list[dict[str, Any]]) -> None:
    out = Output(cli)
    if cli.json_mode:
        out.result({"results": results})
        return
    if not results:
        out.info("Nothing to do: no identities or providers selected.")
        return
    rows = [
        [
            item.get("email"),
            item.get("provider_id"),
            item.get("status"),
            ", ".join(item.get("filled") or []) or "-",
            (item.get("detail") or "")[:90],
        ]
        for item in results
    ]
    out.table(
        "Prefill login",
        ["Email", "Provider", "Status", "Filled", "Detail"],
        rows,
    )
    human = [item for item in results if item.get("status") == "needs_human"]
    if human:
        out.warning(
            f"{len(human)} cell(s) need you: finish CAPTCHA/2FA/verification in the "
            "browser window, then run `fmb identity status`."
        )
    missing = [item for item in results if item.get("status") == "needs_credential"]
    if missing:
        out.info(
            "[dim]Store the missing passwords with "
            "`fmb identity password <email> --provider <id>`.[/dim]"
        )
