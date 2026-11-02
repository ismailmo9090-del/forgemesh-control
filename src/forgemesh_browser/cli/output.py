"""Human/JSON output helpers and shared CLI context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.pretty import Pretty
from rich.table import Table

from forgemesh_browser.settings import Settings


@dataclass
class CliContext:
    settings: Settings
    json_mode: bool = False
    verbose: bool = False
    quiet: bool = False
    profile: str | None = None


def get_context(ctx: typer.Context) -> CliContext:
    if not isinstance(ctx.obj, CliContext):
        ctx.obj = CliContext(settings=Settings())
    return ctx.obj


def resolve_profile(ctx: CliContext, override: str | None) -> str | None:
    return override or ctx.profile


class Output:
    def __init__(self, ctx: CliContext) -> None:
        self.ctx = ctx
        self.json_mode = ctx.json_mode
        self.console = Console(
            stderr=False, quiet=ctx.quiet or ctx.json_mode, highlight=False
        )
        self.error_console = Console(stderr=True, highlight=False)

    def _json(self, payload: Any) -> None:
        typer.echo(json.dumps(payload, indent=2, default=str))

    def result(self, payload: Any, *, pretty: bool = True) -> None:
        if self.json_mode:
            self._json(payload)
        elif pretty:
            self.console.print(Pretty(payload, overflow="fold"))

    def success(self, message: str) -> None:
        if not self.json_mode and not self.ctx.quiet:
            self.console.print(f"[green]✓[/green] {message}")

    def info(self, message: str) -> None:
        if not self.json_mode and not self.ctx.quiet:
            self.console.print(message, markup=True)

    def text(self, value: str) -> None:
        if not self.json_mode:
            self.console.print(value, markup=False, highlight=False)

    def warning(self, message: str) -> None:
        if not self.ctx.quiet:
            self.error_console.print(f"[yellow]warning:[/yellow] {message}")

    def error(self, message: str, *, code: str | None = None, details: dict | None = None) -> None:
        if self.json_mode:
            self.error_console.print(
                json.dumps(
                    {"error": {"code": code or "error", "message": message, "details": details or {}}},
                    default=str,
                )
            )
        else:
            self.error_console.print(f"[red]error:[/red] {message}", highlight=False)

    def table(self, title: str, columns: list[str], rows: list[list[Any]]) -> None:
        table = Table(title=title, header_style="bold cyan", show_lines=False)
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*["" if value is None else str(value) for value in row])
        self.console.print(table)

    def key_values(self, title: str, values: dict[str, Any]) -> None:
        table = Table(title=title, show_header=False, box=None)
        table.add_column("key", style="bold")
        table.add_column("value", overflow="fold")
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, default=str)
            table.add_row(str(key), "" if value is None else str(value))
        self.console.print(table)
