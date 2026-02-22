# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Textual-based interactive TUI for gitup.

Requires the optional ``tui`` extra::

    pip install gitup[tui]
    # or
    uv sync --extra tui

Launch with ``gitup --tui``.

Key bindings
------------
U   Update all bookmarks
S   Show local status (no fetch)
A   Add a new bookmark (prompts for path)
D   Delete the selected bookmark
L   List all bookmarks in the log
R   Refresh the bookmark list
Q   Quit
"""

from __future__ import annotations

import io
import sys
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        ProgressBar,
        RichLog,
        Static,
    )
    from textual import work

    _TEXTUAL_AVAILABLE = True
except ImportError:
    _TEXTUAL_AVAILABLE = False


__all__ = ["launch_tui", "TEXTUAL_AVAILABLE"]

TEXTUAL_AVAILABLE = _TEXTUAL_AVAILABLE


def launch_tui(config_path: str | None = None, args: Namespace | None = None) -> None:
    """Launch the interactive TUI.  Raises ``ImportError`` if textual is not installed."""
    if not _TEXTUAL_AVAILABLE:
        raise ImportError(
            "The 'textual' package is required for the TUI.\n"
            "Install it with:  pip install gitup[tui]"
        )
    app = GitupApp(config_path=config_path, args=args)
    app.run()


if _TEXTUAL_AVAILABLE:

    class _AddBookmarkScreen(ModalScreen[str | None]):
        """Modal dialog that prompts for a path to add as a bookmark."""

        CSS = """
        _AddBookmarkScreen {
            align: center middle;
        }
        #dialog {
            width: 60;
            height: 9;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        #dialog Label {
            margin-bottom: 1;
        }
        #buttons {
            margin-top: 1;
            layout: horizontal;
            height: 3;
            align: right middle;
        }
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Enter the path to bookmark:")
                yield Input(placeholder="/path/to/repo", id="path-input")
                with Horizontal(id="buttons"):
                    yield Button("Add", variant="primary", id="btn-add")
                    yield Button("Cancel", id="btn-cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-add":
                path = self.query_one("#path-input", Input).value.strip()
                self.dismiss(path if path else None)
            else:
                self.dismiss(None)

        def on_input_submitted(self, event: Input.Submitted) -> None:
            path = event.value.strip()
            self.dismiss(path if path else None)

    class GitupApp(App):
        """The gitup interactive terminal application."""

        TITLE = "gitup — the git-repo-updater"
        SUB_TITLE = "Press ? for help"

        CSS = """
        Screen {
            layout: vertical;
        }
        #main-row {
            height: 1fr;
            layout: horizontal;
        }
        #bookmarks-panel {
            width: 40%;
            border: solid $primary;
            padding: 0 1;
        }
        #log-panel {
            width: 60%;
            border: solid $primary;
            padding: 0 1;
        }
        .panel-title {
            text-style: bold;
            color: $primary;
            padding: 0 0 1 0;
        }
        #bookmarks-table {
            height: 1fr;
        }
        #activity-log {
            height: 1fr;
        }
        #progress-row {
            height: 3;
            padding: 0 1;
            layout: horizontal;
            align: left middle;
        }
        #progress-label {
            width: 20;
            content-align: left middle;
        }
        #update-progress {
            width: 1fr;
        }
        """

        BINDINGS = [
            Binding("u", "update_all", "Update All"),
            Binding("s", "show_status", "Status"),
            Binding("a", "add_bookmark", "Add"),
            Binding("d", "delete_selected", "Delete"),
            Binding("r", "refresh", "Refresh"),
            Binding("l", "list_bookmarks", "List"),
            Binding("q", "quit", "Quit"),
        ]

        _progress_total: reactive[int] = reactive(0)
        _progress_done: reactive[int] = reactive(0)
        _busy: reactive[bool] = reactive(False)

        def __init__(
            self,
            config_path: str | None = None,
            args: Namespace | None = None,
        ) -> None:
            super().__init__()
            self._config_path = config_path
            self._base_args = args or _default_tui_args()

        # ------------------------------------------------------------------ #
        # Layout                                                               #
        # ------------------------------------------------------------------ #

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="main-row"):
                with Vertical(id="bookmarks-panel"):
                    yield Label("BOOKMARKS", classes="panel-title")
                    yield DataTable(id="bookmarks-table")
                with Vertical(id="log-panel"):
                    yield Label("ACTIVITY", classes="panel-title")
                    yield RichLog(id="activity-log", highlight=True, markup=True)
            with Horizontal(id="progress-row"):
                yield Static("", id="progress-label")
                yield ProgressBar(id="update-progress", total=100, show_eta=False)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#bookmarks-table", DataTable)
            table.add_columns("Repository", "Status")
            table.cursor_type = "row"
            self._load_bookmarks()
            self._log("gitup TUI ready. Press [bold]U[/bold] to update all bookmarks.")

        # ------------------------------------------------------------------ #
        # Helpers                                                              #
        # ------------------------------------------------------------------ #

        def _load_bookmarks(self) -> None:
            from gitup.config import get_bookmarks
            table = self.query_one("#bookmarks-table", DataTable)
            table.clear()
            for bm in get_bookmarks(self._config_path):
                table.add_row(bm, "—", key=bm)

        def _log(self, text: str) -> None:
            log = self.query_one("#activity-log", RichLog)
            log.write(text)

        def _log_from_thread(self, text: str) -> None:
            self.call_from_thread(self._log, text)

        def _set_row_status(self, path: str, status: str) -> None:
            table = self.query_one("#bookmarks-table", DataTable)
            icon = {"updated": "✓", "up_to_date": "—", "error": "✗"}.get(status, "?")
            color = {"updated": "green", "up_to_date": "blue", "error": "red"}.get(status, "white")
            try:
                table.update_cell(path, "Status", f"[{color}]{icon}[/{color}]")
            except Exception:
                pass  # row might not exist

        def _set_row_status_from_thread(self, path: str, status: str) -> None:
            self.call_from_thread(self._set_row_status, path, status)

        def _set_progress(self, done: int, total: int, label: str = "") -> None:
            bar = self.query_one("#update-progress", ProgressBar)
            lbl = self.query_one("#progress-label", Static)
            bar.update(total=total, progress=done)
            lbl.update(label)

        def _set_progress_from_thread(self, done: int, total: int, label: str = "") -> None:
            self.call_from_thread(self._set_progress, done, total, label)

        def _make_args(self) -> Namespace:
            import copy
            args = copy.copy(self._base_args)
            args._results = []
            args._on_repo_done = self._on_repo_done_callback
            return args

        def _on_repo_done_callback(self, result: object) -> None:
            """Called from a worker thread whenever a repo finishes updating."""
            self._progress_done += 1
            self._set_progress_from_thread(
                self._progress_done,
                self._progress_total,
                f"{self._progress_done}/{self._progress_total}",
            )
            self._set_row_status_from_thread(result.path, result.status)

        # ------------------------------------------------------------------ #
        # Worker: capture print() output and forward to log                  #
        # ------------------------------------------------------------------ #

        def _run_with_log_capture(self, fn, *args, **kwargs) -> None:
            """Run *fn* in the current thread, forwarding all stdout to the log."""
            buf = io.StringIO()
            with redirect_stdout(buf):
                fn(*args, **kwargs)
            output = buf.getvalue()
            for line in output.splitlines():
                if line.strip():
                    self._log_from_thread(line)

        # ------------------------------------------------------------------ #
        # Actions                                                              #
        # ------------------------------------------------------------------ #

        @work(thread=True)
        def action_update_all(self) -> None:
            from gitup.config import get_bookmarks
            from gitup.update import update_bookmarks

            if self._busy:
                self._log_from_thread("[yellow]Already running — please wait.[/yellow]")
                return

            self._busy = True
            bookmarks = get_bookmarks(self._config_path)
            if not bookmarks:
                self._log_from_thread("[yellow]No bookmarks configured.[/yellow]")
                self._busy = False
                return

            self._progress_done = 0
            self._progress_total = len(bookmarks)
            self._set_progress_from_thread(0, len(bookmarks), f"0/{len(bookmarks)}")
            self._log_from_thread(
                f"[bold]Updating {len(bookmarks)} bookmark(s)…[/bold]"
            )

            args = self._make_args()
            self._run_with_log_capture(update_bookmarks, bookmarks, args)

            self._set_progress_from_thread(
                self._progress_total, self._progress_total, "Done"
            )
            self._log_from_thread("[green bold]Update complete.[/green bold]")
            self._busy = False

        @work(thread=True)
        def action_show_status(self) -> None:
            from gitup.config import get_bookmarks
            from gitup.update import status_bookmarks

            if self._busy:
                self._log_from_thread("[yellow]Already running — please wait.[/yellow]")
                return

            self._busy = True
            bookmarks = get_bookmarks(self._config_path)
            self._log_from_thread("[bold]Checking local status…[/bold]")
            args = self._make_args()
            self._run_with_log_capture(status_bookmarks, bookmarks, args)
            self._busy = False

        def action_add_bookmark(self) -> None:
            def _handle_result(path: str | None) -> None:
                if not path:
                    return
                from gitup.config import add_bookmarks
                add_bookmarks([path], self._config_path, quiet=True)
                self._log(f"[green]Added bookmark:[/green] {path}")
                self._load_bookmarks()

            self.push_screen(_AddBookmarkScreen(), _handle_result)

        def action_delete_selected(self) -> None:
            table = self.query_one("#bookmarks-table", DataTable)
            if table.cursor_row < 0:
                return
            row_key = table.get_row_at(table.cursor_row)
            if not row_key:
                return
            path = str(row_key[0])  # first column = path
            from gitup.config import delete_bookmarks
            delete_bookmarks([path], self._config_path, quiet=True)
            self._log(f"[yellow]Removed bookmark:[/yellow] {path}")
            self._load_bookmarks()

        def action_refresh(self) -> None:
            self._load_bookmarks()
            self._log("Bookmark list refreshed.")

        def action_list_bookmarks(self) -> None:
            from gitup.config import get_bookmarks
            bookmarks = get_bookmarks(self._config_path)
            if bookmarks:
                self._log(f"[bold]Bookmarks ({len(bookmarks)}):[/bold]")
                for bm in bookmarks:
                    self._log(f"  {bm}")
            else:
                self._log("[yellow]No bookmarks configured.[/yellow]")


def _default_tui_args() -> Namespace:
    """Return a minimal Namespace suitable for use with update functions inside the TUI."""
    return Namespace(
        quiet=False,
        verbose=False,
        current_only=False,
        fetch_only=False,
        prune=False,
        max_depth=3,
        parallel=1,
        status=False,
        json=False,
        _results=[],
        _on_repo_done=None,
    )
