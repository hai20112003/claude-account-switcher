"""
py2app build script.

Usage:
    python setup.py py2app

This creates a standalone macOS .app bundle in the dist/ folder.
The .app is fully standalone — no Python install needed on the target machine.
"""

from setuptools import setup

# py2app conflicts with pyproject.toml's install_requires,
# so we keep this file minimal and self-contained.
APP = ["src/claude_switcher/app.py"]

OPTIONS = {
    "argv_emulation": False,
    # LSUIElement=True = menu bar app only (no Dock icon, no Cmd+Tab entry)
    "plist": {
        "CFBundleName": "Claude Switcher",
        "CFBundleDisplayName": "Claude Switcher",
        "CFBundleIdentifier": "com.hai20112003.claude-switcher",
        "CFBundleVersion": "0.4.3",
        "CFBundleShortVersionString": "0.4.3",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "12.0",
    },
    # Include our package + rumps and its dependencies
    "packages": ["claude_switcher", "rumps"],
    "includes": ["objc", "Foundation", "AppKit"],
    "excludes": ["pytest", "_pytest", "pygments", "iniconfig", "pluggy", "setuptools.tests"],
    "resources": ["src/claude_switcher/resources"],
}

setup(
    app=APP,
    name="Claude Switcher",
    options={"py2app": OPTIONS},
)
