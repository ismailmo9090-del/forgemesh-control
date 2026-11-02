"""Typer application: global options and command registration."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from forgemesh_browser import __version__
from forgemesh_browser.cli.output import CliContext
from forgemesh_browser.settings import load_settings

app = typer.Typer(
    name="fmb",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "ForgeMesh Browser — terminal-first, AI-ready browser automation.\n\n"
        "Profiles isolate logins, the daemon keeps sessions alive, and every "
        "browser operation is available from the shell or the local API."
    ),
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fmb {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", "-j", help="Machine-readable JSON output."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose diagnostics."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress informational output."),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Profile for this command (default: active profile)."
    ),
    data_dir: Path | None = typer.Option(
        None, "--data-dir", help="Override the data directory (default: XDG data dir)."
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    overrides = {"data_dir": data_dir} if data_dir is not None else {}
    settings = load_settings(**overrides)
    if data_dir is not None:
        os.environ["FMB_DATA_DIR"] = str(settings.data_dir)
    ctx.obj = CliContext(
        settings=settings,
        json_mode=json_output,
        verbose=verbose,
        quiet=quiet,
        profile=profile,
    )
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


from forgemesh_browser.cli.commands import (  # noqa: E402  (registration after app creation)
    accounts,
    ai,
    automation,
    browser,
    daemon,
    files,
    identities,
    profiles,
)

browser.register(app)
profiles.register(app)
accounts.register(app)
identities.register(app)
automation.register(app)
files.register(app)
ai.register(app)
daemon.register(app)
