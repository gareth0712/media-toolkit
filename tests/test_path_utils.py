"""Unit tests for ``media_toolkit.path_utils``."""

from __future__ import annotations

from pathlib import Path

from media_toolkit.path_utils import normalize_path_input


def test_translates_windows_backslash_path() -> None:
    assert normalize_path_input(r"D:\Downloads\wordup") == Path(
        "/mnt/d/Downloads/wordup"
    )


def test_translates_windows_forwardslash_path() -> None:
    assert normalize_path_input("D:/Downloads/wordup") == Path(
        "/mnt/d/Downloads/wordup"
    )


def test_translates_lowercase_drive_letter() -> None:
    assert normalize_path_input(r"c:\users\me") == Path("/mnt/c/users/me")


def test_translates_uppercase_drive_letter() -> None:
    assert normalize_path_input(r"C:\Users\me") == Path("/mnt/c/Users/me")


def test_translates_drive_root_only() -> None:
    assert normalize_path_input("D:\\") == Path("/mnt/d/")


def test_passes_through_wsl_mount_path() -> None:
    assert normalize_path_input("/mnt/c/users") == Path("/mnt/c/users")


def test_passes_through_linux_absolute_path() -> None:
    assert normalize_path_input("/home/user/foo") == Path("/home/user/foo")


def test_passes_through_relative_path() -> None:
    assert normalize_path_input("relative/sub") == Path("relative/sub")


def test_relative_path_with_backslash_is_normalized() -> None:
    # Defensive: relative path with backslashes (rare) gets flipped.
    assert normalize_path_input("relative\\sub") == Path("relative/sub")


def test_none_input_returns_none() -> None:
    assert normalize_path_input(None) is None


def test_empty_string_returns_none() -> None:
    assert normalize_path_input("") is None


def test_whitespace_only_returns_none() -> None:
    assert normalize_path_input("   ") is None


def test_strips_double_quotes() -> None:
    assert normalize_path_input('"D:\\foo"') == Path("/mnt/d/foo")


def test_strips_single_quotes() -> None:
    assert normalize_path_input("'D:\\foo'") == Path("/mnt/d/foo")


def test_quote_stripping_only_when_both_ends_match() -> None:
    # Embedded quote should not be stripped.
    result = normalize_path_input('weird"path')
    assert result == Path('weird"path')


def test_path_object_input() -> None:
    assert normalize_path_input(Path("/tmp/foo")) == Path("/tmp/foo")
