#!/bin/bash
cd "$(dirname "$0")" || exit 1
./DiscordBackupBot
printf '\nБот остановлен. Нажмите Enter, чтобы закрыть окно.\n'
read -r
