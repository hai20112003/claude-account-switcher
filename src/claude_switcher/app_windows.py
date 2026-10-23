"""Windows system tray application using pystray."""

import ctypes
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from claude_switcher.controller import PROVIDER_LABELS, PROVIDERS, SwitcherController

APP_NAME = "Claude Switcher"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeSwitcher"
MUTEX_NAME = "Local\\ClaudeSwitcherTray"
USAGE_REFRESH_SECONDS = 300
ICON_BACKGROUND = (37, 99, 235, 255)

_MB_OK = 0x0
_MB_ICONWARNING = 0x30
_MB_TOPMOST = 0x40000
_ERROR_ALREADY_EXISTS = 183


def _resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    candidate = base / "resources" / name
    return candidate if candidate.exists() else Path(__file__).parent / "resources" / name


def build_icon_image(size: int = 64) -> Image.Image:
    """Draw the macOS template glyph in white on a rounded tile so it shows on any taskbar."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=size // 5, fill=ICON_BACKGROUND
    )
    glyph = Image.open(_resource("icon.png")).convert("RGBA")
    inner = int(size * 0.7)
    alpha = glyph.getchannel("A").resize((inner, inner), Image.LANCZOS)
    white = Image.new("RGBA", (inner, inner), (255, 255, 255, 255))
    white.putalpha(alpha)
    offset = (size - inner) // 2
    tile.alpha_composite(white, (offset, offset))
    return tile


def show_alert(title: str, message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} - {title}", _MB_OK | _MB_ICONWARNING | _MB_TOPMOST)


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" -m claude_switcher'


def start_with_windows_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_start_with_windows(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


class TrayApp:
    def __init__(self):
        self.icon = pystray.Icon(RUN_VALUE, build_icon_image(), APP_NAME)
        self.controller = SwitcherController(
            notify=self._notify,
            alert=show_alert,
            on_change=self._refresh_menu,
        )
        self._stop = threading.Event()
        self.icon.menu = self._build_menu()

    def run(self) -> None:
        self.icon.run(setup=self._setup)

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        self.controller.first_launch()
        self._refresh_menu()
        self.controller.refresh_usage()
        threading.Thread(target=self._periodic_refresh, daemon=True).start()

    def _periodic_refresh(self) -> None:
        while not self._stop.wait(USAGE_REFRESH_SECONDS):
            self.controller.refresh_usage()

    def _notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, f"{APP_NAME}: {title}")
        except Exception:
            pass

    def _refresh_menu(self) -> None:
        self.icon.menu = self._build_menu()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _build_menu(self) -> pystray.Menu:
        items: list[pystray.MenuItem] = []
        sections = [(p, self.controller.accounts(p)) for p in PROVIDERS]
        for provider, accounts in sections:
            if not accounts:
                continue
            if items:
                items.append(pystray.Menu.SEPARATOR)
            items.extend(self._provider_section(provider, accounts))

        if items:
            items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Auto-switch", pystray.Menu(*[
            pystray.MenuItem(
                PROVIDER_LABELS[provider],
                self._toggle_auto_switch(provider),
                checked=lambda _item, p=provider: self.controller.auto_switch_enabled(p),
            )
            for provider in PROVIDERS
        ])))
        items.append(pystray.MenuItem("Add Claude account...", lambda: self.controller.add_account("claude")))
        items.append(pystray.MenuItem("Add Codex account...", lambda: self.controller.add_account("codex")))
        items.append(pystray.MenuItem("Refresh usage", lambda: self.controller.refresh_usage(), default=True))

        removable = [a for _, accounts in sections for a in accounts]
        if removable:
            items.append(pystray.MenuItem("Remove account", pystray.Menu(*[
                pystray.MenuItem(
                    f"[{'Claude' if a.provider == 'claude' else 'Codex'}] {a.email}",
                    self._remove(a.provider, a.email),
                    enabled=not a.active,
                )
                for a in removable
            ])))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(
            "Start with Windows",
            self._toggle_start_with_windows,
            checked=lambda _item: start_with_windows_enabled(),
        ))
        items.append(pystray.MenuItem("Quit", self._quit))
        return pystray.Menu(*items)

    def _provider_section(self, provider: str, accounts) -> list[pystray.MenuItem]:
        busy = self.controller.is_busy(provider)
        title = f"{PROVIDER_LABELS[provider]}{' (working...)' if busy else ''}"
        items = [pystray.MenuItem(title, None, enabled=False)]
        for account in accounts:
            has_creds = self.controller.has_credentials(account)
            label = (
                f"{account.email} ({account.subscription_type})"
                if has_creds
                else f"{account.email} (unavailable)"
            )
            items.append(pystray.MenuItem(
                label,
                self._switch(provider, account.email),
                checked=lambda _item, active=account.active: active,
                radio=True,
                enabled=has_creds and not busy,
            ))
            if has_creds:
                items.append(pystray.MenuItem(f"      {self.controller.usage_text(account)}", None, enabled=False))
        return items

    def _switch(self, provider: str, email: str):
        return lambda: self.controller.switch(provider, email)

    def _remove(self, provider: str, email: str):
        return lambda: self.controller.remove_account(provider, email)

    def _toggle_auto_switch(self, provider: str):
        return lambda: self.controller.toggle_auto_switch(provider)

    def _toggle_start_with_windows(self) -> None:
        try:
            set_start_with_windows(not start_with_windows_enabled())
        except OSError as exc:
            show_alert("Start with Windows", str(exc))
        self._refresh_menu()

    def _quit(self) -> None:
        self._stop.set()
        self.icon.stop()


def _acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        return None
    return handle


def main() -> None:
    mutex = _acquire_single_instance()
    if mutex is None:
        show_alert("Already running", "Claude Switcher is already running in the system tray.")
        return
    TrayApp().run()


if __name__ == "__main__":
    main()
