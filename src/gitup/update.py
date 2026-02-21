# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2018 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.
import copy
import io
import logging
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from glob import glob
import os
import re
import shlex
import sys

from colorama import Fore, Style
from git import RemoteReference as RemoteRef, Repo, exc
from git.util import RemoteProgress

logger = logging.getLogger(__name__)

__all__ = [
    "update_bookmarks",
    "update_directories",
    "status_bookmarks",
    "status_directories",
    "run_command",
    "RepoResult",
]

BOLD = Style.BRIGHT
BLUE = Fore.BLUE + BOLD
GREEN = Fore.GREEN + BOLD
RED = Fore.RED + BOLD
CYAN = Fore.CYAN + BOLD
YELLOW = Fore.YELLOW + BOLD
RESET = Style.RESET_ALL
DIM = Style.DIM

INDENT1 = " " * 3
INDENT2 = " " * 7
ERROR = RED + "Error:" + RESET

# Lock used for atomic stdout writes in parallel mode and for the shared
# results list.
_print_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RepoResult:
    """Structured result of updating or inspecting a single repository."""

    path: str
    name: str
    new_branches: list[str] = field(default_factory=list)
    new_tags: list[str] = field(default_factory=list)
    branches_updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.branches_updated or self.new_branches or self.new_tags:
            return "updated"
        return "up_to_date"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "status": self.status,
            "new_branches": self.new_branches,
            "new_tags": self.new_tags,
            "branches_updated": self.branches_updated,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Output helpers (quiet-aware, buffer-aware for parallel mode)
# ---------------------------------------------------------------------------


def _print(args: Namespace, *pargs: object, **kwargs: object) -> None:
    """Print only when not in quiet mode; writes to the per-repo buffer when
    running in parallel mode."""
    if getattr(args, "quiet", False):
        return
    buf = getattr(args, "_output_buffer", None)
    if buf is not None:
        kwargs.setdefault("file", buf)
    print(*pargs, **kwargs)


def _eprint(args: Namespace, *pargs: object, **kwargs: object) -> None:
    """Print an error — always shown regardless of quiet mode; writes to the
    per-repo error buffer when running in parallel mode."""
    buf = getattr(args, "_error_buffer", None)
    if buf is not None:
        kwargs.setdefault("file", buf)
        print(*pargs, **kwargs)
    else:
        print(*pargs, file=sys.stderr, **kwargs)


# ---------------------------------------------------------------------------
# Progress monitor
# ---------------------------------------------------------------------------


