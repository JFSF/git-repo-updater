# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Named bookmark sets (profiles).

A profile is simply a named bookmark file stored under the gitup config
directory at ``~/.config/gitup/profiles/<name>``.  The special profile name
``"default"`` resolves to the main bookmark file returned by
:func:`gitup.config.get_default_config_path`.

Using ``--profile all`` iterates over every profile in the profiles directory.
"""

import os

from colorama import Fore, Style

from gitup.config import get_default_config_path

__all__ = [
    "get_profile_dir",
    "get_profile_path",
    "list_profiles",
    "resolve_bookmark_file",
]

YELLOW = Fore.YELLOW + Style.BRIGHT
BOLD = Style.BRIGHT
RESET = Style.RESET_ALL
INDENT1 = " " * 3


def get_profile_dir() -> str:
    """Return the directory that contains named profile files."""
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join("~", ".config")
    return os.path.join(os.path.expanduser(xdg_cfg), "gitup", "profiles")


def get_profile_path(name: str) -> str:
    """Return the bookmark file path for *name*.

    The special value ``"default"`` maps to the standard bookmark file.
    """
    if name == "default":
        return get_default_config_path()
    return os.path.join(get_profile_dir(), name)


def list_profiles() -> list[str]:
    """Return a sorted list of available profile names (excluding ``"default"``)."""
    profiles_dir = get_profile_dir()
    if not os.path.isdir(profiles_dir):
        return []
    return sorted(
        entry
        for entry in os.listdir(profiles_dir)
        if os.path.isfile(os.path.join(profiles_dir, entry))
    )


def resolve_bookmark_file(
    profile: str | None, bookmark_file: str | None
) -> str | None:
    """Return the effective bookmark file path given ``--profile`` and ``--bookmark-file``.

    ``--profile`` takes precedence over ``--bookmark-file``.  Returns ``None``
    when neither is provided (the default bookmark file will be used).
    """
    if profile is not None:
        return get_profile_path(profile)
    return bookmark_file


def print_profiles(quiet: bool = False) -> None:
    """Print all available profiles to stdout."""
    profiles = list_profiles()
    if quiet:
        return
    if profiles:
        print(YELLOW + "Available profiles:")
        for name in profiles:
            path = get_profile_path(name)
            print(INDENT1, BOLD + name + RESET, f"({path})")
    else:
        print("No profiles found. Create one with --profile <name> --add <path>.")
