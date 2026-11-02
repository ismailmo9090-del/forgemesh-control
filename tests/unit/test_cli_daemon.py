"""`fmb shutdown` must only kill a process it can positively identify."""

from __future__ import annotations

from forgemesh_browser.cli.client import _cmdline_is_daemon

VENV = "/home/user/projects/app/.venv/bin"


def test_module_form_is_the_daemon() -> None:
    assert _cmdline_is_daemon(["/usr/bin/python3", "-m", "forgemesh_browser.daemon"])


def test_entry_point_form_is_the_daemon() -> None:
    assert _cmdline_is_daemon([f"{VENV}/python", f"{VENV}/fmb", "serve"])


def test_other_commands_and_unrelated_processes_are_not() -> None:
    assert not _cmdline_is_daemon([f"{VENV}/python", f"{VENV}/fmb", "status"])
    assert not _cmdline_is_daemon(["/usr/bin/python3", "serve.py"])
    assert not _cmdline_is_daemon(["/usr/bin/chrome", "--type=renderer"])
    assert not _cmdline_is_daemon([])