class _ProgressMonitor(RemoteProgress):
    """Displays relevant output during the fetching process."""

    def __init__(self, args: Namespace) -> None:
        super().__init__()
        self._started = False
        self._args = args

    def update(
        self,
        op_code: int,
        cur_count: str | float,
        max_count: str | float | None = None,
        message: str = "",
    ) -> None:
        """Called whenever progress changes. Overrides default behavior."""
        if getattr(self._args, "quiet", False):
            return
        if op_code & (self.COMPRESSING | self.RECEIVING):
            cur = str(int(cur_count))
            mx = str(int(max_count)) if max_count else None
            buf = getattr(self._args, "_output_buffer", None)
            out = buf if buf is not None else sys.stdout
            if op_code & self.BEGIN:
                print("\b, " if self._started else " (", end="", flush=True, file=out)
                if not self._started:
                    self._started = True
            if op_code & self.END:
                end = ")"
            elif mx:
                end = "\b" * (1 + len(cur) + len(mx))
            else:
                end = "\b" * len(cur)
            if mx:
                print(f"{cur}/{mx}", end=end, flush=True, file=out)
            else:
                print(cur, end=end, flush=True, file=out)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


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

    Uses ``os.path.relpath`` for plain directories.  For glob patterns
    (containing ``*`` or ``?``) the parent directory of the pattern is used as
    the reference point so the display name stays clean.
    """
    ref = base
    if "*" in base or "?" in base:
        ref = os.path.dirname(base)
    return os.path.relpath(path, ref)


def _format_age(seconds: float) -> str:
    """Return a human-readable representation of an age in seconds."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def _fetch_remotes(
    remotes: list, prune: bool, args: Namespace
) -> tuple[list[str], list[str], list[str]]:
    """Fetch a list of remotes, displaying progress info along the way.

    Returns a 3-tuple of ``(new_branches, new_tags, errors)`` — all as plain
    string lists — so that callers can build structured results.
    """

    def _get_name(ref: object) -> str:
        return ref.remote_head if isinstance(ref, RemoteRef) else ref.name

    info = [
        ("NEW_HEAD", "new branch", "new branches"),
        ("NEW_TAG", "new tag", "new tags"),
        ("FAST_FORWARD", "branch update", "branch updates"),
    ]
    up_to_date = BLUE + "up to date" + RESET

    all_new_branches: list[str] = []
    all_new_tags: list[str] = []
    all_errors: list[str] = []

    for remote in remotes:
        _print(args, INDENT2, "Fetching", BOLD + remote.name, end="")

        if not remote.config_reader.has_option("fetch"):
            _print(args, ":", YELLOW + "skipped:", "no configured refspec.")
            logger.debug("Remote %s has no configured refspec — skipped.", remote.name)
            continue

        try:
            results = remote.fetch(progress=_ProgressMonitor(args), prune=prune)
        except exc.GitCommandError as err:
            msg = re.sub(r"\s+", " ", err.stderr).strip()
            msg = re.sub(r"^stderr: *'(fatal: *)?", "", msg).strip("'")
            if not msg:
                command = " ".join(shlex.quote(arg) for arg in err.command)
                msg = f"{command} failed with status {err.status}."
            elif not msg.endswith("."):
                msg += "."
            logger.debug("Fetch error for remote %s: %s", remote.name, msg)
            all_errors.append(f"{remote.name}: {msg}")
            _print(args, ":", RED + "error:", msg)
            if getattr(args, "quiet", False):
                _eprint(args, INDENT2, "Fetching", BOLD + remote.name + ":", RED + "error:", msg)
            continue
        except AssertionError:
            msg = "something went wrong in GitPython, but the fetch might have been successful."
            logger.debug("AssertionError during fetch of remote %s.", remote.name)
            all_errors.append(f"{remote.name}: {msg}")
            _print(args, ":", RED + "error:", msg)
            if getattr(args, "quiet", False):
                _eprint(args, INDENT2, "Fetching", BOLD + remote.name + ":", RED + "error:", msg)
            continue

        rlist = []
        for attr, singular, plural in info:
            names = [
                _get_name(res.ref) for res in results if res.flags & getattr(res, attr)
            ]
            if names:
                if attr == "NEW_HEAD":
                    all_new_branches.extend(names)
                elif attr == "NEW_TAG":
                    all_new_tags.extend(names)
                desc = singular if len(names) == 1 else plural
                colored = GREEN + desc + RESET
                rlist.append(f"{colored} ({', '.join(names)})")
        summary = (", ".join(rlist) if rlist else up_to_date) + "."
        logger.debug("Remote %s fetch result: %s", remote.name, summary)
        _print(args, ":", summary)

    return all_new_branches, all_new_tags, all_errors


def _update_branch(
    repo: Repo, branch: object, args: Namespace, is_active: bool = False
) -> bool:
    """Update a single branch.

    Returns ``True`` if the branch was successfully fast-forwarded,
    ``False`` otherwise (skipped or already up-to-date).
    """
    _print(args, INDENT2, "Updating", BOLD + branch.name, end=": ")
    upstream = branch.tracking_branch()
    if not upstream:
        _print(args, YELLOW + "skipped:", "no upstream is tracked.")
        logger.debug("Branch %s has no upstream — skipped.", branch.name)
        return False
    try:
        branch.commit
    except ValueError:
        _print(args, YELLOW + "skipped:", "branch has no revisions.")
        logger.debug("Branch %s has no revisions — skipped.", branch.name)
        return False
    try:
        upstream.commit
    except ValueError:
        _print(args, YELLOW + "skipped:", "upstream does not exist.")
        logger.debug("Upstream of %s does not exist — skipped.", branch.name)
        return False

    try:
        base = repo.git.merge_base(branch.commit, upstream.commit)
    except exc.GitCommandError as err:
        logger.debug("merge_base failed for %s: %s", branch.name, err)
        _print(args, YELLOW + "skipped:", "can't find merge base with upstream.")
        return False

    if repo.commit(base) == upstream.commit:
        _print(args, BLUE + "up to date", end=".\n")
        logger.debug("Branch %s is up to date.", branch.name)
        return False

    if is_active:
        try:
            repo.git.merge(upstream.name, ff_only=True)
            _print(args, GREEN + "done", end=".\n")
            logger.debug("Fast-forwarded active branch %s.", branch.name)
            return True
        except exc.GitCommandError as err:
            msg = err.stderr
            if "local changes" in msg and "would be overwritten" in msg:
                _print(args, YELLOW + "skipped:", "uncommitted changes.")
                logger.debug("Branch %s has uncommitted changes — skipped.", branch.name)
            else:
                _print(args, YELLOW + "skipped:", "not possible to fast-forward.")
                logger.debug("Branch %s cannot be fast-forwarded.", branch.name)
            return False
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
            return False
        else:
            repo.git.branch(branch.name, upstream.name, force=True)
            _print(args, GREEN + "done", end=".\n")
            logger.debug("Updated inactive branch %s.", branch.name)
            return True


