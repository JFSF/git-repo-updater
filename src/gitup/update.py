# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.
import logging
from argparse import Namespace
from glob import glob
import os
import re
import shlex
import sys

from colorama import Fore, Style
from git import RemoteReference as RemoteRef, Repo, exc
from git.util import RemoteProgress

logger = logging.getLogger(__name__)

__all__ = ["update_bookmarks", "update_directories", "run_command"]

BOLD = Style.BRIGHT
BLUE = Fore.BLUE + BOLD
GREEN = Fore.GREEN + BOLD
RED = Fore.RED + BOLD
CYAN = Fore.CYAN + BOLD
YELLOW = Fore.YELLOW + BOLD
RESET = Style.RESET_ALL

INDENT1 = " " * 3
INDENT2 = " " * 7
ERROR = RED + "Error:" + RESET


def _print(args: Namespace, *pargs: object, **kwargs: object) -> None:
    """Print only when not in quiet mode."""
    if not getattr(args, "quiet", False):
        print(*pargs, **kwargs)


def _eprint(*pargs: object, **kwargs: object) -> None:
    """Print an error message — always shown regardless of quiet mode."""
    print(*pargs, file=sys.stderr, **kwargs)


class _ProgressMonitor(RemoteProgress):
    """Displays relevant output during the fetching process."""

    def __init__(self, quiet: bool = False) -> None:
        super().__init__()
        self._started = False
        self._quiet = quiet

    def update(
        self,
        op_code: int,
        cur_count: str | float,
        max_count: str | float | None = None,
        message: str = "",
    ) -> None:
        """Called whenever progress changes. Overrides default behavior."""
        if self._quiet:
            return
        if op_code & (self.COMPRESSING | self.RECEIVING):
            cur = str(int(cur_count))
            mx = str(int(max_count)) if max_count else None
            if op_code & self.BEGIN:
                print("\b, " if self._started else " (", end="", flush=True)
                if not self._started:
                    self._started = True
            if op_code & self.END:
                end = ")"
            elif mx:
                end = "\b" * (1 + len(cur) + len(mx))
            else:
                end = "\b" * len(cur)
            if mx:
                print(f"{cur}/{mx}", end=end, flush=True)
            else:
                print(cur, end=end, flush=True)


def _collect(paths: list[str], max_depth: int) -> list[str]:
    """Return all valid repo paths in the given paths, recursively.

    Traversal order is deterministic: directories are sorted alphabetically.
    """
    if max_depth == 0:
        return []

    valid = []
    for path in paths:
        try:
            Repo(path)
            valid.append(path)
        except exc.InvalidGitRepositoryError:
            if not os.path.isdir(path):
                continue
            children = [
                os.path.join(path, entry) for entry in sorted(os.listdir(path))
            ]
            logger.debug("Recursing into %s (%d children)", path, len(children))
            valid += _collect(children, max_depth - 1)
        except exc.NoSuchPathError:
            continue
    return valid


def _get_basename(base: str, path: str) -> str:
    """Return a display name for *path* relative to *base*.

    Uses ``os.path.relpath`` when *base* is a plain directory.  For glob
    patterns (containing ``*`` or ``?``) the parent directory of the pattern
    is used as the reference point so the display name stays clean.
    """
    ref = base
    if "*" in base or "?" in base:
        ref = os.path.dirname(base)
    return os.path.relpath(path, ref)


