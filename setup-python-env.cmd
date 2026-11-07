@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-python-env.ps1" %*
exit /b %errorlevel%
