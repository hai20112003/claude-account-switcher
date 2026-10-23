#!/bin/bash
# Build Claude Switcher as a standalone macOS .app
#
# py2app chokes if pyproject.toml has dependencies (it treats them as
# install_requires, which it no longer supports). Workaround: temporarily
# hide pyproject.toml during the build.

set -e

cd "$(dirname "$0")"

# Activate venv if present
if [ -d .venv ]; then
    source .venv/bin/activate
fi

echo "Building Claude Switcher.app..."

# Hide pyproject.toml so py2app doesn't read dependencies from it
mv pyproject.toml pyproject.toml.bak
trap 'mv pyproject.toml.bak pyproject.toml' EXIT

# Clean previous builds
rm -rf build dist

# Build
python3 setup.py py2app

echo ""
echo "Done! App is at: dist/Claude Switcher.app"
echo "You can copy it to /Applications:"
echo "  cp -r 'dist/Claude Switcher.app' /Applications/"
