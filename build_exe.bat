@echo off
REM Build BACnet-MSTP-Explorer.exe (standalone, single file, no console).
REM Requires: py -3.14 with pyserial and pyinstaller installed.
REM   py -3.14 -m pip install pyserial pyinstaller
cd /d "%~dp0"
py -3.14 -m PyInstaller --onefile --windowed --name "BACnet-MSTP-Explorer" --clean explorer.py
echo.
echo Done. The executable is in the "dist" folder: dist\BACnet-MSTP-Explorer.exe
pause
