"""Console entry point: ``fmc``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from forgemesh_control import __version__
from forgemesh_control.config import load_control_settings

app = typer.Typer(
    name="fmc",
    no_args_is_help=True,
    add_completion=False,
    help="ForgeMesh Control Center — central server dashboard and agent management.",
)
agents_app = typer.Typer(name="agents", help="Central OpenCode/Qoder CLI management.")
setup_app = typer.Typer(name="setup", help="First-run setup helpers.")
app.add_typer(agents_app)
app.add_typer(setup_app)


def _echo(data: Any, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (default 127.0.0.1)."),
    port: int | None = typer.Option(None, help="Bind port (default 8787)."),
) -> None:
    """Run the control server and dashboard."""
    import uvicorn

    settings = load_control_settings(**{k: v for k, v in {"host": host, "port": port}.items() if v})
    uvicorn.run(
        "forgemesh_control.api.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


@app.command()
def token() -> None:
    """Print the local control token path and value (0600 file)."""
    settings = load_control_settings()
    settings.ensure_dirs()
    from forgemesh_control.security import control_token

    value = control_token(settings.token_path)
    typer.echo(f"path: {settings.token_path}")
    typer.echo(value)


@app.command()
def status(as_json: bool = typer.Option(True, "--json/--pretty")) -> None:
    """Show real server, agent and setup status."""

    async def run() -> dict[str, Any]:
        from forgemesh_control.runtime import ControlRuntime

        settings = load_control_settings(auto_install_agents=False)
        runtime = ControlRuntime(settings)
        await runtime.start()
        try:
            return await runtime.status_snapshot()
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=as_json)


@agents_app.command("list")
def agents_list(as_json: bool = typer.Option(True, "--json/--pretty")) -> None:
    async def run() -> list[dict[str, Any]]:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.list_agents()
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=as_json)


@agents_app.command("detect")
def agents_detect(kind: str | None = typer.Argument(None)) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            if kind:
                return await runtime.agents.detect(kind)
            return await runtime.agents.detect_all()
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@agents_app.command("verify")
def agents_verify(kind: str) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.verify(kind, functional=True)
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@agents_app.command("install")
def agents_install(
    kind: str,
    force: bool = typer.Option(False, "--force", help="Reinstall over an existing binary."),
) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.install(kind, force=force, verify=True)
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@agents_app.command("update")
def agents_update(
    kind: str,
    approve: bool = typer.Option(
        False, "--approve", help="Explicitly approve this update (required)."
    ),
) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.update(kind, approve=approve)
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@agents_app.command("logs")
def agents_logs(kind: str) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.logs(kind)
        finally:
            await runtime.stop()

    result = asyncio.run(run())
    typer.echo(result.get("log") or "(no installation log yet)")


@agents_app.command("path")
def agents_path(kind: str, path: str) -> None:
    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.set_path(kind, path)
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@setup_app.command("run")
def setup_run() -> None:
    """Install missing CLIs, verify both, and print the resulting state."""

    async def run() -> Any:
        from forgemesh_control.runtime import ControlRuntime

        runtime = ControlRuntime(load_control_settings(auto_install_agents=False))
        await runtime.start()
        try:
            return await runtime.agents.first_run_setup()
        finally:
            await runtime.stop()

    _echo(asyncio.run(run()), as_json=True)


@app.command()
def version() -> None:
    typer.echo(f"fmc {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
