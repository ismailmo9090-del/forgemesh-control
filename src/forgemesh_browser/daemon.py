"""Daemon entry point (uvicorn). Started by `fmb start` or run with `fmb serve`."""

from __future__ import annotations

import argparse
import ipaddress
import logging

import uvicorn

from forgemesh_browser.api.server import create_app
from forgemesh_browser.settings import load_settings
from forgemesh_browser.utils.logging import configure_logging

logger = logging.getLogger("forgemesh.daemon")


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ForgeMesh Browser daemon")
    parser.add_argument("--host", default=None, help="Bind host (default from settings)")
    parser.add_argument("--port", type=int, default=None, help="Bind port")
    parser.add_argument("--log-level", default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    overrides: dict[str, object] = {}
    if args.host:
        overrides["api_host"] = args.host
    if args.port:
        overrides["api_port"] = args.port
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    settings = load_settings(**overrides)
    configure_logging(settings, json_mode=settings.log_json)

    if not _is_loopback(settings.api_host) and not settings.api_remote:
        raise SystemExit(
            f"Refusing to bind to non-loopback host {settings.api_host!r}. "
            "Set FMB_API_REMOTE=true to allow remote access (token required)."
        )
    app = create_app(settings)
    logger.info(
        "Starting ForgeMesh Browser daemon on %s:%s (data: %s)",
        settings.api_host,
        settings.api_port,
        settings.data_dir,
    )
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
