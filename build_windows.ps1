# Build Claude Switcher as a standalone Windows .exe (no Python needed on the target machine).
#
# Usage, from the repo root:
#   uv venv; uv pip install -e ".[dev]"
#   .\build_windows.ps1
#
# Output: dist\ClaudeSwitcher.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

New-Item -ItemType Directory -Force build | Out-Null
& $python -c "import sys; sys.path.insert(0, 'src'); from claude_switcher.app_windows import build_icon_image; build_icon_image(256).save('build/icon.ico', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])"
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed" }

& $python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --specpath build `
    --name ClaudeSwitcher `
    --icon "$PSScriptRoot\build\icon.ico" `
    --paths "$PSScriptRoot\src" `
    --add-data "$PSScriptRoot\src\claude_switcher\resources;resources" `
    --hidden-import pystray._win32 `
    --exclude-module rumps `
    --exclude-module pytest `
    src\claude_switcher\__main__.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Host ""
Write-Host "Done! App is at: dist\ClaudeSwitcher.exe"
