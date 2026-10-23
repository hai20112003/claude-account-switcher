"""Codex CLI usage fetching via ChatGPT backend APIs."""

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from claude_switcher import keychain
from claude_switcher.codex_core import (
    CodexCredentialsExpiredError,
    backup_codex_credentials,
    normalize_codex_credentials_blob,
    refresh_codex_credentials,
    write_active_codex_credentials,
)
from claude_switcher.usage_state import UsageState, UsageWindow

CODEX_USAGE_URLS = (
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/backend-api/api/codex/usage",
)
CODEX_LOGIN_REQUIRED_USAGE = {"error": {"code": "login_required"}}


def _decode_jwt_payload(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        import base64

        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _extract_codex_token(creds_json: str) -> tuple[str, str] | None:
    """Extract access_token and ChatGPT account id from Codex credentials JSON."""
    try:
        creds_json = normalize_codex_credentials_blob(creds_json) or creds_json
        data = json.loads(creds_json)
        tokens = data.get("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
        token = tokens.get("access_token") or data.get("access_token")
        account_id = tokens.get("account_id") or data.get("account_id")
        if not account_id and tokens.get("id_token"):
            payload = _decode_jwt_payload(tokens["id_token"])
            auth_info = payload.get("https://api.openai.com/auth", {}) if payload else {}
            if isinstance(auth_info, dict):
                account_id = auth_info.get("chatgpt_account_id")
        if token and account_id:
            return str(token), str(account_id)
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _fetch_codex_usage_once(creds_json: str) -> dict | None:
    """Fetch Codex usage once from the first available ChatGPT backend endpoint."""
    result = _extract_codex_token(creds_json)
    if not result:
        return None
    token, account_id = result

    for url in CODEX_USAGE_URLS:
        req = Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("ChatGPT-Account-Id", account_id)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "claude-switcher/0.4.3")

        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError:
            continue
        except (URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return None


def fetch_codex_usage_with_refresh(creds_json: str) -> tuple[dict | None, str | None]:
    """Fetch usage, refreshing Codex credentials once if the saved access token is stale."""
    normalized = normalize_codex_credentials_blob(creds_json) or creds_json
    usage = _fetch_codex_usage_once(normalized)
    if usage is not None:
        return usage, None

    try:
        refreshed = refresh_codex_credentials(normalized)
    except CodexCredentialsExpiredError:
        return CODEX_LOGIN_REQUIRED_USAGE, None
    if not refreshed:
        return None, None

    return _fetch_codex_usage_once(refreshed), refreshed


def fetch_codex_usage(creds_json: str) -> dict | None:
    """Fetch Codex usage from the first available ChatGPT backend endpoint."""
    usage, _ = fetch_codex_usage_with_refresh(creds_json)
    return usage


def fetch_codex_usage_for_account(email: str) -> dict | None:
    """Fetch usage for a saved Codex account stored in Keychain."""
    service = f"codex-switcher:{email}"
    creds = keychain.read_credentials(service)
    if not creds:
        return None
    usage, refreshed = fetch_codex_usage_with_refresh(creds)
    if refreshed:
        keychain.write_credentials(service, email, refreshed)
    return usage


def fetch_active_codex_usage() -> dict | None:
    """Fetch usage for the currently active Codex session."""
    try:
        from claude_switcher.codex_core import read_codex_credentials

        creds = read_codex_credentials()
    except RuntimeError:
        return None
    if not creds:
        return None
    usage, refreshed = fetch_codex_usage_with_refresh(creds)
    if refreshed:
        write_active_codex_credentials(refreshed)
        backup_codex_credentials(refreshed)
    return usage


def _format_reset_delta(reset_at: float) -> str:
    """Convert a Unix timestamp to a human-readable countdown."""
    try:
        now = datetime.now(timezone.utc)
        target = datetime.fromtimestamp(float(reset_at), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "?"

    total_seconds = int((target - now).total_seconds())
    if total_seconds <= 0:
        return "now"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def codex_usage_state(usage: dict | None) -> UsageState:
    """Convert Codex usage data into a normalized usage state."""
    if not usage:
        return UsageState(available=False, display="Usage unavailable")

    error = usage.get("error")
    if isinstance(error, dict) and error.get("code") == "login_required":
        return UsageState(available=False, display="Login required")

    rate_limit = usage.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return UsageState(available=False, display="Usage unavailable")

    parts = []
    windows = []
    for label, key in (("1h", "primary_window"), ("7d", "secondary_window")):
        window = rate_limit.get(key)
        if not isinstance(window, dict) or "used_percent" not in window:
            continue
        try:
            percent = float(window["used_percent"])
        except (TypeError, ValueError):
            continue
        reset = _format_reset_delta(window["reset_at"]) if "reset_at" in window else None
        reset_suffix = f" ({reset})" if reset else ""
        parts.append(f"{label} {percent:.0f}%{reset_suffix}")
        windows.append(UsageWindow(label=label, percent=percent, resets_in=reset))

    if not parts:
        return UsageState(available=False, display="Usage unavailable")
    return UsageState(available=True, display=" | ".join(parts), windows=tuple(windows))


def format_codex_usage(usage: dict | None) -> str:
    """Format Codex usage for menu display."""
    return codex_usage_state(usage).display
