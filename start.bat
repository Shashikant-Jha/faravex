@echo off
rem Set up faravex if needed, then open the browser UI. Options: see scripts\setup.ps1 (e.g. start.bat -Mode check).
rem -ExecutionPolicy Bypass applies to this one process only; no admin rights needed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
