#!/bin/bash
cd "$(dirname "$0")" || exit 1
./DiscordBackupBot --verify
printf '\nПроверка завершена. Нажмите Enter, чтобы закрыть окно.\n'
read -r
