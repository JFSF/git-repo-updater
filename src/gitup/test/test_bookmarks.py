# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

import os

from gitup import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_path(tmpdir):
    return str(tmpdir / "bookmarks")


def _write_bookmarks(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# list_bookmarks
# ---------------------------------------------------------------------------


def test_empty_list(tmpdir, capsys):
    config_path = tmpdir / "config"
    config.list_bookmarks(config_path)
    captured = capsys.readouterr()
    assert captured.out == "You have no bookmarks to display.\n"


def test_list_with_bookmarks(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    _write_bookmarks(cfg, ["/foo/bar", "/baz/qux"])
    config.list_bookmarks(cfg)
    out = capsys.readouterr().out
    assert "/foo/bar" in out
    assert "/baz/qux" in out


def test_list_quiet_produces_no_output(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    _write_bookmarks(cfg, ["/foo/bar"])
    config.list_bookmarks(cfg, quiet=True)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# add_bookmarks
# ---------------------------------------------------------------------------


def test_add_bookmark(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg)
    bookmarks = config.get_bookmarks(cfg)
    assert os.path.normcase(str(tmpdir)) in bookmarks


def test_add_duplicate_bookmark(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg)
    capsys.readouterr()  # flush first output
    config.add_bookmarks([str(tmpdir)], cfg)
    out = capsys.readouterr().out
    assert "Already bookmarked" in out
    # Should only appear once in the file
    assert config.get_bookmarks(cfg).count(os.path.normcase(str(tmpdir))) == 1


def test_add_bookmark_quiet(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg, quiet=True)
    assert capsys.readouterr().out == ""
    assert os.path.normcase(str(tmpdir)) in config.get_bookmarks(cfg)


def test_add_multiple_bookmarks(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    a = tmpdir.mkdir("a")
    b = tmpdir.mkdir("b")
    config.add_bookmarks([str(a), str(b)], cfg)
    bookmarks = config.get_bookmarks(cfg)
    assert os.path.normcase(str(a)) in bookmarks
    assert os.path.normcase(str(b)) in bookmarks


# ---------------------------------------------------------------------------
# delete_bookmarks
# ---------------------------------------------------------------------------


def test_delete_existing_bookmark(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg)
    config.delete_bookmarks([str(tmpdir)], cfg)
    bookmarks = config.get_bookmarks(cfg)
    assert os.path.normcase(str(tmpdir)) not in bookmarks


def test_delete_nonexistent_bookmark(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.delete_bookmarks([str(tmpdir)], cfg)
    out = capsys.readouterr().out
    assert "Not bookmarked" in out


def test_delete_bookmark_quiet(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg, quiet=True)
    config.delete_bookmarks([str(tmpdir)], cfg, quiet=True)
    assert capsys.readouterr().out == ""
    assert config.get_bookmarks(cfg) == []


# ---------------------------------------------------------------------------
# clean_bookmarks
# ---------------------------------------------------------------------------


def test_clean_no_bookmarks(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.clean_bookmarks(cfg)
    assert "no bookmarks" in capsys.readouterr().out


def test_clean_all_valid(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    config.add_bookmarks([str(tmpdir)], cfg, quiet=True)
    config.clean_bookmarks(cfg)
    assert "valid" in capsys.readouterr().out


def test_clean_removes_missing(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    missing = str(tmpdir.join("does_not_exist"))
    _write_bookmarks(cfg, [missing])
    config.clean_bookmarks(cfg)
    assert config.get_bookmarks(cfg) == []
    assert "Deleted" in capsys.readouterr().out


def test_clean_quiet(tmpdir, capsys):
    cfg = _config_path(tmpdir)
    missing = str(tmpdir.join("does_not_exist"))
    _write_bookmarks(cfg, [missing])
    config.clean_bookmarks(cfg, quiet=True)
    assert capsys.readouterr().out == ""
    assert config.get_bookmarks(cfg) == []


# ---------------------------------------------------------------------------
# _ensure_dirs — no race condition
# ---------------------------------------------------------------------------


def test_ensure_dirs_idempotent(tmpdir):
    """Calling _ensure_dirs twice must not raise FileExistsError."""
    target = str(tmpdir.join("nested", "deep", "bookmarks"))
    config._ensure_dirs(target)
    config._ensure_dirs(target)  # must not raise


# ---------------------------------------------------------------------------
# _load_config_file — blank lines and whitespace ignored
# ---------------------------------------------------------------------------


def test_load_strips_blank_lines(tmpdir):
    cfg = _config_path(tmpdir)
    _write_bookmarks(cfg, ["/a", "", "  ", "/b", ""])
    result = config.get_bookmarks(cfg)
    assert result == ["/a", "/b"]


# ---------------------------------------------------------------------------
# get_default_config_path uses XDG_CONFIG_HOME
# ---------------------------------------------------------------------------


def test_default_config_path_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.get_default_config_path()
    assert path.startswith(str(tmp_path))
    assert path.endswith("bookmarks")
