@echo off
cd /d "%~dp0"
chcp 65001 >nul
DiscordBackupBot.exe
echo.
echo Bot stopped. Press any key to close.
pause >nul
