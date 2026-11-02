"""Path containment and filename sanitisation."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgemesh_browser.core.exceptions import SecurityError
from forgemesh_browser.security.path_validation import (
    ensure_output_path,
    is_within,
    safe_join,
    sanitize_filename,
    validate_user_file,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/tmp/report.pdf", "report.pdf"),
        ("a\x00b.txt", "a_b.txt"),
        ("bad:name?.txt", "bad_name_.txt"),
        (".hidden.", "hidden"),
        ("CON.txt", "_CON.txt"),
        ("", "file"),
        ("..", "file"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates() -> None:
    assert len(sanitize_filename("x" * 500)) == 180


def test_is_within(tmp_path: Path) -> None:
    inside = tmp_path / "nested" / "file.txt"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    assert is_within(inside, tmp_path)
    assert not is_within(outside, tmp_path)
    assert is_within(tmp_path, tmp_path)


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    with pytest.raises(SecurityError):
        safe_join(tmp_path, "..", "escape.txt")
    assert safe_join(tmp_path, "ok", "file.txt") == tmp_path / "ok" / "file.txt"


def test_validate_user_file_rules(tmp_path: Path) -> None:
    real = tmp_path / "data.txt"
    real.write_text("hello", encoding="utf-8")

    assert validate_user_file(real) == real.resolve()

    with pytest.raises(SecurityError):
        validate_user_file(tmp_path / "missing.txt")

    with pytest.raises(SecurityError):
        validate_user_file(tmp_path)

    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with pytest.raises(SecurityError):
        validate_user_file(link)

    with pytest.raises(SecurityError):
        validate_user_file(real, max_bytes=2)


def test_validate_user_file_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "relative.txt"
    real.write_text("hi", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert validate_user_file("relative.txt") == real.resolve()


def test_ensure_output_path_confines_relative(tmp_path: Path) -> None:
    base = tmp_path / "results"
    target = ensure_output_path("sub/out?.txt", base=base)
    assert target == base / "sub" / "out_.txt"
    assert target.parent.is_dir()

    with pytest.raises(SecurityError):
        ensure_output_path("../escape/fine.txt", base=base)

    absolute = ensure_output_path(tmp_path / "abs" / "out?.txt", base=base)
    assert absolute == tmp_path / "abs" / "out_.txt"
