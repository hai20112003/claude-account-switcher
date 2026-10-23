"""UI-independent switcher behaviour, used by the Windows tray app.

The controller owns the usage cache, background work and auto-switch decisions. The
UI layer passes three callbacks: ``notify`` for a passing message, ``alert`` for an
error the user must see, and ``on_change`` to redraw its menu. Callbacks may be
called from worker threads.
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

from claude_switcher import keychain
from claude_switcher.auto_switch import account_key, choose_auto_switch_target, should_auto_switch
from claude_switcher.codex_core import (
    add_new_codex_account,
    check_codex_cli,
    import_current_codex_account,
    remove_codex_account,
    switch_codex_account,
)
from claude_switcher.codex_usage import (
    codex_usage_state,
    fetch_active_codex_usage,
    fetch_codex_usage_for_account,
)
from claude_switcher.config import (
    DEFAULT_CONFIG_PATH,
    AccountInfo,
    get_active_account,
    load_accounts,
    load_settings,
    set_auto_switch_enabled,
)
from claude_switcher.core import (
    add_new_account,
    check_claude_cli,
    import_current_account,
    remove_saved_account,
    switch_account,
)
from claude_switcher.usage import claude_usage_state, fetch_active_usage, fetch_usage_for_account
from claude_switcher.usage_state import UsageState

PROVIDERS = ("claude", "codex")
PROVIDER_LABELS = {"claude": "Claude Code", "codex": "Codex CLI"}
AUTO_SWITCH_COOLDOWN_SECONDS = 60
USAGE_PLACEHOLDER = "•••"

Notify = Callable[[str, str], None]
Alert = Callable[[str, str], None]


class SwitcherController:
    def __init__(
        self,
        notify: Notify,
        alert: Alert,
        on_change: Callable[[], None],
        config_path: Path = DEFAULT_CONFIG_PATH,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
    ):
        self.config_path = config_path
        self._notify = notify
        self._alert = alert
        self._on_change = on_change
        self._background = run_in_background or _start_thread
        self._lock = threading.Lock()
        self._usage_cache: dict[tuple[str, str], UsageState] = {}
        self._last_auto_switch_attempt: dict[str, float] = {}
        self._refresh_in_progress = False
        self._busy: set[str] = set()

    # --- state for the menu --------------------------------------------------

    def accounts(self, provider: str | None = None) -> list[AccountInfo]:
        accounts = load_accounts(self.config_path)
        return [a for a in accounts if provider is None or a.provider == provider]

    def has_credentials(self, account: AccountInfo) -> bool:
        prefix = "claude-switcher:" if account.provider == "claude" else "codex-switcher:"
        return keychain.read_credentials(f"{prefix}{account.email}") is not None

    def usage_text(self, account: AccountInfo) -> str:
        state = self._usage_cache.get(account_key(account))
        return state.display if state else USAGE_PLACEHOLDER

    def auto_switch_enabled(self, provider: str) -> bool:
        return bool(load_settings(self.config_path).auto_switch.get(provider, False))

    def is_busy(self, provider: str) -> bool:
        return provider in self._busy

    # --- first launch ----------------------------------------------------------

    def first_launch(self) -> None:
        """Import the accounts already logged in to Claude Code and Codex CLI."""
        if self.config_path.exists():
            return

        claude_available = check_claude_cli()
        codex_available = check_codex_cli()

        if claude_available:
            try:
                imported = import_current_account(self.config_path)
            except Exception as exc:
                imported = None
                self._notify("Claude import skipped", str(exc))
            if imported:
                self._notify("Claude account imported", f"{imported.email} ({imported.subscription_type})")

        if codex_available:
            try:
                imported = import_current_codex_account(self.config_path)
            except Exception as exc:
                imported = None
                self._notify("Codex import skipped", str(exc))
            if imported:
                self._notify("Codex account imported", f"{imported.email} ({imported.subscription_type})")

        if not claude_available and not codex_available:
            self._alert(
                "CLI not found",
                "Please install Claude Code or Codex CLI before using Claude Switcher.",
            )

    # --- actions ---------------------------------------------------------------

    def switch(self, provider: str, email: str) -> None:
        active = get_active_account(self.config_path, provider=provider)
        if active and active.email == email:
            return
        if not self._claim(provider):
            self._notify(
                f"{PROVIDER_LABELS[provider]} is busy",
                "Wait for the current switch or login to finish.",
            )
            return

        def work():
            try:
                _switch(provider, email, self.config_path)
            except Exception as exc:
                self._alert("Switch failed", str(exc))
            else:
                self._notify(f"{PROVIDER_LABELS[provider]} account switched", email)
            finally:
                self._release(provider)
                self._on_change()
            self.refresh_usage()

        self._background(work)

    def add_account(self, provider: str) -> None:
        cli_ok = check_claude_cli() if provider == "claude" else check_codex_cli()
        if not cli_ok:
            name = "Claude Code" if provider == "claude" else "Codex CLI"
            self._alert(f"{name} not found", f"Please install {name} before adding an account.")
            return
        if not self._claim(provider):
            self._notify(f"{PROVIDER_LABELS[provider]} is busy", "Wait for the current action to finish.")
            return

        def work():
            try:
                result = (
                    add_new_account(self.config_path)
                    if provider == "claude"
                    else add_new_codex_account(self.config_path)
                )
            except Exception as exc:
                self._alert("Add account failed", str(exc))
            else:
                if result:
                    self._notify(
                        f"{PROVIDER_LABELS[provider]} account added",
                        f"{result.email} ({result.subscription_type})",
                    )
                else:
                    self._notify("Cancelled", "Login was cancelled or failed.")
            finally:
                self._release(provider)
                self._on_change()
            self.refresh_usage()

        self._background(work)

    def remove_account(self, provider: str, email: str) -> None:
        active = get_active_account(self.config_path, provider=provider)
        if active and active.email == email:
            self._alert(
                "Cannot remove",
                f"You cannot remove the active {PROVIDER_LABELS[provider]} account. Switch first.",
            )
            return
        if provider == "claude":
            remove_saved_account(email, self.config_path)
        else:
            remove_codex_account(email, self.config_path)
        self._usage_cache.pop((provider, email), None)
        self._notify(f"{PROVIDER_LABELS[provider]} account removed", email)
        self._on_change()

    def toggle_auto_switch(self, provider: str) -> None:
        enabled = not self.auto_switch_enabled(provider)
        set_auto_switch_enabled(provider, enabled, self.config_path)
        self._notify(f"Auto-switch {PROVIDER_LABELS[provider]}", "Enabled" if enabled else "Disabled")
        self._on_change()

    def refresh_usage(self) -> None:
        with self._lock:
            if self._refresh_in_progress:
                return
            self._refresh_in_progress = True
        self._background(self._refresh_usage_now)

    # --- internals ---------------------------------------------------------------

    def _claim(self, provider: str) -> bool:
        with self._lock:
            if provider in self._busy:
                return False
            self._busy.add(provider)
            return True

    def _release(self, provider: str) -> None:
        with self._lock:
            self._busy.discard(provider)

    def _refresh_usage_now(self) -> None:
        switched = False
        try:
            accounts = load_accounts(self.config_path)
            active = {p: get_active_account(self.config_path, provider=p) for p in PROVIDERS}
            for account in accounts:
                self._usage_cache[account_key(account)] = self._fetch_usage_state(
                    account, active.get(account.provider)
                )
            self._on_change()
            for provider in PROVIDERS:
                result = self._attempt_auto_switch(provider)
                if result:
                    self._notify_auto_switch(result)
                    switched = switched or result["status"] == "switched"
        finally:
            with self._lock:
                self._refresh_in_progress = False
        if switched:
            self._on_change()
            self.refresh_usage()

    def _fetch_usage_state(self, account: AccountInfo, active: AccountInfo | None) -> UsageState:
        is_active = active is not None and active.email == account.email
        try:
            if account.provider == "claude":
                usage = fetch_active_usage() if is_active else fetch_usage_for_account(account.email)
                return claude_usage_state(usage)
            if account.provider == "codex":
                usage = (
                    fetch_active_codex_usage() if is_active else fetch_codex_usage_for_account(account.email)
                )
                return codex_usage_state(usage)
        except Exception:
            pass
        return UsageState(available=False, display="Usage unavailable")

    def _attempt_auto_switch(self, provider: str) -> dict | None:
        settings = load_settings(self.config_path)
        active = get_active_account(self.config_path, provider=provider)
        if not active:
            return None

        active_state = self._usage_cache.get(account_key(active))
        if not active_state or not should_auto_switch(
            active_state,
            settings.auto_switch.get(provider, False),
            settings.auto_switch_threshold,
        ):
            return None

        now = time.time()
        if now - self._last_auto_switch_attempt.get(provider, 0) < AUTO_SWITCH_COOLDOWN_SECONDS:
            return None
        self._last_auto_switch_attempt[provider] = now

        target = choose_auto_switch_target(
            provider=provider,
            accounts=load_accounts(self.config_path),
            active_email=active.email,
            usage_by_account=self._usage_cache,
            has_credentials=self.has_credentials,
            threshold=settings.auto_switch_threshold,
        )
        if not target:
            return {"status": "no_target", "provider": provider, "email": active.email}
        if not self._claim(provider):
            return None
        try:
            _switch(provider, target.email, self.config_path)
        except Exception as exc:
            return {"status": "error", "provider": provider, "email": active.email, "message": str(exc)}
        finally:
            self._release(provider)
        return {"status": "switched", "provider": provider, "email": target.email}

    def _notify_auto_switch(self, result: dict) -> None:
        label = PROVIDER_LABELS[result["provider"]]
        if result["status"] == "switched":
            self._notify(f"Auto-switched {label}", result["email"])
        elif result["status"] == "no_target":
            self._notify(f"{label} limit reached", "No available account to switch to.")
        elif result["status"] == "error":
            self._notify(f"{label} auto-switch failed", result.get("message", "Unknown error"))


def _switch(provider: str, email: str, config_path: Path) -> None:
    if provider == "claude":
        switch_account(email, config_path)
    else:
        switch_codex_account(email, config_path)


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()
