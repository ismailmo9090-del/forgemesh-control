"""Runnable Linux mesh worker client: ``python -m forgemesh_control.worker_client``.

Pairs with a control server, keeps an authenticated WebSocket open, heartbeats,
and executes assigned tasks by invoking a *locally detected* coding CLI through
the real adapters in :mod:`forgemesh_control.agents` (no invented flags: the
argv conventions are the ones verified in ``opencode.py`` / ``qoder.py``).

Windows is protocol-supported but explicitly UNTESTED: the inventory records
``windows_untested`` and this client has only ever been executed on Linux.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx
import typer
import websockets

from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_control.agents.base import agent_env, first_version
from forgemesh_control.agents.opencode import OpenCodeAgent
from forgemesh_control.agents.qoder import QoderAgent
from forgemesh_control.mesh import MESH_PROTOCOL_VERSION
from forgemesh_control.security import is_loopback

app = typer.Typer(
    name="forgemesh-worker",
    no_args_is_help=True,
    add_completion=False,
    help="ForgeMesh mesh worker: pair, heartbeat and run coding tasks on this machine.",
)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / (
    "forgemesh-worker"
)
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / (
    "forgemesh-worker"
)
TOKEN_FILE = "token"
SERVER_FILE = "server"
DEVICE_KEY_FILE = "device_key"
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

AGENT_BINARIES = {"opencode": "opencode", "qoder": "qodercli"}


class WorkerError(RuntimeError):
    pass


# --------------------------------------------------------------------- secrets
def _config_dir() -> Path:
    return ensure_private_dir(CONFIG_DIR)


def _write_private(name: str, value: str) -> Path:
    directory = _config_dir()
    path = directory / name
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, value.encode("utf-8"))
    finally:
        os.close(handle)
    return secure_file(path)


def _read_private(name: str) -> str | None:
    path = _config_dir() / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _device_key() -> str:
    existing = _read_private(DEVICE_KEY_FILE)
    if existing:
        return existing
    generated = uuid.uuid4().hex + uuid.uuid4().hex[:32]
    _write_private(DEVICE_KEY_FILE, generated)
    return generated


def _token() -> str:
    token = _read_private(TOKEN_FILE)
    if not token:
        raise WorkerError(
            f"No worker token in {_config_dir() / TOKEN_FILE}. Run `register` first."
        )
    return token


# -------------------------------------------------------------------- transport
def _guard_scheme(server: str, *, insecure_http: bool) -> str:
    """Refuse to send the worker token over plain HTTP to a non-loopback host."""
    base = server.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https"):
        raise WorkerError(f"Server URL must be http(s)://, got {server!r}")
    host = parsed.hostname or ""
    if parsed.scheme == "http" and not is_loopback(host) and not insecure_http:
        typer.secho(
            f"WARNING: {base} is plain HTTP to a non-loopback host. Your worker "
            "token would be sent in clear text. Refusing. Use https:// or pass "
            "--insecure-http only on a trusted network.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return base


def _ws_url(base: str) -> str:
    parsed = urlparse(base)
    scheme = {"http": "ws", "https": "wss"}[parsed.scheme]
    return urlunparse(parsed._replace(scheme=scheme))


# ------------------------------------------------------------------- inventory
async def _run(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=agent_env(None),
        )
    except FileNotFoundError:
        return 127, ""
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        return -1, ""
    return process.returncode or 0, raw.decode("utf-8", errors="replace")


def _which_agent(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    path = agent_env(None).get("PATH", "")
    return shutil.which(name, path=path)


async def _detect_agents() -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    for kind, binary_name in AGENT_BINARIES.items():
        path = _which_agent(binary_name)
        entry: dict[str, Any] = {
            "kind": kind,
            "path": path or "",
            "version": "",
            "installed": path is not None,
            # usable would require a real authenticated functional run; we
            # never claim it from a detection pass alone.
            "usable": False,
            "capabilities": {"detected_by": "worker_client", "version_verified": False},
        }
        if path:
            code, text = await _run([path, "--version"])
            version = first_version(text) if code == 0 else ""
            entry["version"] = version
            entry["capabilities"]["version_verified"] = bool(version)
            list_args = ["models"] if kind == "opencode" else ["--list-models"]
            code, text = await _run([path, *list_args], timeout=40)
            if code == 0:
                for line in text.splitlines():
                    stripped = line.strip().lstrip("-*• ").strip()
                    if not stripped or " " in stripped:
                        continue
                    provider, _, model_id = stripped.partition("/")
                    if not model_id:
                        provider, model_id = kind, stripped
                    models.append(
                        {
                            "provider": provider,
                            "model_id": model_id,
                            "display_name": stripped,
                            # Availability is only "detected present in list";
                            # whether it answers is unverified, so: unknown.
                            "availability": "unknown",
                            "source": "worker_cli_list",
                        }
                    )
        agents.append(entry)
    deduped = {(m["provider"], m["model_id"]): m for m in models}
    return {"agents": agents, "models": sorted(deduped.values(), key=lambda m: (m["provider"], m["model_id"]))}  # fmt: skip


def _read_memtotal_kb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


async def collect_inventory(label: str = "") -> dict[str, Any]:
    system = platform.system()
    os_family = {
        "Linux": "linux",
        "Windows": "windows",
        "Darwin": "darwin",
        "FreeBSD": "freebsd",
    }.get(system, "unknown")
    mem_kb = _read_memtotal_kb() if os_family == "linux" else None
    home = Path.home()
    try:
        usage = shutil.disk_usage(home)
        disk = {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError as exc:
        disk = {"error": str(exc)}
    detected = await _detect_agents()
    cpu_count = os.cpu_count()
    hostname = platform.node() or "unknown-host"
    return {
        "device_key": _device_key(),
        "name": label or hostname,
        "os_family": os_family,
        "os_version": platform.release() or platform.version() or "unknown",
        "arch": platform.machine() or "unknown",
        "protocol_version": MESH_PROTOCOL_VERSION,
        "cpu_count": cpu_count,
        "inventory": {
            "hostname": hostname,
            "python": platform.python_version(),
            "cpu_count": cpu_count,
            "ram_total_bytes": mem_kb * 1024 if mem_kb else None,
            "ram_source": "/proc/meminfo" if mem_kb else "unavailable",
            "disk": disk,
            "windows_untested": os_family == "windows",
            "collected_at": datetime.now(UTC).isoformat(),
        },
        "agents": detected["agents"],
        "models": detected["models"],
    }


# -------------------------------------------------------------------- execution
def _agent_for(kind: str, path: str, timeout_seconds: int):
    if kind == "opencode":
        return OpenCodeAgent(Path(path), timeout_seconds=timeout_seconds)
    if kind == "qoder":
        return QoderAgent(Path(path), timeout_seconds=timeout_seconds)
    return None


async def _pick_agent(
    agent_kind: str, inventory: dict[str, Any]
) -> tuple[str, str] | None:
    agents = {item["kind"]: item for item in inventory["agents"]}
    order = [agent_kind] if agent_kind in agents else []
    order += [kind for kind in AGENT_BINARIES if kind not in order]
    for kind in order:
        entry = agents.get(kind)
        if entry and entry["installed"] and _which_agent(AGENT_BINARIES.get(kind, "")):
            path = entry["path"] or _which_agent(AGENT_BINARIES[kind]) or ""
            if path:
                return kind, path
    return None


async def execute_task(
    send: Any, task: dict[str, Any], timeout_seconds: int
) -> None:
    task_id = task.get("id") or ""
    workspace = ensure_private_dir(DATA_DIR / "workspaces" / re.sub(r"[^A-Za-z0-9._-]", "_", task_id)[:40])
    inventory = await collect_inventory()
    detected = await _pick_agent(str(task.get("agent_kind") or ""), inventory)
    if not detected:
        await send(
            {
                "type": "task_result",
                "task_id": task_id,
                "status": "failed",
                "output": "",
                "error": "no_agent_available",
            }
        )
        return
    kind, path = detected
    agent = _agent_for(kind, path, timeout_seconds)
    if agent is None:
        await send(
            {
                "type": "task_result",
                "task_id": task_id,
                "status": "failed",
                "output": "",
                "error": f"unsupported agent kind {kind!r}",
            }
        )
        return
    await send(
        {
            "type": "task_progress",
            "task_id": task_id,
            "percent": 5,
            "message": f"starting {kind} in {workspace}",
        }
    )
    prompt = str(task.get("prompt") or task.get("description") or "")
    model = str(task.get("model_id") or "") or None
    try:
        result = await agent.submit_task(prompt, cwd=workspace, model=model)
    except Exception as exc:  # noqa: BLE001 - report the real crash to the server
        await send(
            {
                "type": "task_result",
                "task_id": task_id,
                "status": "failed",
                "output": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return
    output = result.text
    if len(output) > 190_000:
        output = output[:190_000] + "\n… output truncated by worker at 190000 chars"
    await send(
        {
            "type": "task_result",
            "task_id": task_id,
            "status": "succeeded" if result.ok else "failed",
            "output": output,
            "error": result.error
            or (None if result.ok else f"{kind} exited with code {result.exit_code}"),
        }
    )


# ---------------------------------------------------------------------- CLI
@app.command()
def register(
    server: str = typer.Option(..., "--server", help="Control server base URL."),
    code: str = typer.Option(..., "--code", help="Pairing code from the dashboard."),
    name: str = typer.Option("", "--name", help="Human label for this worker."),
    insecure_http: bool = typer.Option(
        False, "--insecure-http", help="Allow plain HTTP to a non-loopback host (unsafe)."
    ),
) -> None:
    """Redeem a pairing code, store the returned token (0600) and the inventory."""
    base = _guard_scheme(server, insecure_http=insecure_http)

    async def _go() -> dict[str, Any]:
        payload = await collect_inventory(name)
        payload["pairing_code"] = code.strip().upper()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{base}/api/mesh/register", json=payload)
        if response.status_code != 200:
            raise WorkerError(f"Registration failed ({response.status_code}): {response.text}")
        return response.json()

    try:
        data = asyncio.run(_go())
    except (WorkerError, httpx.HTTPError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    token = data.get("token")
    if token:
        _write_private(TOKEN_FILE, token)
    _write_private(SERVER_FILE, base)
    typer.echo(f"paired: worker_id={data['worker_id']}")
    if token:
        typer.echo(f"token stored in {_config_dir() / TOKEN_FILE} (0600, shown once, not repeated)")
    else:
        typer.echo(data.get("token_note", "no token issued"))


@app.command()
def run(
    server: str = typer.Option("", "--server", help="Control server URL (default: stored)."),
    insecure_http: bool = typer.Option(
        False, "--insecure-http", help="Allow plain ws:// to a non-loopback host (unsafe)."
    ),
    task_timeout_seconds: int = typer.Option(1800, "--task-timeout", min=30),
) -> None:
    """Keep an authenticated WS open: heartbeat, claim and execute assigned tasks."""
    base = _guard_scheme(server or _read_private(SERVER_FILE) or "", insecure_http=insecure_http)
    token = _token()
    url = f"{_ws_url(base)}/ws/worker?worker_token={quote(token)}"

    async def _session() -> None:
        async with websockets.connect(url, open_timeout=30, ping_interval=20) as ws:
            send_lock = asyncio.Lock()

            async def send(message: dict[str, Any]) -> None:
                # Serialise writes: heartbeats and task frames share this socket.
                async with send_lock:
                    await ws.send(json.dumps(message))

            heartbeat_task: asyncio.Task[None] | None = None
            running: set[asyncio.Task[None]] = set()
            state = {"max_tasks": 1}

            async def request_claim() -> None:
                if len(running) < state["max_tasks"]:
                    await send({"type": "claim"})

            async def heartbeater(interval: float) -> None:
                while True:
                    await asyncio.sleep(interval)
                    await send({"type": "heartbeat"})

            async for raw in ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "hello":
                    await send(
                        {
                            "type": "hello",
                            "protocol_version": MESH_PROTOCOL_VERSION,
                            "device_key": _device_key(),
                        }
                    )
                    interval = float(message.get("heartbeat_seconds") or 15)
                    state["max_tasks"] = int(message.get("max_concurrent_tasks") or 1)
                    heartbeat_task = asyncio.create_task(heartbeater(interval))
                    typer.echo(f"connected as {message.get('worker_id')}")
                    await request_claim()
                elif kind == "heartbeat_ack":
                    pending = (message.get("data") or {}).get("tasks_pending") or 0
                    if pending > 0:
                        await request_claim()
                elif kind == "assignment":
                    task = message.get("task")
                    if task:
                        typer.echo(f"task {task.get('id')} assigned; executing")
                        exec_task = asyncio.create_task(
                            execute_task(send, task, task_timeout_seconds)
                        )
                        running.add(exec_task)
                        exec_task.add_done_callback(running.discard)
                        await request_claim()
                elif kind == "error":
                    if message.get("code") == "protocol_mismatch":
                        raise WorkerError(
                            f"protocol mismatch: {message.get('message')}"
                        )
                    typer.secho(f"server error: {message}", fg=typer.colors.YELLOW, err=True)

            if heartbeat_task:
                heartbeat_task.cancel()

    typer.echo(f"worker loop for {base} running; Ctrl-C to stop")
    attempt = 0
    while True:
        try:
            asyncio.run(_session())
            attempt = 0
        except KeyboardInterrupt:
            break
        except WorkerError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            if "protocol mismatch" in str(exc):
                break
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            typer.secho(f"connection lost: {exc}", fg=typer.colors.YELLOW, err=True)
        delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
        attempt += 1
        typer.echo(f"reconnecting in {delay:.0f}s…")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            break


@app.command()
def status(
    server: str = typer.Option("", "--server", help="Control server URL (default: stored)."),
    insecure_http: bool = typer.Option(False, "--insecure-http"),
) -> None:
    """Show this machine's real inventory and the server's view of this worker."""

    async def _go() -> dict[str, Any]:
        local = await collect_inventory()
        out: dict[str, Any] = {"local": local}
        base = ""
        try:
            base = _guard_scheme(
                server or _read_private(SERVER_FILE) or "", insecure_http=insecure_http
            )
        except (WorkerError, typer.Exit):
            base = ""
        if base and (CONFIG_DIR / TOKEN_FILE).is_file():
            token = _read_private(TOKEN_FILE) or ""
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(
                        f"{base}/api/mesh/me", headers={"X-Worker-Token": token}
                    )
                out["server"] = (
                    response.json() if response.status_code == 200 else {"error": response.text}
                )
            except httpx.HTTPError as exc:
                out["server"] = {"error": f"server unreachable: {exc}"}
        else:
            out["server"] = {"error": "not paired (no stored token/server)"}
        return out

    typer.echo(json.dumps(asyncio.run(_go()), indent=2, default=str))


def main() -> None:  # pragma: no cover
    try:
        app()
    except WorkerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
