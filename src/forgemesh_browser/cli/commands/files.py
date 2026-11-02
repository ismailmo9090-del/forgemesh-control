"""Uploads, downloads, asset manifest, OCR and visual matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from forgemesh_browser.cli.client import run_with_client
from forgemesh_browser.cli.interactive import export_file, write_output
from forgemesh_browser.cli.output import Output, get_context, resolve_profile


def register(app: typer.Typer) -> None:
    @app.command()
    def upload(
        ctx: typer.Context,
        file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
        selector: str | None = typer.Option(
            None, "--selector", "-s", help="File input or attach button."
        ),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Upload a local file to the current page."""
        cli, prof = _profile(ctx, profile)
        data = run_with_client(
            ctx,
            lambda client: client.upload(
                "/media/upload", file, profile=prof, selector=selector, tab_id=tab
            ),
        )
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Uploaded {data.get('name')} ({data.get('size')} bytes)")

    @app.command()
    def download(
        ctx: typer.Context,
        output: Path | None = typer.Option(
            None, "--output", "-o", help="File or directory to copy the download into."
        ),
        timeout: float = typer.Option(60.0, "--timeout", min=1.0, max=600.0),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """Wait for the next download triggered in the page."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {"timeout": timeout}
        if prof:
            payload["profile"] = prof
        data = run_with_client(
            ctx, lambda client: client.post("/media/download/wait", payload)
        )
        if output is not None:
            target = _export_any(Path(data["path"]), output)
            data = {**data, "path": str(target), "stored_path": data["path"]}
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.success(f"Downloaded: {data['path']} ({data.get('size', 0)} bytes)")
            if data.get("sha256"):
                out.info(f"[dim]sha256 {data['sha256']}[/dim]")

    @app.command()
    def assets(
        ctx: typer.Context,
        kind: str | None = typer.Option(None, "--kind", help="screenshot, download, upload, result…"),
        limit: int = typer.Option(50, "--limit", min=1, max=500),
        profile: str | None = typer.Option(None, "--profile", "-p"),
    ) -> None:
        """List recorded assets from the manifest."""
        cli, prof = _profile(ctx, profile)
        params: dict[str, Any] = {"limit": limit}
        if kind:
            params["kind"] = kind
        if prof:
            params["profile"] = prof
        data = run_with_client(ctx, lambda client: client.get("/media", params=params))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        items = data.get("assets", [])
        if not items:
            out.info("No assets recorded yet.")
            return
        out.table(
            "Assets",
            ["ID", "Kind", "Name", "Size", "Provider", "Created"],
            [
                [
                    item.get("id"),
                    item.get("kind"),
                    item.get("original_name"),
                    item.get("size"),
                    item.get("provider_id") or "",
                    item.get("created_at") or "",
                ]
                for item in items
            ],
        )

    @app.command()
    def asset(
        ctx: typer.Context,
        asset_id: str = typer.Argument(...),
        output: Path | None = typer.Option(None, "--output", "-o"),
    ) -> None:
        """Download one recorded asset by ID."""
        cli = get_context(ctx)
        content = run_with_client(
            ctx, lambda client: client.get_bytes(f"/media/{asset_id}")
        )
        target = output or Path.cwd() / f"asset-{asset_id}"
        target = target.expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        out = Output(cli)
        if cli.json_mode:
            out.result({"asset_id": asset_id, "path": str(target), "size": len(content)})
        else:
            out.success(f"Saved asset {asset_id} to {target} ({len(content)} bytes)")

    @app.command()
    def ocr(
        ctx: typer.Context,
        image: Path = typer.Argument(..., exists=True, dir_okay=False),
        language: str | None = typer.Option(None, "--lang", help="Tesseract language, e.g. eng."),
        output: Path | None = typer.Option(None, "--output", "-o", help="Write text here."),
    ) -> None:
        """Extract text from an image with Tesseract."""
        cli = get_context(ctx)
        payload: dict[str, Any] = {"path": str(image.resolve())}
        if language:
            payload["language"] = language
        data = run_with_client(ctx, lambda client: client.post("/ocr", payload))
        if output is not None:
            write_output(output, data.get("text", ""))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
        else:
            out.text(data.get("text", ""))
            out.info(f"[dim]{data.get('characters', 0)} characters[/dim]")

    @app.command()
    def locate(
        ctx: typer.Context,
        image: Path = typer.Option(..., "--image", "-i", exists=True, dir_okay=False),
        screenshot: Path | None = typer.Option(
            None, "--screenshot", help="Search this image (default: current page)."
        ),
        min_confidence: float = typer.Option(0.75, "--min-confidence", min=0.1, max=1.0),
        max_results: int = typer.Option(5, "--max-results", min=1, max=20),
        output: Path | None = typer.Option(None, "--output", "-o", help="Copy annotated PNG here."),
        profile: str | None = typer.Option(None, "--profile", "-p"),
        tab: str | None = typer.Option(None, "--tab"),
    ) -> None:
        """Find a reference image inside a screenshot (template matching)."""
        cli, prof = _profile(ctx, profile)
        payload: dict[str, Any] = {
            "reference": str(image.resolve()),
            "min_confidence": min_confidence,
            "max_results": max_results,
            "tab_id": tab,
        }
        if screenshot is not None:
            payload["screenshot"] = str(screenshot.resolve())
        if prof:
            payload["profile"] = prof
        data = run_with_client(ctx, lambda client: client.post("/locate", payload))
        overlay = data.get("annotated")
        if output is not None and overlay:
            copied = export_file(overlay, output)
            data = {**data, "annotated": str(copied)}
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        matches = data.get("matches", [])
        if not matches:
            out.warning("No matches above the confidence threshold.")
            return
        out.table(
            "Matches",
            ["#", "X", "Y", "W", "H", "Confidence", "Scale"],
            [
                [
                    index,
                    match.get("x"),
                    match.get("y"),
                    match.get("width"),
                    match.get("height"),
                    match.get("confidence"),
                    match.get("scale"),
                ]
                for index, match in enumerate(matches, start=1)
            ],
        )
        if data.get("annotated"):
            out.info(f"[dim]annotated: {data['annotated']}[/dim]")

    @app.command()
    def compare(
        ctx: typer.Context,
        first: Path = typer.Argument(..., exists=True, dir_okay=False),
        second: Path = typer.Argument(..., exists=True, dir_okay=False),
        output: Path | None = typer.Option(
            None, "--output", "-o", help="Write the difference image here."
        ),
    ) -> None:
        """Compare two images of the same size (pixel difference)."""
        cli = get_context(ctx)
        payload: dict[str, Any] = {
            "first": str(first.resolve()),
            "second": str(second.resolve()),
        }
        if output is not None:
            payload["output"] = str(output.expanduser())
        data = run_with_client(ctx, lambda client: client.post("/compare", payload))
        out = Output(cli)
        if cli.json_mode:
            out.result(data)
            return
        if not data.get("same_size"):
            out.error(
                "Images differ in size, so no pixel comparison was possible.",
                details=data,
            )
            raise SystemExit(1)
        if data.get("identical"):
            out.success("Images are pixel-identical.")
            return
        out.info(
            f"Changed pixels: {data.get('diff_ratio')} • bounding box: {data.get('bbox')}"
        )
        if data.get("diff_image"):
            out.info(f"[dim]diff image: {data['diff_image']}[/dim]")


def _profile(ctx: typer.Context, profile: str | None):
    cli = get_context(ctx)
    return cli, resolve_profile(cli, profile)


def _export_any(source: Path, output: Path) -> Path:
    dest = output.expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    if dest.is_dir() or str(output).endswith(("/", "\\")):
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest / source.name
    return export_file(source, dest)
