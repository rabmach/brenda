@echo off
rem brenda installer launcher - double-click me or run from cmd
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
