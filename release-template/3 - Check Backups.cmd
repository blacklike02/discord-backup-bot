@echo off
cd /d "%~dp0"
chcp 65001 >nul
DiscordBackupBot.exe --verify
echo.
echo Check finished. Press any key to close.
pause >nul
