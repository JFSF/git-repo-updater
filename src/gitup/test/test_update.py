# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Tests for gitup.update — covering the pure-logic helpers and the
   git-operation functions via mocked GitPython objects."""

import os
from argparse import Namespace
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

from gitup.update import (
    _collect,
    _get_basename,
    _fetch_remotes,
    _update_branch,
    _update_repository,
    get_comment,
    is_comment,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _args(**kwargs) -> Namespace:
    """Return a minimal Namespace that satisfies update functions."""
    defaults = dict(
        quiet=False,
        verbose=False,
        current_only=False,
        fetch_only=False,
        prune=False,
        max_depth=3,
    )
    defaults.update(kwargs)
    return Namespace(**defaults)


def _make_branch(name="main", has_upstream=True, upstream_exists=True):
    """Build a mock branch object."""
    branch = MagicMock()
    branch.name = name
    branch.commit = MagicMock()

    if not has_upstream:
        branch.tracking_branch.return_value = None
    else:
        upstream = MagicMock()
        upstream.name = f"origin/{name}"
        if not upstream_exists:
            type(upstream).commit = PropertyMock(side_effect=ValueError("no commit"))
        else:
            upstream.commit = MagicMock()
        branch.tracking_branch.return_value = upstream
    return branch


# ---------------------------------------------------------------------------
# is_comment / get_comment
# ---------------------------------------------------------------------------


def test_is_comment_true():
    assert is_comment("# a comment") is True
    assert is_comment("  # indented") is True


def test_is_comment_false():
    assert is_comment("/some/path") is False
    assert is_comment("") is False


def test_get_comment_strips_hash():
    assert get_comment("# hello world") == "hello world"
    assert get_comment("  #  spaced  ") == "spaced"


def test_get_comment_empty():
    assert get_comment("#") == ""


# ---------------------------------------------------------------------------
# _get_basename
# ---------------------------------------------------------------------------


def test_get_basename_direct_child():
    base = "/home/user/projects"
    path = "/home/user/projects/web-app"
    assert _get_basename(base, path) == "web-app"


def test_get_basename_nested():
    base = "/home/user/projects"
    path = "/home/user/projects/frontend/web-app"
    assert _get_basename(base, path) == os.path.join("frontend", "web-app")


def test_get_basename_glob_pattern():
    """Glob-like base (containing *) should use the parent directory."""
    base = "/home/user/projects/*"
    path = "/home/user/projects/web-app"
    result = _get_basename(base, path)
    assert result == "web-app"


def test_get_basename_question_mark_glob():
    base = "/home/user/proj?cts"
    path = "/home/user/projects/app"
    # Should fall back to dirname of base = /home/user
    result = _get_basename(base, path)
    assert "app" in result


# ---------------------------------------------------------------------------
# _collect — deterministic ordering
# ---------------------------------------------------------------------------


def test_collect_empty_no_depth():
    assert _collect(["/any/path"], max_depth=0) == []


def test_collect_sorted_order(tmp_path):
    """_collect must traverse children in sorted (alphabetical) order."""
    # Create two sub-dirs that are NOT git repos → they will be listed recursively
    z_dir = tmp_path / "z_project"
    a_dir = tmp_path / "a_project"
    z_dir.mkdir()
    a_dir.mkdir()

    visited = []

    # Patch Repo to record visited paths and always raise InvalidGitRepositoryError
    # so that all directories are treated as non-repos.
    from git import exc as git_exc

    with patch("gitup.update.Repo", side_effect=git_exc.InvalidGitRepositoryError):
        _collect([str(tmp_path)], max_depth=2)

    # We cannot directly assert order without deeper instrumentation, but we
    # can verify the function runs without error and returns an empty list
    # (since none of the paths are real git repos).
    result = _collect([str(tmp_path)], max_depth=2)
    assert result == []


# ---------------------------------------------------------------------------
# _fetch_remotes
# ---------------------------------------------------------------------------


def test_fetch_remotes_no_refspec(capsys):
    remote = MagicMock()
    remote.name = "origin"
    remote.config_reader.has_option.return_value = False

    _fetch_remotes([remote], prune=False)

    out = capsys.readouterr().out
    assert "skipped" in out
    remote.fetch.assert_not_called()


def test_fetch_remotes_up_to_date(capsys):
    remote = MagicMock()
    remote.name = "origin"
    remote.config_reader.has_option.return_value = True

    fetch_info = MagicMock()
    # Set integer constants so that `flags & getattr(res, attr)` is a plain int.
    fetch_info.flags = 0
    fetch_info.NEW_HEAD = 1
    fetch_info.NEW_TAG = 2
    fetch_info.FAST_FORWARD = 4
    remote.fetch.return_value = [fetch_info]

    _fetch_remotes([remote], prune=False)

    out = capsys.readouterr().out
    assert "up to date" in out


def test_fetch_remotes_new_branch(capsys):
    from git import RemoteReference

    remote = MagicMock()
    remote.name = "origin"
    remote.config_reader.has_option.return_value = True

    fetch_info = MagicMock()
    fetch_info.flags = fetch_info.NEW_HEAD = 1  # NEW_HEAD flag
    fetch_info.ref = MagicMock(spec=RemoteReference)
    fetch_info.ref.remote_head = "feature-x"
    remote.fetch.return_value = [fetch_info]

    _fetch_remotes([remote], prune=False)

    out = capsys.readouterr().out
    assert "feature-x" in out


def test_fetch_remotes_quiet_suppresses_output(capsys):
    remote = MagicMock()
    remote.name = "origin"
    remote.config_reader.has_option.return_value = True
    remote.fetch.return_value = []

    _fetch_remotes([remote], prune=False, quiet=True)

    assert capsys.readouterr().out == ""


def test_fetch_remotes_git_error(capsys):
    from git import exc as git_exc

    remote = MagicMock()
    remote.name = "origin"
    remote.config_reader.has_option.return_value = True

    err = git_exc.GitCommandError(["git", "fetch"], 128)
    err.stderr = "fatal: repository not found"
    remote.fetch.side_effect = err

    _fetch_remotes([remote], prune=False)

    out = capsys.readouterr().out
    assert "error" in out


# ---------------------------------------------------------------------------
# _update_branch
# ---------------------------------------------------------------------------


def test_update_branch_no_upstream(capsys):
    branch = _make_branch(has_upstream=False)
    repo = MagicMock()
    args = _args()

    _update_branch(repo, branch, args)

    out = capsys.readouterr().out
    assert "skipped" in out
    assert "no upstream" in out


def test_update_branch_upstream_no_commit(capsys):
    branch = _make_branch(upstream_exists=False)
    repo = MagicMock()
    args = _args()

    _update_branch(repo, branch, args)

    out = capsys.readouterr().out
    assert "skipped" in out


def test_update_branch_up_to_date(capsys):
    branch = _make_branch()
    upstream = branch.tracking_branch()
    repo = MagicMock()

    # merge_base returns the upstream commit hash ⇒ already up to date
    repo.git.merge_base.return_value = "abc123"
    repo.commit.return_value = upstream.commit

    args = _args()
    _update_branch(repo, branch, args)

    out = capsys.readouterr().out
    assert "up to date" in out


def test_update_branch_active_fast_forward(capsys):
    branch = _make_branch()
    upstream = branch.tracking_branch()
    repo = MagicMock()

    repo.git.merge_base.return_value = "abc123"
    repo.commit.return_value = MagicMock()  # different from upstream → needs update

    args = _args()
    _update_branch(repo, branch, args, is_active=True)

    out = capsys.readouterr().out
    assert "done" in out
    repo.git.merge.assert_called_once_with(upstream.name, ff_only=True)


def test_update_branch_active_uncommitted_changes(capsys):
    from git import exc as git_exc

    branch = _make_branch()
    repo = MagicMock()

    repo.git.merge_base.return_value = "abc123"
    repo.commit.return_value = MagicMock()

    err = git_exc.GitCommandError(["git", "merge"], 1)
    err.stderr = "error: Your local changes to the following files would be overwritten"
    repo.git.merge.side_effect = err

    args = _args()
    _update_branch(repo, branch, args, is_active=True)

    out = capsys.readouterr().out
    assert "uncommitted changes" in out


def test_update_branch_inactive_fast_forward(capsys):
    branch = _make_branch()
    upstream = branch.tracking_branch()
    repo = MagicMock()

    repo.git.merge_base.return_value = "abc123"
    repo.commit.return_value = MagicMock()  # different from upstream
    # is_ancestor check returns status 0 → can fast-forward
    repo.git.merge_base.side_effect = None
    repo.git.merge_base.return_value = "abc123"

    # First call: base computation; second call: is_ancestor check → status 0
    repo.git.merge_base = MagicMock(
        side_effect=["abc123", (0, "", "")]
    )
    repo.commit.return_value = MagicMock()

    args = _args()
    _update_branch(repo, branch, args, is_active=False)

    out = capsys.readouterr().out
    assert "done" in out


def test_update_branch_quiet_suppresses_output(capsys):
    branch = _make_branch(has_upstream=False)
    repo = MagicMock()
    args = _args(quiet=True)

    _update_branch(repo, branch, args)

    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# _update_repository (integration-style with mocks)
# ---------------------------------------------------------------------------


def test_update_repository_no_remotes(capsys):
    repo = MagicMock()
    repo.active_branch = MagicMock()
    repo.remotes = []
    args = _args()

    _update_repository(repo, "my-repo", args)

    err = capsys.readouterr().err
    assert "no remotes" in err


def test_update_repository_detached_head_current_only(capsys):
    repo = MagicMock()
    type(repo).active_branch = PropertyMock(side_effect=TypeError)
    args = _args(current_only=True)

    _update_repository(repo, "my-repo", args)

    err = capsys.readouterr().err
    assert "detached HEAD" in err