def _update_repository(repo: Repo, repo_name: str, args: Namespace) -> None:
    """Update a single git repository by fetching remotes and fast-forwarding.

    The specific actions depend on the arguments given. We will fetch all
    remotes if *args.current_only* is ``False``, or only the remote tracked by
    the current branch if ``True``. If *args.fetch_only* is ``False``, we will
    also update all fast-forwardable branches that are tracking valid
    upstreams. If *args.prune* is ``True``, remote-tracking branches that no
    longer exist on their remote after fetching will be deleted.

    When *args._results* is a list the ``RepoResult`` for this repo is
    appended to it (used by ``--json``).
    """
    result = RepoResult(path=str(repo.working_dir), name=repo_name)

    _print(args, INDENT1, BOLD + repo_name + ":")
    logger.debug("Updating repository: %s", repo_name)

    try:
        active = repo.active_branch
    except TypeError:  # HEAD is detached
        active = None

    if args.current_only:
        if not active:
            _eprint(
                args,
                INDENT2,
                ERROR,
                "--current-only doesn't make sense with a detached HEAD.",
            )
            result.errors.append("detached HEAD with --current-only")
            _store_result(args, result)
            return
        ref = active.tracking_branch()
        if not ref:
            _eprint(args, INDENT2, ERROR, "no remote tracked by current branch.")
            result.errors.append("no remote tracked by current branch")
            _store_result(args, result)
            return
        remotes = [repo.remotes[ref.remote_name]]
    else:
        remotes = repo.remotes

    if not remotes:
        _eprint(args, INDENT2, ERROR, "no remotes configured to fetch.")
        result.errors.append("no remotes configured to fetch")
        _store_result(args, result)
        return

    new_branches, new_tags, errors = _fetch_remotes(remotes, args.prune, args)
    result.new_branches.extend(new_branches)
    result.new_tags.extend(new_tags)
    result.errors.extend(errors)

    if not args.fetch_only:
        for branch in sorted(repo.heads, key=lambda b: b.name):
            updated = _update_branch(repo, branch, args, is_active=(branch == active))
            if updated:
                result.branches_updated.append(branch.name)

    _store_result(args, result)


def _store_result(args: Namespace, result: RepoResult) -> None:
    """Append *result* to ``args._results`` if the list exists."""
    results_list = getattr(args, "_results", None)
    if results_list is not None:
        with _print_lock:
            results_list.append(result)


def _status_repository(repo: Repo, repo_name: str, args: Namespace) -> None:
    """Show local status of a repository without fetching."""
    _print(args, INDENT1, BOLD + repo_name + ":")

    # --- active branch ---
    try:
        active = repo.active_branch
        branch_name = active.name
    except TypeError:
        active = None
        branch_name = "(detached HEAD)"

    upstream = active.tracking_branch() if active else None
    upstream_name = upstream.name if upstream else None

    # --- ahead / behind ---
    ahead = behind = 0
    if active and upstream:
        try:
            ahead = sum(
                1 for _ in repo.iter_commits(f"{upstream_name}..{branch_name}")
            )
            behind = sum(
                1 for _ in repo.iter_commits(f"{branch_name}..{upstream_name}")
            )
        except exc.GitCommandError:
            pass

    # --- dirty state ---
    dirty = repo.is_dirty(untracked_files=True)
    n_modified = len(repo.index.diff(None)) if dirty else 0
    n_staged = len(repo.index.diff(repo.head.commit)) if (dirty and not repo.head.is_detached) else 0
    n_untracked = len(repo.untracked_files)

    # --- last fetch time ---
    fetch_head = os.path.join(repo.git_dir, "FETCH_HEAD")
    if os.path.exists(fetch_head):
        age_str = _format_age(time.time() - os.path.getmtime(fetch_head))
    else:
        age_str = "never"

    # --- format output ---
    if upstream_name:
        branch_label = (
            BLUE + branch_name + RESET
            + DIM + " → " + RESET
            + BOLD + upstream_name + RESET
        )
        if ahead == 0 and behind == 0:
            sync_str = BLUE + "up to date" + RESET
        else:
            parts = []
            if ahead:
                parts.append(GREEN + f"↑{ahead}" + RESET)
            if behind:
                parts.append(YELLOW + f"↓{behind}" + RESET)
            sync_str = "  ".join(parts)
    else:
        branch_label = BLUE + branch_name + RESET + DIM + "  (no upstream)" + RESET
        sync_str = ""

    if dirty:
        dirty_parts = []
        if n_staged:
            dirty_parts.append(f"{n_staged} staged")
        if n_modified:
            dirty_parts.append(f"{n_modified} modified")
        if n_untracked:
            dirty_parts.append(f"{n_untracked} untracked")
        state_str = YELLOW + "dirty" + RESET + DIM + f" ({', '.join(dirty_parts)})" + RESET
    else:
        state_str = GREEN + "clean" + RESET

    _print(args, INDENT2, "branch    ", branch_label, sync_str)
    _print(args, INDENT2, "state     ", state_str)
    _print(args, INDENT2, "last fetch", DIM + age_str + RESET)


