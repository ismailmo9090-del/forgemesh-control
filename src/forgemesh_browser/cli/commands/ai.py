"""AI integration: page understanding, multimodal prompts and provider status."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from forgemesh_browser.cli.client import run_with_client
from forgemesh_browser.cli.interactive import write_output
from forgemesh_browser.cli.output import Output, get_context, resolve_profile


def register(app: typer.Typer) -> None:
    @app.command()
    def ai(
        ctx: typer.Context,
        prompt: str = typer.Argument("", help="Instruction or question."),
        image: list[Path] = typer.Option(
            [], "--image", "-i", help="Reference image (repeatable)."
        ),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        page: bool | None = typer.Option(
            None,
            "--page/--no-page",
            help="Include the current page. Default: on for text-only prompts.",
        ),
        provider: str | None = typer.Option(None, "--provider", help="AI provider id."),
        model: str | None = typer.Option(
            None, "--model", "-m", help="Model name ('local' selects Ollama)."
        ),
        output_format: str = typer.Option("text", "--format", help="text or json."),
        output: Path | None = typer.Option(None, "--output", "-o", help="Write the answer here."),
        temperature: float = typer.Option(0.2, "--temperature", min=0.0, max=2.0),
        max_tokens: int = typer.Option(2048, "--max-tokens", min=1, max=32000),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Ask a configured AI model, optionally with page context and images."""
        cli, prof = _profile(ctx, profile)
        if output_format not in {"text", "json"}:
            raise typer.BadParameter("--format must be text or json")
        resolved_model = model
        if model == "local":
            provider = provider or "ollama"
            resolved_model = None
        include_page = page if page is not None else not image
        for item in image:
            if not item.is_file():
                raise typer.BadParameter(f"Image not found: {item}")
        payload: dict[str, Any] = {
            "prompt": prompt,
            "include_page": include_page,
            "images": [str(path.resolve()) for path in image],
            "provider": provider,
            "model": resolved_model,
            "output_format": output_format,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "profile": prof,
            "tab_id": tab,
        }
        data = run_with_client(ctx, lambda client: client.post("/ai", payload))
        text = data.get("text", "")
        target: Path | None = None
        if output is not None:
            target = _write_answer(output, text, output_format)
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        if text:
            out.text(text)
        if output_format == "json" and not data.get("json_valid"):
            out.warning("The model did not return valid JSON.")
        meta = data.get("usage") or {}
        usage = ""
        if isinstance(meta, dict) and meta:
            usage = " • " + ", ".join(f"{k}={v}" for k, v in meta.items())
        out.info(
            f"[dim]{data.get('provider')}/{data.get('model')} "
            f"• task {data.get('task_id')} • {data.get('duration_ms', 0):.0f} ms{usage}[/dim]"
        )
        if target is not None:
            out.success(f"Answer written to {target}")

    @app.command("ai-providers")
    def ai_providers(ctx: typer.Context) -> None:
        """List configured AI backends and their capabilities."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get("/ai/providers"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.table(
            "AI providers",
            ["ID", "Local", "Vision", "Default"],
            [
                [
                    item.get("id"),
                    item.get("local"),
                    item.get("supports_vision"),
                    item.get("default"),
                ]
                for item in data.get("providers", [])
            ],
        )

    @app.command("ai-health")
    def ai_health(ctx: typer.Context) -> None:
        """Probe each configured AI backend."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get("/ai/health"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.result(data)

    @app.command("ai-models")
    def ai_models(
        ctx: typer.Context,
        provider: str | None = typer.Option(None, "--provider"),
    ) -> None:
        """List models exposed by the configured backends."""
        cli = get_context(ctx)
        params = {"provider": provider} if provider else None
        data = run_with_client(ctx, lambda client: client.get("/ai/models", params=params))
        out = Output(cli)
        out.result(data)

    @app.command()
    def providers(ctx: typer.Context) -> None:
        """List browser-automation provider adapters (ChatGPT, Claude, …)."""
        cli = get_context(ctx)
        data = run_with_client(ctx, lambda client: client.get("/providers"))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        out.table(
            "Provider adapters",
            ["ID", "Name", "URL", "Enabled"],
            [
                [
                    item.get("id"),
                    item.get("name"),
                    item.get("url"),
                    item.get("enabled"),
                ]
                for item in data.get("providers", [])
            ],
        )


def _profile(ctx: typer.Context, profile: str | None):
    cli = get_context(ctx)
    return cli, resolve_profile(cli, profile)


def _write_answer(output: Path, text: str, output_format: str) -> Path:
    target = output.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.is_dir() or str(output).endswith(("/", "\\")):
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target / f"ai-{stamp}.{'json' if output_format == 'json' else 'txt'}"
    return write_output(target, text)
