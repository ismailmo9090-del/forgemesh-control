"""Git workspaces on the central server.

Everything here shells out to the real ``git`` binary and reports what it
actually returned. A repository that was never created is reported as such -
the dashboard never gets a synthetic "clean" status.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from forgemesh_browser.security.permissions import ensure_private_dir
from forgemesh_control.config import ControlSettings
from forgemesh_control.security import ensure_within, safe_slug

logger = logging.getLogger("forgemesh.control.workspaces")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "ForgeMesh Control",
    "GIT_AUTHOR_EMAIL": "control@forgemesh.local",
    "GIT_COMMITTER_NAME": "ForgeMesh Control",
    "GIT_COMMITTER_EMAIL": "control@forgemesh.local",
}
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
_REMOTE_RE = re.compile(r"^(https://|git@)[^\s]+$")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


def validate_remote(remote: str) -> str:
    """Accept only public https:// or git@ remotes.

    The clone runs on the control server, so a loopback, link-local or private
    address would let a project setting reach local services and cloud metadata
    endpoints. Credentials embedded in the URL are refused as well.
    """
    value = (remote or "").strip()
    if not _REMOTE_RE.match(value):
        raise WorkspaceError(
            "Only https:// or git@ remotes are accepted; no shell is invoked with the URL."
        )
    if value.startswith("git@"):
        host = value[4:].split(":", 1)[0]
    else:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise WorkspaceError("Only https:// or git@ remotes are accepted.")
        if parsed.username or parsed.password:
            raise WorkspaceError("Remote URLs must not embed credentials.")
        host = parsed.hostname or ""
    host = host.strip("[]").lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS or host.endswith(".local"):
        raise WorkspaceError(f"Remote host {host or '(empty)'} is not allowed.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise WorkspaceError(f"Remote address {host} is private or reserved.")
    return value


class WorkspaceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "workspace_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


async def git_available() -> tuple[bool, str]:
    path = shutil.which("git")
    if path is None:
        return False, "git is not installed on this machine"
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except (OSError, TimeoutError) as exc:
        return False, f"git is not runnable: {exc}"
    return process.returncode == 0, stdout.decode(errors="replace").strip()


async def _run_git(
    args: list[str], *, cwd: Path, timeout: int = 120
) -> tuple[int, str, str | None]:
    binary = shutil.which("git")
    if binary is None:
        return 127, "", "git is not installed on this machine"
    env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", **GIT_IDENTITY}
    import os

    merged = {**os.environ, **env}
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            cwd=str(cwd),
            env=merged,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        return 124, "", f"`git {' '.join(args)}` timed out after {timeout}s"
    except OSError as exc:
        return 125, "", f"`git {' '.join(args)}` could not start: {exc}"
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip() or None,
    )


class WorkspaceService:
    def __init__(self, settings: ControlSettings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[bool, str]] = {}

    # ---------------------------------------------------------------------- roots
    def resolve(self, raw_path: str) -> Path:
        if not raw_path:
            raise WorkspaceError("No workspace path is recorded for this project.")
        # Always resolve: a lexical prefix check accepts "root/../../etc" and
        # symlinks that point outside the workspace root.
        try:
            return ensure_within(self.settings.workspaces, Path(raw_path))
        except Exception as exc:  # noqa: BLE001 - surface as a workspace error
            raise WorkspaceError(str(exc)) from exc

    def branch_name(self, task_id: str, title: str) -> str:
        return f"forgemesh/{safe_slug(task_id, max_length=24)}-{safe_slug(title, max_length=30)}"

    async def _git_lines(self, args: list[str], *, cwd: Path) -> list[str]:
        code, out, _error = await _run_git(args, cwd=cwd)
        if code != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------------------ lifecycle
    async def probe_git(self) -> dict[str, Any]:
        if "git" not in self._cache:
            self._cache["git"] = await git_available()
        ok, detail = self._cache["git"]
        return {"available": ok, "detail": detail, "path": shutil.which("git")}

    async def init_repo(self, raw_path: str, *, default_branch: str = "main") -> dict[str, Any]:
        git_check = await self.probe_git()
        if not git_check["available"]:
            raise WorkspaceError(git_check["detail"] or "git is unavailable")
        path = self.resolve(raw_path)
        ensure_private_dir(path)
        if (path / ".git").is_dir():
            return await self.status(path, required=False) | {
                "created": False,
                "path": str(path),
            }
        code, out, error = await _run_git(["init", "-b", default_branch, str(path)], cwd=path)
        if code != 0:
            code, out, error = await _run_git(["init", str(path)], cwd=path)
            if code == 0:
                default_branch = await self.current_branch(path) or default_branch
        if code != 0:
            raise WorkspaceError(f"git init failed: {error or out}")
        payload = await self.status(path, required=False)
        payload.update({"created": True, "path": str(path), "branch": default_branch})
        return payload

    async def clone(self, remote: str, raw_path: str) -> dict[str, Any]:
        remote = validate_remote(remote)
        git_check = await self.probe_git()
        if not git_check["available"]:
            raise WorkspaceError(git_check["detail"] or "git is unavailable")
        path = self.resolve(raw_path)
        if path.exists() and any(path.iterdir()):
            raise WorkspaceError(f"Workspace {path} is not empty; refusing to clone over it.")
        path.parent.mkdir(parents=True, exist_ok=True)
        code, out, error = await _run_git(
            ["clone", remote, str(path)], cwd=path.parent, timeout=900
        )
        if code != 0:
            raise WorkspaceError(f"git clone failed: {error or out}")
        return {
            "cloned": True,
            "path": str(path),
            "remote": remote,
            "branch": await self.current_branch(path),
            "output": out[:2000],
        }

    async def current_branch(self, path: Path) -> str | None:
        code, out, _ = await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        return out if code == 0 else None

    async def create_branch(
        self, raw_path: str, branch: str, *, base: str | None = None
    ) -> dict[str, Any]:
        if not _SAFE_BRANCH.match(branch):
            raise WorkspaceError(f"Unsafe branch name: {branch!r}")
        path = self.resolve(raw_path)
        if base and not _SAFE_BRANCH.match(base):
            raise WorkspaceError(f"Unsafe base branch: {base!r}")
        if base:
            code, _, error = await _run_git(["checkout", base], cwd=path)
            if code != 0:
                raise WorkspaceError(f"Could not check out {base}: {error}")
        code, out, error = await _run_git(["checkout", "-b", branch], cwd=path)
        if code != 0 and "already exists" in (error or "").lower():
            code, out, error = await _run_git(["checkout", branch], cwd=path)
            if code != 0:
                raise WorkspaceError(f"Could not switch to {branch}: {error}")
            return {"branch": branch, "created": False, "output": out}
        if code != 0:
            raise WorkspaceError(f"Could not create {branch}: {error or out}")
        return {"branch": branch, "created": True, "output": out}

    async def status(self, raw_path: Any, *, required: bool = True) -> dict[str, Any]:
        path = raw_path if isinstance(raw_path, Path) else self.resolve(str(raw_path))
        if not (path / ".git").is_dir():
            payload = {
                "is_repo": False,
                "path": str(path),
                "branch": None,
                "clean": None,
                "changes": [],
                "error": f"{path} is not a git repository",
            }
            if required:
                raise WorkspaceError(payload["error"], code="not_a_repo")
            return payload
        code, porcelain, error = await _run_git(["status", "--porcelain=v1"], cwd=path)
        if code != 0:
            if required:
                raise WorkspaceError(f"git status failed: {error}")
            return {"is_repo": True, "path": str(path), "error": error, "changes": []}
        changes = [line for line in porcelain.splitlines() if line.strip()]
        commit_code, head, _ = await _run_git(["rev-parse", "--short", "HEAD"], cwd=path)
        ahead_behind = None
        code, tracking, _ = await _run_git(
            ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=path
        )
        if code == 0 and tracking:
            behind, _, ahead = tracking.partition("\t")
            ahead_behind = {"ahead": int(ahead or 0), "behind": int(behind or 0)}
        return {
            "is_repo": True,
            "path": str(path),
            "branch": await self.current_branch(path),
            "head": head if commit_code == 0 else None,
            "clean": not changes,
            "changes": changes[:200],
            "upstream": ahead_behind,
        }

    async def diff_stat(self, raw_path: str) -> dict[str, Any]:
        path = self.resolve(raw_path)
        code, out, error = await _run_git(["diff", "--stat", "HEAD"], cwd=path)
        if code != 0:
            raise WorkspaceError(f"git diff failed: {error or out}")
        names = await self._git_lines(["diff", "--name-only", "HEAD"], cwd=path)
        return {"path": str(path), "stat": out, "files": len(names), "changed_files": names}

    async def last_commit_files(self, raw_path: str) -> list[str]:
        """Files introduced or changed by HEAD - what a finished task actually made."""
        path = self.resolve(raw_path)
        return await self._git_lines(
            ["show", "--pretty=format:", "--name-only", "HEAD"], cwd=path
        )

    async def commit(
        self, raw_path: str, message: str, *, files: list[str] | None = None
    ) -> dict[str, Any]:
        if not message.strip():
            raise WorkspaceError("A commit message is required.")
        path = self.resolve(raw_path)
        # `--` would turn "-A" into a pathspec, so the flag goes before it.
        args = ["add", "-A"] if not files else ["add", "--", *files]
        code, out, error = await _run_git(args, cwd=path)
        if code != 0:
            raise WorkspaceError(f"git add failed: {error or out}")
        code, out, error = await _run_git(["commit", "-m", message], cwd=path)
        if code != 0:
            if "nothing to commit" in (out + (error or "")).lower():
                return {"committed": False, "reason": "nothing to commit", "output": out}
            raise WorkspaceError(f"git commit failed: {error or out}")
        sha_code, sha, _ = await _run_git(["rev-parse", "HEAD"], cwd=path)
        return {
            "committed": True,
            "commit": sha if sha_code == 0 else None,
            "output": out[:2000],
        }

    async def merge(
        self, raw_path: str, branch: str, *, into: str | None = None, squash: bool = False
    ) -> dict[str, Any]:
        if not _SAFE_BRANCH.match(branch):
            raise WorkspaceError(f"Unsafe branch name: {branch!r}")
        path = self.resolve(raw_path)
        target = into or await self.current_branch(path)
        if not target:
            raise WorkspaceError("No branch to merge into.")
        args = ["merge", "--no-edit", branch]
        if squash:
            args.insert(1, "--squash")
        code, out, error = await _run_git(args, cwd=path)
        result = {
            "merged": code == 0,
            "branch": branch,
            "into": target,
            "output": (out or error or "")[:4000],
            "exit_code": code,
        }
        if code != 0:
            # Leave the tree exactly as the operator can inspect it: an aborted
            # merge is reported, never silently rolled back.
            result["conflict_state"] = await self.status(path, required=False)
        return result

    async def log(self, raw_path: str, limit: int = 20) -> list[dict[str, Any]]:
        path = self.resolve(raw_path)
        code, out, error = await _run_git(
            [
                "log",
                f"--max-count={max(1, min(limit, 100))}",
                "--pretty=format:%H%x1f%an%x1f%ad%x1f%s",
                "--date=iso-strict",
            ],
            cwd=path,
        )
        if code != 0:
            if "does not have any commits" in (error or "").lower():
                return []
            raise WorkspaceError(f"git log failed: {error or out}")
        entries = []
        for line in out.splitlines():
            fields = line.split("\x1f")
            if len(fields) == 4:
                entries.append(
                    {"commit": fields[0], "author": fields[1], "date": fields[2], "subject": fields[3]}
                )
        return entries
