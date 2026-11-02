"""Element interaction, waiting and declarative workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from forgemesh_browser.cli.client import run_with_client
from forgemesh_browser.cli.output import Output, get_context, resolve_profile
from forgemesh_browser.core.exceptions import ForgeMeshError


def register(app: typer.Typer) -> None:
    @app.command()
    def click(
        ctx: typer.Context,
        target: str = typer.Argument(..., help="Ref (e4), role+name, label, CSS or text."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
        click_count: int = typer.Option(1, "--count", min=1, max=3),
    ) -> None:
        """Click an element. Ambiguous targets fail instead of guessing."""
        _run(ctx, "click", profile, tab, target=target, click_count=click_count)

    @app.command()
    def fill(
        ctx: typer.Context,
        target: str = typer.Argument(...),
        text: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Replace an input's value."""
        _run(ctx, "fill", profile, tab, target=target, text=text)

    @app.command("type")
    def type_text(
        ctx: typer.Context,
        target: str = typer.Argument(...),
        text: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Type into an element key by key."""
        _run(ctx, "type", profile, tab, target=target, text=text)

    @app.command()
    def press(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="Playwright key, e.g. Enter, Tab, Control+A."),
        target: str | None = typer.Option(None, "--target", help="Element to focus first."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Press a keyboard key on the page or an element."""
        _run(ctx, "press", profile, tab, key=key, target=target)

    @app.command()
    def select(
        ctx: typer.Context,
        target: str = typer.Argument(...),
        value: str = typer.Argument(..., help="Option value or visible label."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Choose an option in a <select> element."""
        _run(ctx, "select", profile, tab, target=target, value=value)

    @app.command()
    def check(
        ctx: typer.Context,
        target: str = typer.Argument(...),
        checked: bool = typer.Option(True, "--check/--uncheck"),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Check or uncheck a checkbox/radio element."""
        _run(ctx, "check", profile, tab, target=target, checked=checked)

    @app.command()
    def hover(
        ctx: typer.Context,
        target: str = typer.Argument(...),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Hover over an element."""
        _run(ctx, "hover", profile, tab, target=target)

    @app.command()
    def scroll(
        ctx: typer.Context,
        direction: str = typer.Argument("down", help="down, up, top or bottom."),
        amount: float = typer.Option(1.0, "--amount", min=0.1, max=50.0, help="Viewports."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Scroll the page."""
        _run(ctx, "scroll", profile, tab, direction=direction, amount=amount)

    @app.command()
    def wait(
        ctx: typer.Context,
        seconds: float = typer.Argument(..., min=0.0, max=300.0),
    ) -> None:
        """Pause for a number of seconds."""
        _run(ctx, "wait", None, None, seconds=seconds)

    @app.command("wait-for")
    def wait_for(
        ctx: typer.Context,
        selector: str | None = typer.Option(None, "--selector", "-s"),
        text: str | None = typer.Option(None, "--text", "-t"),
        state: str = typer.Option("visible", "--state", help="visible, attached, hidden."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Wait until a selector or text appears."""
        if bool(selector) == bool(text):
            raise typer.BadParameter("Provide exactly one of --selector or --text.")
        if selector:
            _run(ctx, "wait_for_selector", profile, tab, selector=selector, state=state)
        else:
            _run(ctx, "wait_for_text", profile, tab, text=text)

    @app.command()
    def workflow(
        ctx: typer.Context,
        file: Path = typer.Argument(..., exists=True, dir_okay=False, help="JSON/YAML workflow."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
        name: str = typer.Option("workflow", "--name"),
    ) -> None:
        """Run a declarative workflow file with steps [{action, ...}]."""
        spec = _load_workflow(file)
        _run(
            ctx,
            "workflow",
            profile,
            tab,
            steps=spec,
            workflow_name=name,
        )


def _run(
    ctx: typer.Context,
    action: str,
    profile: str | None,
    tab: str | None,
    **fields: Any,
) -> None:
    cli = get_context(ctx)
    prof = resolve_profile(cli, profile)
    payload: dict[str, Any] = {"action": action, **fields}
    if prof:
        payload["profile"] = prof
    if tab:
        payload["tab_id"] = tab
    data = run_with_client(ctx, lambda client: client.post("/automation/execute", payload))
    out = Output(cli)
    if cli.json_mode:
        out.result(data)
        return
    if action == "wait":
        out.success(f"Waited {fields.get('seconds', 0)}s")
    elif action == "workflow":
        steps = data.get("steps", data.get("completed_steps", []))
        if data.get("status") == "failed":
            out.error(
                f"Workflow failed at step {data.get('failed_step')} "
                f"({data.get('action')}): {data.get('error')}"
            )
            raise SystemExit(1)
        out.success(
            f"Workflow '{data.get('name', 'workflow')}' completed "
            f"({len(steps)} step(s), {data.get('duration_ms', 0):.0f} ms)"
        )
        for step in steps:
            out.info(f"[dim]ok[/dim] {step.get('action')} {step.get('step', '')}")
    else:
        detail = ""
        if data.get("url"):
            detail = f" → {data['url']}"
        strategy = f" [dim]({data['strategy']})[/dim]" if data.get("strategy") else ""
        out.success(f"{action} {fields.get('target') or fields.get('key') or ''}{detail}{strategy}")


def _load_workflow(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ForgeMeshError(f"Cannot read workflow {path}: {exc}") from exc
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list) or not steps:
        raise ForgeMeshError("Workflow must contain a non-empty 'steps' list")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not step.get("action"):
            raise ForgeMeshError(f"Step {index} must be an object with an 'action'")
    return steps
