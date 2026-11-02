"""Browser lifecycle, navigation, tabs, inspection and screenshots."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from forgemesh_browser.cli.client import health, run_with_client
from forgemesh_browser.cli.interactive import export_file
from forgemesh_browser.cli.output import Output, get_context, resolve_profile
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database


def _profile(ctx: typer.Context, profile: str | None):
    cli = get_context(ctx)
    return cli, resolve_profile(cli, profile)


def _start_if_headless_change(client, profile: str | None, headless: bool) -> Any:
    payload: dict[str, Any] = {"headless": headless}
    if profile:
        payload["profile"] = profile
    return client.post("/browser/start", payload)


def register(app: typer.Typer) -> None:
    @app.command()
    def start(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        headed: bool = typer.Option(
            False, "--headed", help="Show a browser window (needed for manual login)."
        ),
    ) -> None:
        """Start the browser for a profile (headless by default)."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"headless": False if headed else None}
        if prof:
            payload["profile"] = prof
        data = run_with_client(ctx, lambda client: client.post("/browser/start", payload))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.key_values(
                "Browser started",
                {
                    "Profile": data.get("profile"),
                    "Mode": "headed" if data.get("headless") is False else "headless",
                    "Tabs": data.get("tabs"),
                },
            )
            out.success(f"Browser ready for profile '{data.get('profile')}'")

    @app.command()
    def stop(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        all_profiles: bool = typer.Option(False, "--all", help="Stop every running profile."),
    ) -> None:
        """Stop a running browser profile."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"all": all_profiles}
        if prof:
            payload["profile"] = prof
        data = run_with_client(
            ctx,
            lambda client: client.post("/browser/stop", payload),
            require_running=True,
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        elif "stopped_all" in data:
            out.success(f"Stopped {data['stopped_all']} browser profile(s)")
        else:
            out.success(f"Stopped profile '{data.get('stopped')}'")

    @app.command()
    def status(ctx: typer.Context) -> None:
        """Show daemon, profile and session status without starting anything."""
        cli = get_context(ctx)
        out = Output(cli)
        payload = health(cli.settings)
        if payload is None:
            profiles = _offline_profiles(cli.settings)
            offline = {
                "daemon": {"running": False, "data_dir": str(cli.settings.data_dir)},
                "selected_profile": _selected_profile(cli.settings),
                "profiles": [
                    profile.model_dump(mode="json") for profile in profiles
                ],
            }
            if cli.json_mode:
                out.result(offline)
            else:
                out.info("[yellow]Daemon is not running.[/yellow] Run any browser command to start it.")
                _print_profiles(out, offline)
            return
        data = run_with_client(
            ctx,
            lambda client: _fetch_status(client),
        )
        if cli.json_mode:
            out.result(data)
            return
        daemon = data.get("daemon") or {}
        out.key_values(
            "ForgeMesh Browser",
            {
                "Daemon": f"running (pid {daemon.get('pid')})",
                "Data dir": daemon.get("data_dir"),
                "Browser channel": data.get("channel"),
                "Selected profile": data.get("selected_profile"),
                "Max contexts": data.get("max_contexts"),
            },
        )
        _print_profiles(out, data)
        _print_all_profiles(out, data.get("all_profiles") or [])

    @app.command()
    def open(
        ctx: typer.Context,
        url: str = typer.Argument(..., help="URL or search term."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab", help="Target tab id."),
        headed: bool = typer.Option(False, "--headed", help="Show the window."),
    ) -> None:
        """Open a URL in the profile's browser."""
        cli, prof = _profile(ctx, profile)
        if headed:
            run_with_client(
                ctx, lambda client: _start_if_headless_change(client, prof, False)
            )
        payload: dict[str, Any] = {"url": url, "tab_id": tab}
        if prof:
            payload["profile"] = prof
        data = run_with_client(
            ctx, lambda client: client.post("/browser/navigate", payload)
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.key_values(
                "Page loaded",
                {
                    "URL": data.get("url"),
                    "Title": data.get("title"),
                    "Status": data.get("status"),
                    "Tab": data.get("tab"),
                },
            )

    @app.command()
    def tabs(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """List open tabs."""
        cli, prof = _profile(ctx, profile)
        params = {"profile": prof} if prof else None
        data = run_with_client(ctx, lambda client: client.get("/browser/tabs", params=params))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        rows = [
            [
                "→" if tab.get("is_active") else "",
                tab.get("id"),
                tab.get("title"),
                tab.get("url"),
            ]
            for tab in data.get("tabs", [])
        ]
        if not rows:
            out.info("No open tabs. Run `fmb open <url>` first.")
            return
        out.table("Tabs", ["", "Tab", "Title", "URL"], rows)

    @app.command("new-tab")
    def new_tab(
        ctx: typer.Context,
        url: str = typer.Argument("about:blank"),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Open a new tab."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"url": url}
        if prof:
            payload["profile"] = prof
        data = run_with_client(ctx, lambda client: client.post("/browser/tabs", payload))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Opened tab {data.get('id')} ({data.get('url')})")

    @app.command("close-tab")
    def close_tab(
        ctx: typer.Context,
        tab: str | None = typer.Option(None, "--tab", help="Tab id (default: active)."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Close a tab."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"tab_id": tab}
        if prof:
            payload["profile"] = prof
        data = run_with_client(
            ctx, lambda client: client.post("/browser/tabs/close", payload)
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Closed tab {data.get('closed')}")

    @app.command()
    def activate(
        ctx: typer.Context,
        tab: str = typer.Argument(..., help="Tab id."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Make a tab active."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"tab_id": tab}
        if prof:
            payload["profile"] = prof
        data = run_with_client(
            ctx, lambda client: client.post("/browser/tabs/activate", payload)
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Active tab: {data.get('active_tab')}")

    @app.command()
    def screenshot(
        ctx: typer.Context,
        output: Path | None = typer.Option(None, "--output", "-o", help="Copy the file here."),
        full_page: bool = typer.Option(False, "--full-page", help="Capture the whole page."),
        selector: str | None = typer.Option(None, "--selector", "-s", help="Capture one element."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Capture a viewport, full-page or element screenshot."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {
            "full_page": full_page,
            "selector": selector,
            "tab_id": tab,
        }
        if prof:
            payload["profile"] = prof
        data = run_with_client(ctx, lambda client: client.post("/screenshots", payload))
        if output is not None:
            copied = export_file(data["path"], output)
            data = {**data, "path": str(copied), "stored_path": data["path"]}
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Screenshot saved: {data['path']}")
            out.info(
                f"[dim]{data.get('width', '?')}×{data.get('height', '?')} • "
                f"{data.get('size', 0)} bytes[/dim]"
            )

    @app.command()
    def inspect(
        ctx: typer.Context,
        interactive_only: bool = typer.Option(
            True, "--interactive-only/--all", help="Only interactive elements."
        ),
        limit: int = typer.Option(300, "--limit", min=1, max=2000),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
        max_rows: int = typer.Option(60, "--max-rows", help="Rows to print in table mode."),
    ) -> None:
        """Inspect the DOM and print stable element references."""
        cli, prof = _profile(ctx, profile)
        data = run_with_client(
            ctx,
            lambda client: client.post(
                "/automation/execute",
                {
                    "action": "inspect",
                    "profile": prof,
                    "tab_id": tab,
                    "interactive_only": interactive_only,
                    "limit": limit,
                },
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        elements = data.get("elements", [])
        rows = []
        for element in elements[:max_rows]:
            notes = []
            if element.get("type"):
                notes.append(str(element["type"]))
            if element.get("placeholder"):
                notes.append(f"placeholder={element['placeholder']}")
            if element.get("href"):
                notes.append(str(element["href"])[:60])
            if element.get("disabled"):
                notes.append("disabled")
            rows.append(
                [
                    element.get("ref"),
                    element.get("role") or "",
                    (element.get("name") or element.get("text") or "")[:70],
                    element.get("tag"),
                    "; ".join(notes),
                ]
            )
        out.info(
            f"[bold]{data.get('title') or '(untitled)'}[/bold] — {data.get('url')}"
        )
        out.table("Elements", ["Ref", "Role", "Name", "Tag", "Notes"], rows)
        if len(elements) > max_rows:
            out.info(f"[dim]Showing {max_rows} of {len(elements)} elements. Use --json for all.[/dim]")
        out.info(f"[dim]{len(elements)} element(s); click with: fmb click <ref-or-name>[/dim]")

    @app.command()
    def extract(
        ctx: typer.Context,
        output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON here."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Extract text, headings, links, images and forms as JSON."""
        cli, prof = _profile(ctx, profile)
        data = run_with_client(
            ctx,
            lambda client: client.post(
                "/automation/execute",
                {"action": "extract", "profile": prof, "tab_id": tab},
            ),
        )
        out = Output(cli)
        if output is not None:
            target = _write_json(output, data)
            if not cli.json_mode:
                out.success(f"Extracted page data to {target}")
        elif cli.json_mode:
            out.result(data)
        else:
            out.key_values(
                "Page",
                {
                    "URL": data.get("url"),
                    "Title": data.get("title"),
                    "Description": data.get("description"),
                    "Headings": len(data.get("headings", [])),
                    "Links": len(data.get("links", [])),
                    "Images": len(data.get("images", [])),
                    "Forms": len(data.get("forms", [])),
                    "Text chars": len(data.get("text", "")),
                },
            )
            out.info("[dim]Use --json or --output page.json for the full structure.[/dim]")

    @app.command()
    def reload(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Reload the active tab."""
        _simple_navigation(ctx, "/browser/reload", profile, tab, "Reloaded")

    @app.command()
    def back(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Go back in history."""
        _simple_navigation(ctx, "/browser/back", profile, tab, "Went back")

    @app.command()
    def forward(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Go forward in history."""
        _simple_navigation(ctx, "/browser/forward", profile, tab, "Went forward")

    @app.command()
    def console(
        ctx: typer.Context,
        profile: str | None = typer.Option(None, "--profile", "-p"),
        limit: int = typer.Option(100, "--limit", min=1, max=1000),
    ) -> None:
        """Show captured console messages and page errors."""
        cli, prof = _profile(ctx, profile)
        params: dict[str, Any] = {"limit": limit}
        if prof:
            params["profile"] = prof
        data = run_with_client(ctx, lambda client: client.get("/browser/console", params=params))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        for item in data.get("console", []):
            out.info(
                f"[dim]{item.get('type', 'log')}[/dim] {item.get('text', '')}"
            )
        for item in data.get("errors", []):
            out.warning(f"page error: {item.get('message', item)}")
        if not data.get("console") and not data.get("errors"):
            out.info("No console output captured.")


def _simple_navigation(
    ctx: typer.Context,
    path: str,
    profile: str | None,
    tab: str | None,
    verb: str,
) -> None:
    cli, prof = _profile(ctx, profile)
    payload: dict[str, Any] = {"tab_id": tab}
    if prof:
        payload["profile"] = prof
    data = run_with_client(ctx, lambda client: client.post(path, payload))
    out = Output(cli)
    if cli.json_mode:
        out.result(data)
    else:
        out.success(f"{verb}: {data.get('url')} — {data.get('title')}")


def _write_json(path: Path, data: Any) -> Path:
    target = path.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


async def _fetch_status(client) -> dict[str, Any]:
    status = await client.get("/status")
    status["all_profiles"] = await client.get("/profiles")
    return status


def _print_all_profiles(out: Output, profiles: list[dict[str, Any]]) -> None:
    if not profiles:
        return
    out.table(
        "All profiles",
        ["", "ID", "Name", "Accounts", "Last used"],
        [
            [
                "→" if profile.get("selected") else "",
                profile.get("id"),
                profile.get("name"),
                profile.get("account_count"),
                profile.get("last_used_at") or "never",
            ]
            for profile in profiles
        ],
    )


def _offline_profiles(settings: Settings):
    async def load():
        database = Database(settings.database_path)
        await database.init()
        try:
            manager = ProfileManager(
                settings, database, EventBus(), AuditLogger(database, settings.logs_dir)
            )
            return await manager.list()
        finally:
            await database.close()

    try:
        return asyncio.run(load())
    except Exception:
        return []


def _selected_profile(settings: Settings) -> str | None:
    from forgemesh_browser.profiles.storage import read_state

    return read_state(settings).active_profile


def _print_profiles(out: Output, data: dict[str, Any]) -> None:
    profiles = data.get("profiles") or []
    rows = []
    for profile in profiles:
        if "headless" in profile:
            rows.append(
                [
                    profile.get("profile"),
                    "headed" if profile.get("headless") is False else "headless",
                    profile.get("tabs"),
                    profile.get("url") or "",
                ]
            )
        else:
            rows.append(
                [
                    ("→ " if profile.get("selected") else "") + str(profile.get("id")),
                    profile.get("name"),
                    profile.get("account_count"),
                    profile.get("size_bytes"),
                ]
            )
    if not rows:
        out.info("No profiles yet. Create one with `fmb profile create <name>`.")
        return
    if "headless" in (profiles[0] if profiles else {}):
        out.table("Running profiles", ["Profile", "Mode", "Tabs", "URL"], rows)
    else:
        out.table("Profiles", ["Profile", "Name", "Accounts", "Bytes"], rows)
