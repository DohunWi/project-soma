param(
    [string]$Python = ".venv-clean\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PythonPath = (Resolve-Path (Join-Path $Root $Python)).Path

& $PythonPath -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name SOMA_Launcher `
    --distpath (Join-Path $Root "dist") `
    --workpath (Join-Path $Root "build\soma-launcher") `
    --specpath (Join-Path $Root "build") `
    (Join-Path $PSScriptRoot "soma_launcher.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Host "Built: $(Join-Path $Root 'dist\SOMA_Launcher.exe')"
