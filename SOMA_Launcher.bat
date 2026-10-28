@echo off
setlocal
cd /d "%~dp0"

if exist ".venv-clean\Scripts\pythonw.exe" (
  ".venv-clean\Scripts\pythonw.exe" "tools\launcher\soma_launcher.py"
  exit /b %errorlevel%
)

if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\pythonw.exe" "tools\launcher\soma_launcher.py"
  exit /b %errorlevel%
)

echo SOMA Python environment was not found.
echo Expected .venv-clean or .venv under %~dp0
pause
exit /b 1