def _fetch_remotes(remotes: list, prune: bool, quiet: bool = False) -> None:
    """Fetch a list of remotes, displaying progress info along the way."""

    def _get_name(ref: object) -> str:
        """Return the local name of a remote or tag reference."""
        return ref.remote_head if isinstance(ref, RemoteRef) else ref.name

    # TODO: missing branch deleted (via --prune):
    info = [
        ("NEW_HEAD", "new branch", "new branches"),
        ("NEW_TAG", "new tag", "new tags"),
        ("FAST_FORWARD", "branch update", "branch updates"),
    ]
    up_to_date = BLUE + "up to date" + RESET

    for remote in remotes:
        if not quiet:
            print(INDENT2, "Fetching", BOLD + remote.name, end="")

        if not remote.config_reader.has_option("fetch"):
            if not quiet:
                print(":", YELLOW + "skipped:", "no configured refspec.")
            logger.debug("Remote %s has no configured refspec — skipped.", remote.name)
            continue

        try:
            results = remote.fetch(
                progress=_ProgressMonitor(quiet=quiet), prune=prune
            )
        except exc.GitCommandError as err:
            # We should have to do this ourselves, but GitPython doesn't give
            # us a sensible way to get the raw stderr...
            msg = re.sub(r"\s+", " ", err.stderr).strip()
            msg = re.sub(r"^stderr: *'(fatal: *)?", "", msg).strip("'")
            if not msg:
                command = " ".join(shlex.quote(arg) for arg in err.command)
                msg = f"{command} failed with status {err.status}."
            elif not msg.endswith("."):
                msg += "."
            logger.debug("Fetch error for remote %s: %s", remote.name, msg)
            if not quiet:
                print(":", RED + "error:", msg)
            else:
                _eprint(INDENT2, "Fetching", BOLD + remote.name + ":", RED + "error:", msg)
            return
        except AssertionError:  # Seems to be the result of a bug in GitPython
            # This happens when git initiates an auto-gc during fetch:
            msg = "something went wrong in GitPython, but the fetch might have been successful."
            logger.debug("AssertionError during fetch of remote %s.", remote.name)
            if not quiet:
                print(":", RED + "error:", msg)
            else:
                _eprint(INDENT2, "Fetching", BOLD + remote.name + ":", RED + "error:", msg)
            return

        rlist = []
        for attr, singular, plural in info:
            names = [
                _get_name(res.ref) for res in results if res.flags & getattr(res, attr)
            ]
            if names:
                desc = singular if len(names) == 1 else plural
                colored = GREEN + desc + RESET
                rlist.append(f"{colored} ({', '.join(names)})")
        summary = (", ".join(rlist) if rlist else up_to_date) + "."
        logger.debug("Remote %s fetch result: %s", remote.name, summary)
        if not quiet:
            print(":", summary)


def _update_branch(
    repo: Repo, branch: object, args: Namespace, is_active: bool = False
) -> None:
    """Update a single branch."""
    quiet = getattr(args, "quiet", False)
    _print(args, INDENT2, "Updating", BOLD + branch.name, end=": ")
    upstream = branch.tracking_branch()
    if not upstream:
        _print(args, YELLOW + "skipped:", "no upstream is tracked.")
        logger.debug("Branch %s has no upstream — skipped.", branch.name)
        return
    try:
        branch.commit
    except ValueError:
        _print(args, YELLOW + "skipped:", "branch has no revisions.")
        logger.debug("Branch %s has no revisions — skipped.", branch.name)
        return
    try:
        upstream.commit
    except ValueError:
        _print(args, YELLOW + "skipped:", "upstream does not exist.")
        logger.debug("Upstream of %s does not exist — skipped.", branch.name)
        return

    try:
        base = repo.git.merge_base(branch.commit, upstream.commit)
    except exc.GitCommandError as err:
        logger.debug("merge_base failed for %s: %s", branch.name, err)
        _print(args, YELLOW + "skipped:", "can't find merge base with upstream.")
        return

    if repo.commit(base) == upstream.commit:
        _print(args, BLUE + "up to date", end=".\n")
        logger.debug("Branch %s is up to date.", branch.name)
        return

    if is_active:
        try:
            repo.git.merge(upstream.name, ff_only=True)
            _print(args, GREEN + "done", end=".\n")
            logger.debug("Fast-forwarded active branch %s.", branch.name)
        except exc.GitCommandError as err:
            msg = err.stderr
            if "local changes" in msg and "would be overwritten" in msg:
                _print(args, YELLOW + "skipped:", "uncommitted changes.")
                logger.debug("Branch %s has uncommitted changes — skipped.", branch.name)
            else:
                _print(args, YELLOW + "skipped:", "not possible to fast-forward.")
                logger.debug("Branch %s cannot be fast-forwarded.", branch.name)
    else:
        status = repo.git.merge_base(
            branch.commit,
            upstream.commit,
            is_ancestor=True,
            with_extended_output=True,
            with_exceptions=False,
        )[0]
        if status != 0:
            _print(args, YELLOW + "skipped:", "not possible to fast-forward.")
            logger.debug("Branch %s cannot be fast-forwarded (status %s).", branch.name, status)
        else:
            repo.git.branch(branch.name, upstream.name, force=True)
            _print(args, GREEN + "done", end=".\n")
            logger.debug("Updated inactive branch %s.", branch.name)


