@echo off
REM Double-click this to set up Disk-Rip (installs prerequisites + writes config).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
