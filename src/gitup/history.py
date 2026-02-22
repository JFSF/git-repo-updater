# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""SQLite-based update history and analytics.

The database is stored at ``~/.local/share/gitup/history.db`` (respecting
``XDG_DATA_HOME``).

Schema
------
``updates`` table columns:

* ``id``               — auto-increment primary key
* ``timestamp``        — ISO-8601 string (seconds precision)
* ``session``          — UUID grouping all repos updated in one ``gitup`` run
* ``path``             — absolute path of the repository
* ``name``             — display name used by gitup
* ``status``           — ``"updated"``, ``"up_to_date"``, or ``"error"``
* ``new_branches``     — JSON array
* ``new_tags``         — JSON array
* ``branches_updated`` — JSON array
* ``errors``           — JSON array
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime

from colorama import Fore, Style

__all__ = [
    "get_history_db_path",
    "record_updates",
    "show_history",
    "show_stats",
]

BOLD = Style.BRIGHT
DIM = Style.DIM
YELLOW = Fore.YELLOW + BOLD
GREEN = Fore.GREEN + BOLD
RED = Fore.RED + BOLD
BLUE = Fore.BLUE + BOLD
CYAN = Fore.CYAN + BOLD
RESET = Style.RESET_ALL

INDENT1 = " " * 3
INDENT2 = " " * 7

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS updates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    session          TEXT    NOT NULL,
    path             TEXT    NOT NULL,
    name             TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    new_branches     TEXT    NOT NULL DEFAULT '[]',
    new_tags         TEXT    NOT NULL DEFAULT '[]',
    branches_updated TEXT    NOT NULL DEFAULT '[]',
    errors           TEXT    NOT NULL DEFAULT '[]'
)
"""


def get_history_db_path() -> str:
    """Return the path to the history SQLite database."""
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join("~", ".local", "share")
    return os.path.join(os.path.expanduser(xdg_data), "gitup", "history.db")


def _connect() -> sqlite3.Connection:
    db_path = get_history_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def record_updates(results: list, session_id: str | None = None) -> None:
    """Persist *results* (list of :class:`~gitup.update.RepoResult`) to the DB.

    All rows in a single call share the same *session_id* UUID so that the
    full update run can later be reconstructed.
    """
    if not results:
        return
    sid = session_id or str(uuid.uuid4())
    ts = datetime.now().isoformat(timespec="seconds")
    rows = [
        (
            ts,
            sid,
            r.path,
            r.name,
            r.status,
            json.dumps(r.new_branches),
            json.dumps(r.new_tags),
            json.dumps(r.branches_updated),
            json.dumps(r.errors),
        )
        for r in results
    ]
    with _connect() as conn:
        conn.executemany(
            """INSERT INTO updates
               (timestamp, session, path, name, status,
                new_branches, new_tags, branches_updated, errors)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def show_history(n: int = 20, quiet: bool = False) -> None:
    """Print the last *n* update records to stdout."""
    try:
        conn = _connect()
    except Exception:
        if not quiet:
            print("No history available yet.")
        return

    rows = conn.execute(
        "SELECT * FROM updates ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()

    if not rows:
        if not quiet:
            print("No history available yet. Run gitup to record updates.")
        return

    if quiet:
        return

    print(YELLOW + f"Last {min(n, len(rows))} update records:" + RESET)
    for row in reversed(rows):
        status = row["status"]
        if status == "updated":
            color = GREEN
        elif status == "error":
            color = RED
        else:
            color = BLUE

        branches = json.loads(row["branches_updated"])
        new_br = json.loads(row["new_branches"])
        tags = json.loads(row["new_tags"])
        errors = json.loads(row["errors"])

        detail_parts = []
        if branches:
            detail_parts.append(f"{', '.join(branches)} fast-forwarded")
        if new_br:
            detail_parts.append(f"{len(new_br)} new branch{'es' if len(new_br) != 1 else ''}")
        if tags:
            detail_parts.append(f"{len(tags)} new tag{'s' if len(tags) != 1 else ''}")
        if errors:
            detail_parts.append(errors[0])
        detail = f"  ({', '.join(detail_parts)})" if detail_parts else ""

        ts_short = row["timestamp"][:16]  # YYYY-MM-DDTHH:MM
        print(
            f"  {DIM}{ts_short}{RESET}  "
            f"{BOLD}{row['name']:<30}{RESET}  "
            f"{color}{status:<12}{RESET}"
            f"{DIM}{detail}{RESET}"
        )


def show_stats(quiet: bool = False) -> None:
    """Print aggregate analytics to stdout."""
    try:
        conn = _connect()
    except Exception:
        if not quiet:
            print("No history available yet.")
        return

    total_runs = conn.execute(
        "SELECT COUNT(DISTINCT session) FROM updates"
    ).fetchone()[0]

    if total_runs == 0:
        if not quiet:
            print("No history available yet. Run gitup to record updates.")
        conn.close()
        return

    total_repos = conn.execute("SELECT COUNT(*) FROM updates").fetchone()[0]
    total_updated = conn.execute(
        "SELECT COUNT(*) FROM updates WHERE status='updated'"
    ).fetchone()[0]
    total_errors = conn.execute(
        "SELECT COUNT(*) FROM updates WHERE status='error'"
    ).fetchone()[0]

    # Per-repo stats
    per_repo = conn.execute(
        """SELECT name, path,
                  COUNT(*) as runs,
                  SUM(CASE WHEN status='updated' THEN 1 ELSE 0 END) as updated,
                  SUM(CASE WHEN status='error'   THEN 1 ELSE 0 END) as errors
           FROM updates
           GROUP BY path
           ORDER BY runs DESC
           LIMIT 10"""
    ).fetchall()

    # Most active hour
    peak_hour_row = conn.execute(
        """SELECT SUBSTR(timestamp, 12, 2) as hour, COUNT(*) as cnt
           FROM updates
           GROUP BY hour
           ORDER BY cnt DESC
           LIMIT 1"""
    ).fetchone()

    conn.close()

    if quiet:
        return

    success_pct = round(100 * (total_repos - total_errors) / total_repos) if total_repos else 0
    avg_repos = round(total_repos / total_runs, 1) if total_runs else 0

    print(YELLOW + "Update statistics:" + RESET)
    print(f"  {BOLD}Sessions recorded :{RESET} {total_runs}")
    print(f"  {BOLD}Total repo updates :{RESET} {total_repos}")
    print(f"  {BOLD}Successful         :{RESET} {total_repos - total_errors}  ({success_pct}%)")
    print(f"  {BOLD}Errors             :{RESET} {total_errors}")
    print(f"  {BOLD}Avg repos/session  :{RESET} {avg_repos}")
    if peak_hour_row and peak_hour_row["hour"]:
        print(f"  {BOLD}Peak update hour   :{RESET} {peak_hour_row['hour']}:00")
    print()

    if per_repo:
        # Bar chart column width
        max_runs = per_repo[0]["runs"]
        bar_width = 20
        print(BOLD + "  Most active repositories:" + RESET)
        for row in per_repo:
            pct = round(bar_width * row["runs"] / max_runs) if max_runs else 0
            bar = GREEN + "█" * pct + RESET + DIM + "░" * (bar_width - pct) + RESET
            ok_pct = round(100 * (row["runs"] - row["errors"]) / row["runs"]) if row["runs"] else 0
            print(
                f"    {BOLD}{row['name']:<25}{RESET} {bar}  "
                f"{row['runs']} runs  {DIM}({ok_pct}% ok){RESET}"
            )
