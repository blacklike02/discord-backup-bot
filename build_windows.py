"""Build an isolated Windows x64 distribution from explicitly selected files."""
import os
import platform
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build this distribution on Windows x64 with Python x64.')
    root = Path(__file__).resolve().parent
    if (root / '.env.example').read_text(encoding='utf-8').strip() != 'DISCORD_TOKEN=вставьте_токен_бота':
        raise SystemExit('Restore the placeholder in .env.example before building the shareable archive.')
    build = root / 'build'
    output = root / 'dist'
    build.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
        '--name', 'DiscordBackupBot', '--distpath', str(build / 'exe'),
        '--workpath', str(build / 'work'), '--specpath', str(build), str(root / 'run_bot.py'),
    ], cwd=root, check=True)
    files = {
        'DiscordBackupBot.exe': build / 'exe' / 'DiscordBackupBot.exe',
        'README.md': root / 'README-WINDOWS.md',
        'LICENSE': root / 'LICENSE',
        '.env': root / '.env.example',
        **{p.name: p for p in sorted((root / 'release-template').glob('*.cmd'))},
    }
    expected = {'1 - Edit Token.cmd', '2 - Start Bot.cmd', '3 - Check Backups.cmd'}
    if set(files) != expected | {'DiscordBackupBot.exe', 'README.md', 'LICENSE', '.env'}:
        raise SystemExit('Unexpected release template files; archive was not created.')
    archive = output / 'Discord-Backup-Bot-Windows-x64.zip'
    temporary = output / 'Discord-Backup-Bot-Windows-x64.tmp'
    with ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=6) as z:
        for name, path in files.items():
            z.write(path, 'Discord-Backup-Bot/' + name)
    with ZipFile(temporary) as z:
        if z.testzip() is not None or len(z.namelist()) != len(files):
            raise RuntimeError('Archive validation failed')
        if z.read('Discord-Backup-Bot/.env') != (root / '.env.example').read_bytes():
            raise RuntimeError('Incorrect token template')
    os.replace(temporary, archive)
    print(f'Windows archive: {archive}')


if __name__ == '__main__':
    main()
