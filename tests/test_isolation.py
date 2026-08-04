"""Tests for athenaeum.isolation.resolve_under."""

import os
import uuid

import pytest

from athenaeum.isolation import PathEscapeError, UserPaths, resolve_under, validate_user_id


def test_valid_nested_path(tmp_path):
    result = resolve_under(tmp_path, "/tables/customers.md")
    assert result == tmp_path.resolve() / "tables" / "customers.md"


def test_relative_form_and_root(tmp_path):
    assert resolve_under(tmp_path, "a/b.md") == tmp_path.resolve() / "a" / "b.md"
    assert resolve_under(tmp_path, "/") == tmp_path.resolve()


def test_traversal_rejected(tmp_path):
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "../evil.md")
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "a/../../evil.md")
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "..\\evil.md")


def test_os_absolute_rejected(tmp_path):
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "C:/Windows/evil.md")
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "C:\\Windows\\evil.md")
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "//server/share/evil.md")


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "_outside")
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(PathEscapeError):
        resolve_under(tmp_path, "link/evil.md")


def test_user_paths_wrapper(tmp_path):
    paths = UserPaths(tmp_path)
    assert paths.root == tmp_path.resolve()
    assert paths.resolve("/x.md") == tmp_path.resolve() / "x.md"
    with pytest.raises(PathEscapeError):
        paths.resolve("../x.md")


def test_validate_user_id_accepts_uuids_and_slugs():
    validate_user_id(str(uuid.uuid4()))
    validate_user_id("user-1")


@pytest.mark.parametrize("bad", ["", "   ", "../x", "a/b", "a\\b", "a..b", "..", "a\x00b"])
def test_validate_user_id_rejects_unsafe_segments(bad):
    with pytest.raises(ValueError):
        validate_user_id(bad)