def _run_command(repo: Repo, repo_name: str, args: Namespace) -> None:
    """Run an arbitrary shell command on the given repository."""
    _print(args, INDENT1, BOLD + repo_name + ":")

    cmd = shlex.split(args.command)
    try:
        out = repo.git.execute(cmd, with_extended_output=True, with_exceptions=False)
    except exc.GitCommandNotFound as err:
        _eprint(args, INDENT2, ERROR, err)
        return

    if not getattr(args, "quiet", False):
        for line in out[1].splitlines() + out[2].splitlines():
            _print(args, INDENT2, line)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(base_path: str, callback: object, args: Namespace) -> None:
    """Apply *callback* on each valid git repo found under *base_path*.

    When ``args.parallel`` is greater than 1, repos are processed concurrently
    using a thread pool. Each thread buffers its stdout/stderr independently so
    that output is printed atomically per-repo.
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
            _eprint(args, ERROR, BOLD + base, "doesn't exist!")
            return
        valid = _collect(paths, max_depth)
    except exc.InvalidGitRepositoryError:
        if not os.path.isdir(base) or args.max_depth == 0:
            _eprint(args, ERROR, BOLD + base, "isn't a repository!")
            return
        valid = _collect([base], max_depth)

    base = os.path.abspath(base)
    suffix = "" if len(valid) == 1 else "s"
    _print(args, BOLD + base, "({0} repo{1}):".format(len(valid), suffix))

    valid = [os.path.abspath(path) for path in valid]
    sorted_paths = sorted(
        (_get_basename(base, path), path) for path in valid
    )

    parallel = getattr(args, "parallel", 1)
    if parallel > 1 and len(sorted_paths) > 1:
        _dispatch_parallel(sorted_paths, callback, args)
    else:
        for name, path in sorted_paths:
            callback(Repo(path), name, args)


def _dispatch_parallel(
    sorted_paths: list[tuple[str, str]],
    callback: object,
    base_args: Namespace,
) -> None:
    """Run *callback* on each (name, path) pair using a thread pool.

    Each worker gets its own copy of *args* with independent stdout/stderr
    string buffers. Output is flushed to the real streams atomically once a
    worker finishes, preserving per-repo coherence.
    """

    def _run_one(name_path: tuple[str, str]) -> tuple[str, str]:
        name, path = name_path
        local_args = copy.copy(base_args)
        local_args._output_buffer = io.StringIO()
        local_args._error_buffer = io.StringIO()
        callback(Repo(path), name, local_args)
        return (
            local_args._output_buffer.getvalue(),
            local_args._error_buffer.getvalue(),
        )

    max_workers = base_args.parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one, np): np for np in sorted_paths}
        for future in as_completed(futures):
            try:
                out, err = future.result()
            except Exception as exc_:
                out, err = "", f"{ERROR} parallel worker failed: {exc_}\n"
            with _print_lock:
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                if err:
                    sys.stderr.write(err)
                    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Utility functions (public)
# ---------------------------------------------------------------------------


def is_comment(path: str) -> bool:
    """Return ``True`` if the line starts with a ``#`` symbol."""
    return path.lstrip().startswith("#")


def get_comment(path: str) -> str:
    """Return the content of a comment line, stripped of the ``#`` marker."""
    return path.lstrip().lstrip("#").strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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


def status_bookmarks(bookmarks: list[str], args: Namespace) -> None:
    """Show the local status of all bookmarked repositories (no fetch)."""
    if not bookmarks:
        _print(args, "You don't have any bookmarks configured! Get help with 'gitup -h'.")
        return
    for path in bookmarks:
        _dispatch(path, _status_repository, args)


def status_directories(paths: list[str], args: Namespace) -> None:
    """Show the local status of repositories under the given paths (no fetch)."""
    for path in paths:
        _dispatch(path, _status_repository, args)


def run_command(paths: list[str], args: Namespace) -> None:
    """Run an arbitrary shell command on all repos."""
    for path in paths:
        _dispatch(path, _run_command, args)
