# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Tests for the low-priority features:

- profile.py   (named bookmark sets)
- notify.py    (desktop notifications)
- history.py   (SQLite analytics)
- cli.py       (_parse_interval, --watch, --profile, --history flags)
- tui.py       (Textual TUI — skipped when textual is not installed)
"""

import os
import subprocess
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Profile tests
# ---------------------------------------------------------------------------


class TestProfile:
    def test_get_profile_path_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import get_profile_path, get_default_config_path
        assert get_profile_path("default") == get_default_config_path()

    def test_get_profile_path_named(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import get_profile_path, get_profile_dir
        expected = os.path.join(get_profile_dir(), "work")
        assert get_profile_path("work") == expected

    def test_list_profiles_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import list_profiles
        assert list_profiles() == []

    def test_list_profiles_with_entries(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import get_profile_dir, list_profiles
        pdir = get_profile_dir()
        os.makedirs(pdir, exist_ok=True)
        (os.path.join(pdir, "work"),  open(os.path.join(pdir, "work"),  "w").write("/a"))
        (os.path.join(pdir, "personal"), open(os.path.join(pdir, "personal"), "w").write("/b"))
        profiles = list_profiles()
        assert "work" in profiles
        assert "personal" in profiles
        assert profiles == sorted(profiles)  # must be sorted

    def test_resolve_bookmark_file_profile_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import resolve_bookmark_file, get_profile_path
        result = resolve_bookmark_file("work", "/some/other/file")
        assert result == get_profile_path("work")

    def test_resolve_bookmark_file_fallback(self, tmp_path):
        from gitup.profile import resolve_bookmark_file
        explicit = str(tmp_path / "myfile")
        assert resolve_bookmark_file(None, explicit) == explicit

    def test_resolve_bookmark_file_both_none(self):
        from gitup.profile import resolve_bookmark_file
        assert resolve_bookmark_file(None, None) is None

    def test_print_profiles_quiet(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import print_profiles
        print_profiles(quiet=True)
        assert capsys.readouterr().out == ""

    def test_print_profiles_no_profiles(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from gitup.profile import print_profiles
        print_profiles()
        assert "No profiles found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Notify tests
# ---------------------------------------------------------------------------


class TestNotify:
    def test_build_summary_no_updates(self):
        from gitup.notify import build_summary_message
        from gitup.update import RepoResult
        results = [RepoResult("/a", "a"), RepoResult("/b", "b")]
        msg = build_summary_message(results)
        assert "2 repos" in msg
        assert "up to date" in msg

    def test_build_summary_with_updates(self):
        from gitup.notify import build_summary_message
        from gitup.update import RepoResult
        results = [
            RepoResult("/a", "a", branches_updated=["main"]),
            RepoResult("/b", "b"),
        ]
        msg = build_summary_message(results)
        assert "1 updated" in msg

    def test_build_summary_with_errors(self):
        from gitup.notify import build_summary_message
        from gitup.update import RepoResult
        results = [RepoResult("/a", "a", errors=["fetch failed"])]
        msg = build_summary_message(results)
        assert "1 error" in msg

    def test_is_notify_available_returns_bool(self):
        from gitup.notify import is_notify_available
        result = is_notify_available()
        assert isinstance(result, bool)

    def test_send_notification_bad_platform_does_not_raise(self, monkeypatch):
        """send_notification must silently no-op on unknown platforms."""
        import gitup.notify as _n
        monkeypatch.setattr(_n.platform, "system", lambda: "Haiku")
        _n.send_notification("title", "message")  # must not raise

    def test_send_notification_tool_missing_does_not_raise(self, monkeypatch):
        """send_notification must not raise when notify-send is absent."""
        import gitup.notify as _n
        monkeypatch.setattr(_n.platform, "system", lambda: "Linux")
        # Patch subprocess.run to simulate FileNotFoundError
        import subprocess
        monkeypatch.setattr(_n.subprocess, "run",
                            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("not found")))
        _n.send_notification("title", "message")  # must not raise


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------


class TestHistory:
    @pytest.fixture(autouse=True)
    def _use_temp_db(self, monkeypatch, tmp_path):
        """Redirect the history DB to a temporary directory."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        # Force module to re-evaluate path
        import gitup.history as _h
        monkeypatch.setattr(_h, "get_history_db_path",
                            lambda: str(tmp_path / "gitup" / "history.db"))

    def test_record_and_retrieve(self, capsys):
        from gitup.history import record_updates, show_history
        from gitup.update import RepoResult

        results = [
            RepoResult("/a", "repo-a", branches_updated=["main"]),
            RepoResult("/b", "repo-b"),
        ]
        record_updates(results)
        show_history(n=10)
        out = capsys.readouterr().out
        assert "repo-a" in out
        assert "repo-b" in out

    def test_show_history_empty(self, capsys):
        from gitup.history import show_history
        show_history()
        assert "No history" in capsys.readouterr().out

    def test_show_history_quiet(self, capsys):
        from gitup.history import show_history
        from gitup.update import RepoResult
        from gitup.history import record_updates
        record_updates([RepoResult("/a", "a")])
        show_history(quiet=True)
        assert capsys.readouterr().out == ""

    def test_show_stats_empty(self, capsys):
        from gitup.history import show_stats
        show_stats()
        assert "No history" in capsys.readouterr().out

    def test_show_stats_with_data(self, capsys):
        from gitup.history import record_updates, show_stats
        from gitup.update import RepoResult

        for _ in range(3):
            record_updates([
                RepoResult("/a", "alpha", branches_updated=["main"]),
                RepoResult("/b", "beta"),
            ])
        show_stats()
        out = capsys.readouterr().out
        assert "Sessions recorded" in out
        assert "alpha" in out

    def test_record_status_error(self):
        from gitup.history import record_updates, _connect
        from gitup.update import RepoResult

        results = [RepoResult("/a", "a", errors=["fetch failed"])]
        record_updates(results)

        conn = _connect()
        row = conn.execute("SELECT status FROM updates WHERE name='a'").fetchone()
        conn.close()
        assert row["status"] == "error"

    def test_same_session_groups_rows(self):
        from gitup.history import record_updates, _connect
        from gitup.update import RepoResult
        import uuid

        sid = str(uuid.uuid4())
        results = [RepoResult("/a", "a"), RepoResult("/b", "b")]
        record_updates(results, session_id=sid)

        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM updates WHERE session=?", (sid,)
        ).fetchall()
        conn.close()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# _parse_interval tests (from cli.py)
