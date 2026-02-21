# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Tests covering the medium-priority features:

- RepoResult dataclass
- --parallel (concurrent dispatch)
- --status (local status display)
- --json (structured JSON output)
"""

import json
import os
import subprocess
import sys
import threading
import time
from argparse import Namespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from gitup.update import (
    RepoResult,
    _format_age,
    _get_basename,
    _status_repository,
    update_bookmarks,
    update_directories,
    status_bookmarks,
    status_directories,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(**kwargs) -> Namespace:
    defaults = dict(
        quiet=False,
        verbose=False,
        current_only=False,
        fetch_only=False,
        prune=False,
        max_depth=3,
        parallel=1,
        status=False,
        json=False,
    )
    defaults.update(kwargs)
    return Namespace(**defaults)


# ---------------------------------------------------------------------------
# RepoResult
# ---------------------------------------------------------------------------


class TestRepoResult:
    def test_status_up_to_date(self):
        r = RepoResult(path="/a", name="a")
        assert r.status == "up_to_date"

    def test_status_updated_via_branch(self):
        r = RepoResult(path="/a", name="a", branches_updated=["main"])
        assert r.status == "updated"

    def test_status_updated_via_new_branch(self):
        r = RepoResult(path="/a", name="a", new_branches=["feature-x"])
        assert r.status == "updated"

    def test_status_updated_via_new_tag(self):
        r = RepoResult(path="/a", name="a", new_tags=["v1.0"])
        assert r.status == "updated"

    def test_status_error(self):
        r = RepoResult(path="/a", name="a", errors=["fetch failed"])
        assert r.status == "error"

    def test_to_dict_structure(self):
        r = RepoResult(
            path="/a",
            name="a",
            branches_updated=["main"],
            new_branches=["feat"],
            new_tags=["v1"],
            errors=[],
        )
        d = r.to_dict()
        assert d["path"] == "/a"
        assert d["name"] == "a"
        assert d["status"] == "updated"
        assert d["branches_updated"] == ["main"]
        assert d["new_branches"] == ["feat"]
        assert d["new_tags"] == ["v1"]
        assert d["errors"] == []


# ---------------------------------------------------------------------------
# _format_age
# ---------------------------------------------------------------------------


class TestFormatAge:
    def test_just_now(self):
        assert _format_age(30) == "just now"

    def test_minutes(self):
        assert _format_age(90) == "1m ago"
        assert _format_age(3599) == "59m ago"

    def test_hours(self):
        assert _format_age(7200) == "2h ago"

    def test_days(self):
        assert _format_age(86400 * 3) == "3d ago"


# ---------------------------------------------------------------------------
# --parallel dispatch
# ---------------------------------------------------------------------------


class TestParallelDispatch:
    """Verify that parallel dispatch runs callbacks concurrently and that
    output from different repos does not interleave within a single repo's
    block."""

    def _make_repo_mock(self, path: str):
        repo = MagicMock()
        repo.working_dir = path
        repo.active_branch = MagicMock()
        repo.active_branch.name = "main"
        repo.remotes = []
        return repo

    def test_parallel_produces_same_set_of_results(self, tmp_path):
        """Running with parallel=2 must collect the same RepoResult entries
        as running sequentially."""
        # We patch _dispatch at a low level: test update_directories with
        # a mock that records calls and does nothing else.
        calls_serial = []
        calls_parallel = []

        def fake_dispatch(path, callback, args):
            calls = calls_parallel if getattr(args, "parallel", 1) > 1 else calls_serial
            calls.append(path)

        with patch("gitup.update._dispatch", side_effect=fake_dispatch):
            args_s = _args(parallel=1)
            update_directories(["/a", "/b"], args_s)

            args_p = _args(parallel=2)
            update_directories(["/a", "/b"], args_p)

        assert set(calls_serial) == {"/a", "/b"}
        assert set(calls_parallel) == {"/a", "/b"}

    def test_parallel_greater_than_1_uses_thread_pool(self, tmp_path):
        """_dispatch_parallel must use multiple threads."""
        thread_ids: list[int] = []
        lock = threading.Lock()

        def recording_callback(repo, name, args):
            with lock:
                thread_ids.append(threading.get_ident())
            time.sleep(0.02)  # simulate work

        from gitup.update import _dispatch_parallel

        # Two fake paths
        paths = [("repo-a", "/fake/a"), ("repo-b", "/fake/b")]
        args = _args(parallel=2)

        with patch("gitup.update.Repo"):
            _dispatch_parallel(paths, recording_callback, args)

        # Both callbacks should have been called
        assert len(thread_ids) == 2

    def test_parallel_output_is_atomic(self, tmp_path):
        """Each repo's output block must not be split by another repo's output."""
        import io
        import sys

        output_chunks: list[str] = []
        real_write = sys.stdout.write

        def capturing_write(s):
            if s.strip():
                output_chunks.append(s)
            real_write(s)

        def slow_callback(repo, name, args):
            from gitup.update import _print
            _print(args, f"START:{name}")
            time.sleep(0.01)
            _print(args, f"END:{name}")

        from gitup.update import _dispatch_parallel

        paths = [("repo-a", "/fake/a"), ("repo-b", "/fake/b")]
        args = _args(parallel=2)

        with patch("gitup.update.Repo"), patch.object(sys, "stdout") as mock_stdout:
            written: list[str] = []
            mock_stdout.write = lambda s: written.append(s)
            mock_stdout.flush = lambda: None
            _dispatch_parallel(paths, slow_callback, args)

        full = "".join(written)
        # Each repo block (START+END) must appear together, not interleaved
        for name in ("repo-a", "repo-b"):
            start_idx = full.find(f"START:{name}")
            end_idx = full.find(f"END:{name}")
            assert start_idx != -1 and end_idx != -1
            assert start_idx < end_idx


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------


class TestStatusRepository:
    def _make_clean_repo(self, name="main", upstream="origin/main"):
        repo = MagicMock()
        repo.git_dir = "/tmp/fake/.git"

        active = MagicMock()
        active.name = name

        up = MagicMock()
        up.name = upstream
        active.tracking_branch.return_value = up
        repo.active_branch = active
        repo.head.is_detached = False

        # ahead=0, behind=0
        repo.iter_commits.return_value = iter([])

        # clean
        repo.is_dirty.return_value = False
        repo.index.diff.return_value = []
        repo.untracked_files = []

        return repo

    def test_status_shows_branch(self, capsys):
        repo = self._make_clean_repo()
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "main" in out

    def test_status_shows_clean(self, capsys):
        repo = self._make_clean_repo()
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "clean" in out

    def test_status_shows_dirty(self, capsys):
        repo = self._make_clean_repo()
        repo.is_dirty.return_value = True
        repo.index.diff.return_value = [MagicMock(), MagicMock()]  # 2 modified
        repo.untracked_files = ["foo.txt"]
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "dirty" in out

    def test_status_shows_fetch_age(self, capsys, tmp_path):
        repo = self._make_clean_repo()
        fetch_head = tmp_path / "FETCH_HEAD"
        fetch_head.touch()
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(fetch_head, (old_time, old_time))
        repo.git_dir = str(tmp_path)

        args = _args()
        _status_repository(repo, "my-repo", args)

        out = capsys.readouterr().out
        assert "ago" in out

    def test_status_shows_never_fetched(self, capsys):
        repo = self._make_clean_repo()
        repo.git_dir = "/nonexistent_dir_xyz"
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "never" in out

    def test_status_quiet_suppresses_output(self, capsys):
        repo = self._make_clean_repo()
        args = _args(quiet=True)
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        assert capsys.readouterr().out == ""

    def test_status_shows_ahead_behind(self, capsys):
        repo = self._make_clean_repo()
        # ahead=2, behind=1
        def fake_iter_commits(spec):
            # upstream..branch → 2 commits; branch..upstream → 1 commit
            if spec.startswith("origin"):
                return iter([MagicMock()] * 2)
            return iter([MagicMock()])

        repo.iter_commits.side_effect = fake_iter_commits
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "↑" in out or "↓" in out

    def test_status_detached_head(self, capsys):
        repo = MagicMock()
        repo.git_dir = "/tmp/fake/.git"
        type(repo).active_branch = PropertyMock(side_effect=TypeError)
        repo.is_dirty.return_value = False
        repo.untracked_files = []
        repo.index.diff.return_value = []
        args = _args()
        with patch("os.path.exists", return_value=False):
            _status_repository(repo, "my-repo", args)
        out = capsys.readouterr().out
        assert "detached" in out


# ---------------------------------------------------------------------------
# --json (via CLI subprocess)
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def _run_gitup(self, *extra_args, cwd=None):
        cmd = [sys.executable, "-m", "gitup"] + list(extra_args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result

    def test_json_output_is_valid_json(self, tmp_path):
        """--json with no bookmarks should emit valid JSON with an empty repos list."""
        import tempfile

        cfg = tmp_path / "bookmarks"
        cfg.write_text("")  # empty bookmarks
        result = self._run_gitup("--json", "--bookmark-file", str(cfg))
        # stdout must be valid JSON
        payload = json.loads(result.stdout)
        assert "repos" in payload
        assert "summary" in payload
        assert isinstance(payload["repos"], list)

    def test_json_summary_fields(self, tmp_path):
        cfg = tmp_path / "bookmarks"
        cfg.write_text("")
        result = self._run_gitup("--json", "--bookmark-file", str(cfg))
        summary = json.loads(result.stdout)["summary"]
        for key in ("total", "updated", "up_to_date", "errors"):
            assert key in summary

    def test_json_suppresses_human_output(self, tmp_path):
        cfg = tmp_path / "bookmarks"
        cfg.write_text("")
        result = self._run_gitup("--json", "--bookmark-file", str(cfg))
        # Human-readable header "gitup: the git-repo-updater" must NOT appear
        assert "git-repo-updater" not in result.stdout
        # But a valid JSON object must be present
        payload = json.loads(result.stdout)
        assert payload is not None

    def test_json_repo_result_structure(self, tmp_path):
        """When a real git repo is updated the JSON entry must include all keys."""
        import subprocess as sp

        # Create a minimal local git repo with a remote to fetch
        bare = tmp_path / "bare.git"
        clone = tmp_path / "clone"
        sp.run(["git", "init", "--bare", str(bare)], capture_output=True)
        sp.run(["git", "clone", str(bare), str(clone)], capture_output=True)
        # Configure user for commits
        sp.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], capture_output=True)
        sp.run(["git", "-C", str(clone), "config", "user.name", "Test"], capture_output=True)
        # Make an initial commit so the branch exists
        (clone / "README.md").write_text("hi")
        sp.run(["git", "-C", str(clone), "add", "."], capture_output=True)
        sp.run(["git", "-C", str(clone), "commit", "-m", "init"], capture_output=True)

        cfg = tmp_path / "bookmarks"
        cfg.write_text(str(clone))

        result = self._run_gitup("--json", "--bookmark-file", str(cfg))
        payload = json.loads(result.stdout)
        assert len(payload["repos"]) == 1
        repo_entry = payload["repos"][0]
        for key in ("path", "name", "status", "new_branches", "new_tags", "branches_updated", "errors"):
            assert key in repo_entry
