"""Interactive helpers: confirmations, file export."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer


def confirm(prompt: str, *, yes: bool = False) -> None:
    if yes:
        return
    confirmed = typer.confirm(prompt, default=False)
    if not confirmed:
        typer.echo("Cancelled.")
        raise typer.Exit(1)


def export_file(source: str | Path, output: str | Path | None) -> Path:
    src = Path(source).expanduser()
    if output is None:
        return src
    dest = Path(output).expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def write_output(path: str | Path, content: str) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target