# ---------------------------------------------------------------------------


class TestParseInterval:
    def test_seconds(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("30s") == 30

    def test_minutes(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("5m") == 300

    def test_hours(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("2h") == 7200

    def test_days(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("1d") == 86400

    def test_combined(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("1h30m") == 5400
        assert _parse_interval("2h15m30s") == 8130

    def test_case_insensitive(self):
        from gitup.cli import _parse_interval
        assert _parse_interval("1H") == 3600

    def test_invalid_raises(self):
        from gitup.cli import _parse_interval
        with pytest.raises(ValueError, match="Invalid interval"):
            _parse_interval("forever")


# ---------------------------------------------------------------------------
# CLI integration — --profile and --history flags
# ---------------------------------------------------------------------------


def _run_gitup(*args, extra_env=None):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "gitup"] + list(args),
        capture_output=True, text=True, env=env,
    )


class TestCLILowFlags:
    def test_profile_list_empty(self, tmp_path):
        result = _run_gitup(
            "--profile", "list",
            extra_env={"XDG_CONFIG_HOME": str(tmp_path)},
        )
        assert "No profiles found" in result.stdout

    def test_profile_list_with_profiles(self, tmp_path):
        profiles_dir = tmp_path / "gitup" / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "work").write_text("/a")
        result = _run_gitup(
            "--profile", "list",
            extra_env={"XDG_CONFIG_HOME": str(tmp_path)},
        )
        assert "work" in result.stdout

    def test_history_empty(self, tmp_path):
        result = _run_gitup(
            "--history",
            extra_env={"XDG_DATA_HOME": str(tmp_path)},
        )
        assert "No history" in result.stdout

    def test_history_stats_empty(self, tmp_path):
        result = _run_gitup(
            "--history-stats",
            extra_env={"XDG_DATA_HOME": str(tmp_path)},
        )
        assert "No history" in result.stdout

    def test_watch_invalid_interval(self):
        result = _run_gitup("--watch", "forever")
        assert result.returncode != 0
        assert "Invalid interval" in result.stderr


# ---------------------------------------------------------------------------
# TUI tests (skipped if textual is not installed)
# ---------------------------------------------------------------------------


textual = pytest.importorskip("textual", reason="textual not installed")


class TestTUI:
    def test_textual_available_flag(self):
        from gitup.tui import TEXTUAL_AVAILABLE
        assert TEXTUAL_AVAILABLE is True

    def test_launch_tui_raises_without_textual(self, monkeypatch):
        import gitup.tui as _tui
        monkeypatch.setattr(_tui, "_TEXTUAL_AVAILABLE", False)
        monkeypatch.setattr(_tui, "TEXTUAL_AVAILABLE", False)
        with pytest.raises(ImportError, match="textual"):
            _tui.launch_tui()

    def test_app_can_be_instantiated(self):
        from gitup.tui import GitupApp
        app = GitupApp()
        assert app is not None

    def test_default_tui_args_has_required_fields(self):
        from gitup.tui import _default_tui_args
        args = _default_tui_args()
        assert hasattr(args, "quiet")
        assert hasattr(args, "parallel")
        assert hasattr(args, "_results")
        assert args.parallel == 1
