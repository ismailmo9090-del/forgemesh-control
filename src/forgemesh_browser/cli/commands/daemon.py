"""Daemon lifecycle: foreground serve, shutdown, logs, tasks and events."""

from __future__ import annotations

import os
import time
from typing import Any

import typer

from forgemesh_browser.cli.client import health, run_with_client, shutdown_daemon
from forgemesh_browser.cli.output import Output, get_context
from forgemesh_browser.core.exceptions import DaemonError
from forgemesh_browser.utils.logging import tail_file


def register(app: typer.Typer) -> None:
    @app.command()
    def serve(
        ctx: typer.Context,
        host: str | None = typer.Option(None, "--host"),
        port: int | None = typer.Option(None, "--port"),
        log_level: str | None = typer.Option(None, "--log-level"),
    ) -> None:
        """Run the API daemon in the foreground (systemd-friendly)."""
        cli = get_context(ctx)
        if health(cli.settings):
            raise DaemonError(
                "A daemon is already running. Use `fmb shutdown` first."
            )
        os.environ["FMB_DATA_DIR"] = str(cli.settings.data_dir)
        from forgemesh_browser.daemon import main as daemon_main

        args: list[str] = []
        if host:
            args += ["--host", host]
        if port:
            args += ["--port", str(port)]
        if log_level:
            args += ["--log-level", log_level]
        daemon_main(args)

    @app.command()
    def shutdown(ctx: typer.Context) -> None:
        """Stop the local daemon and every browser it manages."""
        cli = get_context(ctx)
        data = shutdown_daemon(cli.settings)
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        elif data.get("stopped"):
            out.success(f"Daemon stopped (pid {data.get('pid')})")
        else:
            out.info(data.get("message", "Daemon is not running."))

    @app.command()
    def logs(
        ctx: typer.Context,
        lines: int = typer.Option(80, "--lines", "-n", min=1, max=5000),
        follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new lines."),
    ) -> None:
        """Show the daemon log."""
        cli = get_context(ctx)
        path = cli.settings.daemon_log_path
        content = tail_file(path, lines)
        if content:
            typer.echo(content)
        elif not follow:
            typer.echo(f"No log output yet at {path}")
        if follow:
            _follow(path)

    @app.command()
    def tasks(
        ctx: typer.Context,
        limit: int = typer.Option(25, "--limit", min=1, max=500),
    ) -> None:
        """List recent tasks from the journal."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx, lambda client: client.get("/tasks", params={"limit": limit})
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        items = data.get("tasks", [])
        if not items:
            out.info("No tasks recorded yet.")
            return
        out.table(
            "Tasks",
            ["ID", "Kind", "Status", "Profile", "Provider", "Duration", "Created"],
            [
                [
                    item.get("id"),
                    item.get("kind"),
                    item.get("status"),
                    item.get("profile_id") or "",
                    item.get("provider_id") or "",
                    item.get("duration_ms"),
                    item.get("created_at") or "",
                ]
                for item in items
            ],
        )

    @app.command()
    def task(
        ctx: typer.Context,
        task_id: str = typer.Argument(...),
    ) -> None:
        """Show one task, including its result or error."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get(f"/tasks/{task_id}"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.key_values(
            f"Task {data.get('id')}",
            {
                "Kind": data.get("kind"),
                "Status": data.get("status"),
                "Profile": data.get("profile_id") or "",
                "Provider": data.get("provider_id") or "",
                "Duration": f"{data.get('duration_ms')} ms" if data.get("duration_ms") else "",
                "Error": data.get("error") or "",
            },
        )
        if data.get("result") is not None:
            out.result(data["result"])

    @app.command()
    def events(
        ctx: typer.Context,
        limit: int = typer.Option(50, "--limit", min=1, max=500),
    ) -> None:
        """Show recent in-memory events."""
        cli = get_context(ctx)
        data = run_with_client(
            ctx, lambda client: client.get("/events/history", params={"limit": limit})
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        items: list[dict[str, Any]] = data.get("events", [])
        if not items:
            out.info("No events recorded yet.")
            return
        out.table(
            "Events",
            ["Time", "Type", "Data"],
            [
                [item.get("timestamp"), item.get("type"), _compact(item.get("data"))]
                for item in items
            ],
        )


def _compact(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    return ", ".join(f"{key}={value}" for key, value in list(data.items())[:4])


def _follow(path) -> None:
    position = path.stat().st_size if path.is_file() else 0
    typer.echo("Following daemon log (Ctrl+C to stop)…")
    try:
        while True:
            if path.is_file():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    chunk = handle.read()
                    position = handle.tell()
                if chunk:
                    typer.echo(chunk, nl=False)
            time.sleep(0.5)
    except KeyboardInterrupt:
        raise typer.Exit() from None
