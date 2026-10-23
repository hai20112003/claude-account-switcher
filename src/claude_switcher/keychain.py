"""Credential storage: macOS Keychain via the `security` CLI, or the Windows store.

On Windows the active Claude Code session lives in ~/.claude/.credentials.json and
saved account backups are DPAPI-encrypted files (see winstore.py). The public
functions keep the same service-name API on both platforms.
"""

import re
import subprocess
import sys

from claude_switcher import winstore

CLAUDE_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECONDS = 5
BACKEND = "windows" if sys.platform == "win32" else "macos"


def _windows() -> bool:
    return BACKEND == "windows"


def read_credentials(service: str) -> str | None:
    """Read the password blob for a Keychain entry. Returns None if not found."""
    if _windows():
        return winstore.read_active() if service == CLAUDE_SERVICE else winstore.read_saved(service)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() if result.stdout.strip() else None


def write_credentials(service: str, account: str, password: str) -> None:
    """Write a Keychain entry, replacing all existing entries for that service."""
    if _windows():
        if service == CLAUDE_SERVICE:
            winstore.write_active(password)
        else:
            winstore.write_saved(service, account, password)
        return
    # Delete all existing entries for this service (there may be duplicates)
    while True:
        try:
            r = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Keychain delete timed out. Check macOS Keychain access permissions.") from exc
        if r.returncode != 0:
            break

    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", service,
                "-a", account,
                "-w", password,
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Keychain write timed out. Check macOS Keychain access permissions.") from exc
    if result.returncode != 0:
        raise RuntimeError("Keychain write failed. Check macOS Keychain access permissions.")


def delete_credentials(service: str) -> bool:
    """Delete a Keychain entry. Returns True if deleted, False if not found."""
    if _windows():
        return winstore.delete_active() if service == CLAUDE_SERVICE else winstore.delete_saved(service)
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def read_account_attribute(service: str) -> str | None:
    """Read the account (-a) attribute of a Keychain entry by parsing security output."""
    if _windows():
        return winstore.active_account() if service == CLAUDE_SERVICE else winstore.saved_account(service)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', result.stdout)
    return match.group(1) if match else None
