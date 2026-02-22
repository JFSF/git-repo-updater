# -*- coding: utf-8  -*-
#
# Copyright (C) 2011-2025 Ben Kurtovic <ben.kurtovic@gmail.com>
# Released under the terms of the MIT License. See LICENSE for details.

"""Cross-platform desktop notifications.

Requires no additional dependencies — uses platform-native tools:

* **Linux**: ``notify-send`` (libnotify)
* **macOS**: ``osascript`` (built-in)
* **Windows**: PowerShell's ``System.Windows.Forms.NotifyIcon``

If the platform tool is unavailable or fails the notification is silently
skipped and the error is recorded at DEBUG level.
"""

import logging
import platform
import shlex
import subprocess

__all__ = ["send_notification", "is_notify_available"]

logger = logging.getLogger(__name__)

_APP_NAME = "gitup"


def send_notification(title: str, message: str) -> None:
    """Send a native desktop notification.

    Silently no-ops if the platform tool is unavailable.
    """
    system = platform.system()
    try:
        if system == "Linux":
            _notify_linux(title, message)
        elif system == "Darwin":
            _notify_macos(title, message)
        elif system == "Windows":
            _notify_windows(title, message)
        else:
            logger.debug("Notifications not supported on platform: %s", system)
    except FileNotFoundError as exc:
        logger.debug("Notification tool not found: %s", exc)
    except subprocess.CalledProcessError as exc:
        logger.debug("Notification failed (exit %s): %s", exc.returncode, exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Unexpected notification error: %s", exc)


def is_notify_available() -> bool:
    """Return ``True`` if a notification tool is likely available on this system."""
    system = platform.system()
    if system == "Linux":
        return _command_exists("notify-send")
    if system == "Darwin":
        return _command_exists("osascript")
    if system == "Windows":
        return True  # PowerShell is always present
    return False


# ---------------------------------------------------------------------------
# Platform implementations
# ---------------------------------------------------------------------------


def _command_exists(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def _notify_linux(title: str, message: str) -> None:
    subprocess.run(
        ["notify-send", f"--app-name={_APP_NAME}", "--urgency=normal", title, message],
        check=True,
        capture_output=True,
    )


def _notify_macos(title: str, message: str) -> None:
    # Escape for AppleScript string context
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" with title "{safe_title}" '
        f'subtitle "{_APP_NAME}"'
    )
    subprocess.run(
        ["osascript", "-e", script],
        check=True,
        capture_output=True,
    )


def _notify_windows(title: str, message: str) -> None:
    # Use PowerShell to show a balloon notification via NotifyIcon
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(5000, '{safe_title}', '{safe_msg}', 'Info'); "
        "Start-Sleep -Milliseconds 5500; "
        "$n.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        check=True,
        capture_output=True,
    )


def build_summary_message(results: list) -> str:  # list[RepoResult]
    """Build a concise one-line summary suitable for a notification body."""
    total = len(results)
    updated = sum(1 for r in results if r.status == "updated")
    errors = sum(1 for r in results if r.status == "error")

    parts = [f"{total} repo{'s' if total != 1 else ''}"]
    if updated:
        parts.append(f"{updated} updated")
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if not updated and not errors:
        parts.append("all up to date")
    return " · ".join(parts)
