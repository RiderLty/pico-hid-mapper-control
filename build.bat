@echo off
setlocal
cd /d "%~dp0"

REM Use a dedicated build environment, independent of the system packages.
if not exist ".venv-build\Scripts\python.exe" (
    py -3 -m venv .venv-build
    if errorlevel 1 goto :failed
)

".venv-build\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-build.txt
if errorlevel 1 goto :failed

".venv-build\Scripts\python.exe" -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name pico-hid-mapper-control ^
    --paths "%~dp0." ^
    --hidden-import pygame._sdl2.controller ^
    --hidden-import serial.tools.list_ports ^
    --icon "%~dp0icon.png" ^
    --add-data "%~dp0icon.png;." ^
    --distpath "%~dp0dist" ^
    --workpath "%~dp0build" ^
    --specpath "%~dp0build" ^
    "%~dp0start.pyw"
if errorlevel 1 goto :failed

echo.
echo OK: %~dp0dist\pico-hid-mapper-control.exe
exit /b 0

:failed
echo.
echo Build failed. See the output above.
pause
exit /b 1
