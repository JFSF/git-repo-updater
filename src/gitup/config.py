# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

from glob import glob
import os

from colorama import Fore, Style

from gitup.migrate import run_migrations

__all__ = [
    "get_default_config_path",
    "get_bookmarks",
    "add_bookmarks",
    "delete_bookmarks",
    "list_bookmarks",
    "clean_bookmarks",
]

YELLOW = Fore.YELLOW + Style.BRIGHT
RED = Fore.RED + Style.BRIGHT

INDENT1 = " " * 3


def _ensure_dirs(path: str) -> None:
    """Ensure the directories within the given pathname exist."""
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)


def _load_config_file(config_path: str | None = None) -> list[str]:
    """Read the config file and return a list of bookmarks."""
    run_migrations()
    cfg_path = config_path or get_default_config_path()

    try:
        with open(cfg_path, encoding="utf-8") as config_file:
            paths = config_file.read().splitlines()
    except OSError:
        return []
    return [path.strip() for path in paths if path.strip()]


def _save_config_file(bookmarks: list[str], config_path: str | None = None) -> None:
    """Save the bookmarks list to the given config file."""
    run_migrations()
    cfg_path = config_path or get_default_config_path()
    _ensure_dirs(cfg_path)

    with open(cfg_path, "w", encoding="utf-8") as config_file:
        config_file.write("\n".join(bookmarks))


def _normalize_path(path: str) -> str:
    """Normalize the given path."""
    if path.startswith("~"):
        return os.path.normcase(os.path.normpath(path))
    return os.path.normcase(os.path.abspath(path))


def get_default_config_path() -> str:
    """Return the default path to the configuration file."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join("~", ".config")
    return os.path.join(os.path.expanduser(xdg_cfg), "gitup", "bookmarks")


def get_bookmarks(config_path: str | None = None) -> list[str]:
    """Get a list of all bookmarks, or an empty list if there are none."""
    return _load_config_file(config_path)


def add_bookmarks(
    paths: list[str], config_path: str | None = None, quiet: bool = False
) -> None:
    """Add a list of paths as bookmarks to the config file."""
    config = _load_config_file(config_path)
    paths = [_normalize_path(path) for path in paths]

    added, exists = [], []
    for path in paths:
        if path in config:
            exists.append(path)
        else:
            config.append(path)
            added.append(path)
    _save_config_file(config, config_path)

    if not quiet:
        if added:
            print(YELLOW + "Added bookmarks:")
            for path in added:
                print(INDENT1, path)
        if exists:
            print(RED + "Already bookmarked:")
            for path in exists:
                print(INDENT1, path)


def delete_bookmarks(
    paths: list[str], config_path: str | None = None, quiet: bool = False
) -> None:
    """Remove a list of paths from the bookmark config file."""
    config = _load_config_file(config_path)
    paths = [_normalize_path(path) for path in paths]

    deleted, notmarked = [], []
    if config:
        for path in paths:
            if path in config:
                config.remove(path)
                deleted.append(path)
            else:
                notmarked.append(path)
        _save_config_file(config, config_path)
    else:
        notmarked = paths

    if not quiet:
        if deleted:
            print(YELLOW + "Deleted bookmarks:")
            for path in deleted:
                print(INDENT1, path)
        if notmarked:
            print(RED + "Not bookmarked:")
            for path in notmarked:
                print(INDENT1, path)


def list_bookmarks(config_path: str | None = None, quiet: bool = False) -> None:
    """Print all of our current bookmarks."""
    bookmarks = _load_config_file(config_path)
    if not quiet:
        if bookmarks:
            print(YELLOW + "Current bookmarks:")
            for bookmark_path in bookmarks:
                print(INDENT1, bookmark_path)
        else:
            print("You have no bookmarks to display.")


def clean_bookmarks(config_path: str | None = None, quiet: bool = False) -> None:
    """Delete any bookmarks that don't exist."""
    bookmarks = _load_config_file(config_path)
    if not bookmarks:
        if not quiet:
            print("You have no bookmarks to clean up.")
        return

    delete = [
        path
        for path in bookmarks
        if not (os.path.isdir(path) or glob(os.path.expanduser(path)))
    ]
    if not delete:
        if not quiet:
            print("All of your bookmarks are valid.")
        return

    bookmarks = [path for path in bookmarks if path not in delete]
    _save_config_file(bookmarks, config_path)

    if not quiet:
        print(YELLOW + "Deleted bookmarks:")
        for path in delete:
            print(INDENT1, path)
