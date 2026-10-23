"""Entry point: macOS menu bar app or Windows tray app, depending on the platform."""

import sys


def main() -> None:
    if sys.platform == "win32":
        from claude_switcher.app_windows import main as run
    else:
        from claude_switcher.app import main as run
    run()


if __name__ == "__main__":
    main()