def _update_repository(repo: Repo, repo_name: str, args: Namespace) -> None:
    """Update a single git repository by fetching remotes and rebasing/merging.

    The specific actions depend on the arguments given. We will fetch all
    remotes if *args.current_only* is ``False``, or only the remote tracked by
    the current branch if ``True``. If *args.fetch_only* is ``False``, we will
    also update all fast-forwardable branches that are tracking valid
    upstreams. If *args.prune* is ``True``, remote-tracking branches that no
    longer exist on their remote after fetching will be deleted.
    """
    quiet = getattr(args, "quiet", False)
    _print(args, INDENT1, BOLD + repo_name + ":")
    logger.debug("Updating repository: %s", repo_name)

    try:
        active = repo.active_branch
    except TypeError:  # Happens when HEAD is detached
        active = None
    if args.current_only:
        if not active:
            _eprint(INDENT2, ERROR, "--current-only doesn't make sense with a detached HEAD.")
            return
        ref = active.tracking_branch()
        if not ref:
            _eprint(INDENT2, ERROR, "no remote tracked by current branch.")
            return
        remotes = [repo.remotes[ref.remote_name]]
    else:
        remotes = repo.remotes

    if not remotes:
        _eprint(INDENT2, ERROR, "no remotes configured to fetch.")
        return
    _fetch_remotes(remotes, args.prune, quiet=quiet)

    if not args.fetch_only:
        for branch in sorted(repo.heads, key=lambda b: b.name):
            _update_branch(repo, branch, args, is_active=(branch == active))


def _run_command(repo: Repo, repo_name: str, args: Namespace) -> None:
    """Run an arbitrary shell command on the given repository."""
    _print(args, INDENT1, BOLD + repo_name + ":")

    cmd = shlex.split(args.command)
    try:
        out = repo.git.execute(cmd, with_extended_output=True, with_exceptions=False)
    except exc.GitCommandNotFound as err:
        _eprint(INDENT2, ERROR, err)
        return

    if not getattr(args, "quiet", False):
        for line in out[1].splitlines() + out[2].splitlines():
            print(INDENT2, line)


def _dispatch(base_path: str, callback: object, args: Namespace) -> None:
    """Apply a callback function on each valid repo in the given path.

    Determine whether the directory is a git repo on its own, a directory of
    git repositories, a shell glob pattern, or something invalid. If the first,
    apply the callback on it; if the second or third, apply the callback on all
    repositories contained within; if the last, print an error.

    The given args are passed directly to the callback function after the repo.
    """
    base = os.path.expanduser(base_path)
    max_depth = args.max_depth
    if max_depth >= 0:
        max_depth += 1

    try:
        Repo(base)
        valid = [base]
    except exc.NoSuchPathError:
        if is_comment(base):
            comment = get_comment(base)
            if comment:
                _print(args, CYAN + BOLD + comment)
            return
        paths = glob(base)
        if not paths:
            _eprint(ERROR, BOLD + base, "doesn't exist!")
            return
        valid = _collect(paths, max_depth)
    except exc.InvalidGitRepositoryError:
        if not os.path.isdir(base) or args.max_depth == 0:
            _eprint(ERROR, BOLD + base, "isn't a repository!")
            return
        valid = _collect([base], max_depth)

    base = os.path.abspath(base)
    suffix = "" if len(valid) == 1 else "s"
    _print(args, BOLD + base, "({0} repo{1}):".format(len(valid), suffix))

    valid = [os.path.abspath(path) for path in valid]
    paths = [(_get_basename(base, path), path) for path in valid]
    for name, path in sorted(paths):
        callback(Repo(path), name, args)


def is_comment(path: str) -> bool:
    """Return True if the line starts with a ``#`` symbol."""
    return path.lstrip().startswith("#")


def get_comment(path: str) -> str:
    """Return the string content after stripping the leading ``#`` comment marker."""
    return path.lstrip().lstrip("#").strip()


def update_bookmarks(bookmarks: list[str], args: Namespace) -> None:
    """Loop through and update all bookmarks."""
    if not bookmarks:
        _print(args, "You don't have any bookmarks configured! Get help with 'gitup -h'.")
        return

    for path in bookmarks:
        _dispatch(path, _update_repository, args)


def update_directories(paths: list[str], args: Namespace) -> None:
    """Update a list of directories supplied by command arguments."""
    for path in paths:
        _dispatch(path, _update_repository, args)


def run_command(paths: list[str], args: Namespace) -> None:
    """Run an arbitrary shell command on all repos."""
    for path in paths:
        _dispatch(path, _run_command, args)
