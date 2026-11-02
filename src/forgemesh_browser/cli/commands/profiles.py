"""Isolated browser profiles (Chrome-like)."""

from __future__ import annotations

import typer

from forgemesh_browser.cli.client import run_with_client
from forgemesh_browser.cli.interactive import confirm
from forgemesh_browser.cli.output import Output, get_context


def register(app: typer.Typer) -> None:
    profile_app = typer.Typer(
        name="profile",
        no_args_is_help=True,
        help="Create, inspect and switch isolated browser profiles.",
    )
    app.add_typer(profile_app, name="profile")

    @profile_app.command("create")
    def create(
        ctx: typer.Context,
        profile_id: str = typer.Argument(..., help="Slug, e.g. primary or work-2."),
        name: str | None = typer.Option(None, "--name", help="Display name."),
        description: str = typer.Option("", "--description", "-d"),
    ) -> None:
        """Create an isolated profile with its own cookies and storage."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx,
            lambda client: client.post(
                "/profiles",
                {"id": profile_id, "name": name, "description": description},
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Created profile '{data['id']}' ({data['name']})")
            out.info(f"[dim]{data['path']}[/dim]")

    @profile_app.command("list")
    def list_profiles(ctx: typer.Context) -> None:
        """List profiles."""
        cli = get_context(ctx)
        profiles = run_with_client(ctx, lambda client: client.get("/profiles"))
        out = Output(cli)
        if cli.json_mode:
            out.result(profiles)
            return
        rows = [
            [
                "→" if profile.get("selected") else "",
                profile.get("id"),
                profile.get("name"),
                profile.get("account_count"),
                profile.get("size_bytes"),
                profile.get("last_used_at") or "never",
            ]
            for profile in profiles
        ]
        if not rows:
            out.info("No profiles. Create one with `fmb profile create <name>`.")
            return
        out.table("Profiles", ["", "ID", "Name", "Accounts", "Bytes", "Last used"], rows)

    @profile_app.command("current")
    def current(ctx: typer.Context) -> None:
        """Show the active profile."""
        cli = get_context(ctx)
        profiles = run_with_client(ctx, lambda client: client.get("/profiles"))
        selected = next((p for p in profiles if p.get("selected")), None)
        out = Output(cli)
        if cli.json_mode:
            out.result(selected or {})
        elif selected:
            out.success(f"Active profile: {selected['id']} ({selected['name']})")
        else:
            out.warning("No active profile. Create one with `fmb profile create <name>`.")

    @profile_app.command("activate")
    def activate(
        ctx: typer.Context,
        profile_id: str = typer.Argument(...),
    ) -> None:
        """Select the default profile for future commands."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx, lambda client: client.post(f"/profiles/{profile_id}/activate")
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Active profile: {data['id']} ({data['name']})")

    @profile_app.command("inspect")
    def inspect(
        ctx: typer.Context,
        profile_id: str = typer.Argument(...),
    ) -> None:
        """Show profile details, isolation state and registered accounts."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get(f"/profiles/{profile_id}"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        profile = data.get("profile", {})
        isolation = data.get("isolation", {})
        out.key_values(
            f"Profile: {profile.get('name')}",
            {
                "ID": profile.get("id"),
                "Path": profile.get("path"),
                "Selected": profile.get("selected"),
                "Running": data.get("running"),
                "Size (bytes)": profile.get("size_bytes"),
                "Created": profile.get("created_at"),
                "Last used": profile.get("last_used_at"),
                "Isolated path": isolation.get("path"),
                "Directory mode": isolation.get("mode"),
                "Private (0700)": isolation.get("private"),
                "Symlink": isolation.get("is_symlink"),
                "Browser state": ", ".join(isolation.get("browser_dirs") or []) or "empty",
                "Shared with": isolation.get("shared_with_other_profiles") or "none",
            },
        )
        accounts = data.get("accounts", [])
        if accounts:
            out.table(
                "Accounts",
                ["Provider", "Status", "Last OK", "Last error"],
                [
                    [
                        account.get("provider_id"),
                        account.get("status"),
                        account.get("last_ok_at") or "",
                        (account.get("last_error") or "")[:80],
                    ]
                    for account in accounts
                ],
            )
        else:
            out.info("[dim]No accounts registered in this profile yet.[/dim]")

    @profile_app.command("rename")
    def rename(
        ctx: typer.Context,
        profile_id: str = typer.Argument(...),
        name: str = typer.Argument(..., help="New display name."),
    ) -> None:
        """Rename a profile (its directory and ID stay the same)."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx, lambda client: client.patch(f"/profiles/{profile_id}", {"name": name})
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Renamed profile '{profile_id}' to '{data['name']}'")

    @profile_app.command("delete")
    def delete(
        ctx: typer.Context,
        profile_id: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    ) -> None:
        """Delete a profile and its browser storage. This is irreversible."""
        cli = get_context(ctx)
        if cli.json_mode and not yes:
            raise typer.BadParameter(
                "Use --yes to delete a profile in JSON mode (deletion is irreversible)."
            )
        confirm(
            f"Delete profile '{profile_id}' and all of its cookies, storage and sessions?",
            yes=yes,
        )
        data = run_with_client(
            ctx,
            lambda client: client.delete(
                f"/profiles/{profile_id}", params={"confirm": "true"}
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Deleted profile '{data.get('deleted')}'")
