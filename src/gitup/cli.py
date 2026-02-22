# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

import argparse
import json
import logging
import os
import platform
import re
import sys
import time

from colorama import init as color_init, Style

from gitup import __version__
from gitup.config import (
    get_default_config_path,
    get_bookmarks,
    add_bookmarks,
    delete_bookmarks,
    list_bookmarks,
    clean_bookmarks,
)
from gitup.profile import (
    get_profile_path,
    list_profiles,
    print_profiles,
    resolve_bookmark_file,
)
from gitup.update import (
    update_bookmarks,
    update_directories,
    status_bookmarks,
    status_directories,
    run_command,
    RepoResult,
)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        description="Easily update multiple git repositories at once.",
        epilog="""
            Both relative and absolute paths are accepted by all arguments.
            Direct bug reports and feature requests to
            https://github.com/earwig/git-repo-updater.""",
        add_help=False,
    )

    group_u = parser.add_argument_group("updating repositories")
    group_b = parser.add_argument_group("bookmarking")
    group_a = parser.add_argument_group("advanced")
    group_m = parser.add_argument_group("miscellaneous")

    # --- updating ---
    group_u.add_argument(
        "directories_to_update",
        nargs="*",
        metavar="path",
        help="""update this repository, or all repositories it contains
        (if not a repo directly)""",
    )
    group_u.add_argument(
        "-u",
        "--update",
        action="store_true",
        help="update all bookmarks (default behavior when called without arguments)",
    )
    group_u.add_argument(
        "-t",
        "--depth",
        dest="max_depth",
        metavar="n",
        type=int,
        default=3,
        help="""max recursion depth when searching for repos in subdirectories
        (default: 3; use 0 for no recursion, or -1 for unlimited)""",
    )
    group_u.add_argument(
        "-c",
        "--current-only",
        action="store_true",
        help="only fetch the remote tracked by the current branch instead of all remotes",
    )
    group_u.add_argument(
        "-f",
        "--fetch-only",
        action="store_true",
        help="only fetch remotes, don't try to fast-forward any branches",
    )
    group_u.add_argument(
        "-p",
        "--prune",
        action="store_true",
        help="after fetching, delete remote-tracking branches that no longer exist on their remote",
    )
    group_u.add_argument(
        "--parallel",
        metavar="N",
        type=int,
        default=1,
        help="update up to N repositories in parallel (default: 1)",
    )
    group_u.add_argument(
        "-s",
        "--status",
        action="store_true",
        help="show local repository status without fetching",
    )
    group_u.add_argument(
        "-w",
        "--watch",
        metavar="interval",
        help="repeat updates at the given interval (e.g. 30m, 1h, 2h30m)",
    )

    # --- bookmarking ---
    group_b.add_argument(
        "-a",
        "--add",
        dest="bookmarks_to_add",
        nargs="+",
        metavar="path",
        help="add directory(s) as bookmarks",
    )
    group_b.add_argument(
        "-d",
        "--delete",
        dest="bookmarks_to_del",
        nargs="+",
        metavar="path",
        help="delete bookmark(s) (leaves actual directories alone)",
    )
    group_b.add_argument(
        "-l",
        "--list",
        dest="list_bookmarks",
        action="store_true",
        help="list current bookmarks",
    )
    group_b.add_argument(
        "-n",
        "--clean",
        "--cleanup",
        dest="clean_bookmarks",
        action="store_true",
        help="delete any bookmarks that don't exist",
    )
    group_b.add_argument(
        "-b",
        "--bookmark-file",
        nargs="?",
        metavar="path",
        help="use a specific bookmark config file (default: {0})".format(
            get_default_config_path()
        ),
    )
    group_b.add_argument(
        "-P",
        "--profile",
        metavar="name",
        help=(
            "use a named bookmark profile (~/.config/gitup/profiles/<name>); "
            "use 'all' to update every profile; "
            "use 'list' to show available profiles"
        ),
    )

    # --- advanced ---
    group_a.add_argument(
        "-e",
        "--exec",
        "--batch",
        dest="command",
        metavar="command",
        help="run a shell command on all repos",
    )
    group_a.add_argument(
        "--json",
        action="store_true",
        help="output a JSON summary of results (implies --quiet for human output)",
    )
    group_a.add_argument(
        "--notify",
        action="store_true",
        help="send a desktop notification when updates complete",
    )
    group_a.add_argument(
        "--history",
        action="store_true",
        help="show the last 20 update records and exit",
    )
    group_a.add_argument(
        "--history-stats",
        dest="history_stats",
        action="store_true",
        help="show aggregate update statistics and exit",
    )
    group_a.add_argument(
        "--tui",
        action="store_true",
        help="launch the interactive terminal UI (requires: pip install gitup[tui])",
    )

    # --- miscellaneous ---
    group_m.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )
    group_m.add_argument(
        "-v",
        "--version",
        action="version",
        version="gitup {0} (Python {1})".format(__version__, platform.python_version()),
    )
    group_m.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress all non-error output",
    )
    group_m.add_argument(
        "-V",
        "--verbose",
        action="store_true",
        help="show debug information (overrides --quiet for log messages)",
    )
    group_m.add_argument(
        "--selftest",
        action="store_true",
        help="run integrated test suite and exit (pytest must be available)",
    )

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    """Configure the root logger based on verbosity level."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s [%(levelname)s]: %(message)s",
    )


def _parse_interval(interval_str: str) -> int:
    """Parse a human-readable interval string to a number of seconds.

    Supported units: ``s``, ``m``, ``h``, ``d``.
    Units may be combined: ``1h30m`` → 5400.

    Raises :class:`ValueError` on unrecognised input.
    """
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    matches = re.findall(r"(\d+)([smhd])", interval_str.lower())
    if not matches:
        raise ValueError(
            f"Invalid interval {interval_str!r}. "
            "Use formats like 30s, 5m, 2h, 1d, or combined 1h30m."
        )
    return sum(int(v) * multipliers[u] for v, u in matches)


def _emit_json(results: list[RepoResult]) -> None:
    """Print a JSON summary of *results* to stdout."""
    total = len(results)
    updated = sum(1 for r in results if r.status == "updated")
    up_to_date = sum(1 for r in results if r.status == "up_to_date")
    errors = sum(1 for r in results if r.status == "error")
    payload = {
        "repos": [r.to_dict() for r in results],
        "summary": {
            "total": total,
            "updated": updated,
            "up_to_date": up_to_date,
            "errors": errors,
        },
    }
    print(json.dumps(payload, indent=2))


def _do_notify(results: list[RepoResult]) -> None:
    """Send a desktop notification summarising *results*."""
    try:
        from gitup.notify import send_notification, build_summary_message
        send_notification("gitup", build_summary_message(results))
    except Exception as exc:
        logging.getLogger(__name__).debug("Notification failed: %s", exc)


def _do_history_record(results: list[RepoResult]) -> None:
    """Persist *results* to the history database."""
    try:
        from gitup.history import record_updates
        record_updates(results)
    except Exception as exc:
        logging.getLogger(__name__).debug("History recording failed: %s", exc)


def _selftest() -> None:
    """Run the integrated test suite with pytest."""
    from .test import run_tests
    run_tests()


# ---------------------------------------------------------------------------
# Core update dispatch (extracted so --watch can call it repeatedly)
# ---------------------------------------------------------------------------


def _ensure_results(args: argparse.Namespace) -> None:
    """Ensure args._results exists (used by --json / --notify / --history)."""
    if not hasattr(args, "_results"):
        args._results = []
    else:
        args._results.clear()


def _run_update_cycle(
    args: argparse.Namespace, bookmark_file: str | None
) -> None:
    """Execute one full update / status / command cycle."""
    if args.json or args.notify or args.history:
        _ensure_results(args)

    if args.status:
        if args.directories_to_update:
            status_directories(args.directories_to_update, args)
        else:
            status_bookmarks(get_bookmarks(bookmark_file), args)
    elif args.command:
        if args.directories_to_update:
            run_command(args.directories_to_update, args)
        if args.update or not args.directories_to_update:
            run_command(get_bookmarks(bookmark_file), args)
    else:
        acted = False
        if args.directories_to_update:
            update_directories(args.directories_to_update, args)
            acted = True
        if args.update or not acted:
            update_bookmarks(get_bookmarks(bookmark_file), args)

    results: list[RepoResult] = getattr(args, "_results", [])

    if args.json:
        _emit_json(results)
    if args.notify:
        _do_notify(results)
    if args.history and results:
        _do_history_record(results)


def _run_bookmark_ops(
    args: argparse.Namespace, bookmark_file: str | None
) -> None:
    """Handle --add / --delete / --list / --clean operations."""
    if args.bookmarks_to_add:
        add_bookmarks(args.bookmarks_to_add, bookmark_file, quiet=args.quiet)
    if args.bookmarks_to_del:
        delete_bookmarks(args.bookmarks_to_del, bookmark_file, quiet=args.quiet)
    if args.list_bookmarks:
        list_bookmarks(bookmark_file, quiet=args.quiet)
    if args.clean_bookmarks:
        clean_bookmarks(bookmark_file, quiet=args.quiet)


# ---------------------------------------------------------------------------
# main() / run()
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and then call the appropriate function(s)."""
    parser = _build_parser()
    color_init(autoreset=True)
    args = parser.parse_args()

    _setup_logging(args.verbose)

    # --tui: launch interactive UI and exit
    if args.tui:
        from gitup.tui import launch_tui, TEXTUAL_AVAILABLE
        if not TEXTUAL_AVAILABLE:
            print(
                "The 'textual' package is required for the TUI.\n"
                "Install it with:  pip install gitup[tui]",
                file=sys.stderr,
            )
            raise SystemExit(1)
        bookmark_file = resolve_bookmark_file(
            getattr(args, "profile", None), args.bookmark_file
        )
        launch_tui(config_path=bookmark_file, args=args)
        return

    # --json implies quiet (JSON to stdout, no human-readable noise)
    if args.json:
        args._results: list[RepoResult] = []
        args.quiet = True

    # Enable result collection for --notify / --history even without --json
    if args.notify or args.history:
        _ensure_results(args)

    if not args.quiet:
        print(Style.BRIGHT + "gitup" + Style.RESET_ALL + ": the git-repo-updater")
        print()

    if args.selftest:
        _selftest()
        return

    # --history / --history-stats: read-only analytics, exit after
    if args.history and not args.directories_to_update and not args.update:
        from gitup.history import show_history
        show_history(quiet=args.quiet)
        return
    if args.history_stats:
        from gitup.history import show_stats
        show_stats(quiet=args.quiet)
        return

    # --profile list
    profile = getattr(args, "profile", None)
    if profile == "list":
        print_profiles(quiet=args.quiet)
        return

    if args.bookmark_file:
        args.bookmark_file = os.path.expanduser(args.bookmark_file)

    # --profile all: iterate every named profile in sequence
    if profile == "all":
        profiles = list_profiles()
        if not profiles:
            print("No profiles found.", file=sys.stderr)
            return
        for pname in profiles:
            if not args.quiet:
                print(Style.BRIGHT + f"── profile: {pname} ──" + Style.RESET_ALL)
            pfile = get_profile_path(pname)
            _run_bookmark_ops(args, pfile)
            _run_update_cycle(args, pfile)
        return

    bookmark_file = resolve_bookmark_file(profile, args.bookmark_file)

    # Bookmark management operations (add / delete / list / clean)
    _run_bookmark_ops(args, bookmark_file)

    # --watch: repeat the update cycle at the given interval
    if args.watch:
        try:
            interval = _parse_interval(args.watch)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        run_num = 0
        while True:
            run_num += 1
            if not args.quiet:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                print(Style.BRIGHT + f"\n[{ts}] Run #{run_num}" + Style.RESET_ALL)
            _run_update_cycle(args, bookmark_file)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                raise
        return

    _run_update_cycle(args, bookmark_file)


def run() -> None:
    """Thin wrapper for main() that catches KeyboardInterrupts."""
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
