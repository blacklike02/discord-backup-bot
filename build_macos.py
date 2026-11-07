"""Build and smoke-test a self-contained macOS distribution on a Mac."""
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Build this distribution on macOS.')
    machine = platform.machine().lower()
    architectures = {'arm64': 'Apple-Silicon', 'x86_64': 'Intel'}
    if machine not in architectures:
        raise SystemExit(f'Unsupported macOS architecture: {machine}')
    root = Path(__file__).resolve().parent
    template = root / '.env.example'
    if template.read_text(encoding='utf-8').strip() != 'DISCORD_TOKEN=вставьте_токен_бота':
        raise SystemExit('Restore the placeholder in .env.example before building.')
    build, dist = root / 'build', root / 'dist'
    build.mkdir(exist_ok=True)
    dist.mkdir(exist_ok=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
        '--name', 'DiscordBackupBot', '--distpath', str(build / 'macos-exe'),
        '--workpath', str(build / 'macos-work'), '--specpath', str(build), str(root / 'run_bot.py'),
    ], cwd=root, check=True)
    executable = build / 'macos-exe' / 'DiscordBackupBot'
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(executable)], check=True)
    actual_arch = subprocess.check_output(['/usr/bin/lipo', '-archs', str(executable)], text=True).split()
    if machine not in actual_arch:
        raise RuntimeError(f'Binary architecture mismatch: {actual_arch}')
    files = {
        'DiscordBackupBot': executable,
        'README.md': root / 'README-MACOS.md',
        'LICENSE': root / 'LICENSE',
        '.env': template,
        **{p.name: p for p in sorted((root / 'macos-template').glob('*.command'))},
    }
    commands = {'1 - Edit Token.command', '2 - Start Bot.command', '3 - Check Backups.command'}
    if set(files) != commands | {'DiscordBackupBot', 'README.md', 'LICENSE', '.env'}:
        raise SystemExit('Unexpected release template files; archive was not created.')
    archive = dist / f'Discord-Backup-Bot-macOS-{architectures[machine]}.zip'
    temporary = archive.with_suffix('.tmp')
    with ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=6) as z:
        for name, path in files.items():
            info = ZipInfo('Discord-Backup-Bot/' + name)
            info.create_system = 3
            info.external_attr = (0o100755 if name in commands or name == 'DiscordBackupBot' else 0o100644) << 16
            info.compress_type = ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    with ZipFile(temporary) as z:
        if z.testzip() is not None or set(z.namelist()) != {'Discord-Backup-Bot/' + n for n in files}:
            raise RuntimeError('Archive validation failed')
        if z.read('Discord-Backup-Bot/.env') != template.read_bytes():
            raise RuntimeError('Incorrect token template')
    # Use macOS's extractor to verify that ZIP executable permissions survive.
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        subprocess.run(['/usr/bin/ditto', '-x', '-k', str(temporary), str(tmpdir)], check=True)
        app = tmpdir / 'Discord-Backup-Bot'
        binary = app / 'DiscordBackupBot'
        for name in commands | {'DiscordBackupBot'}:
            if not os.access(app / name, os.X_OK):
                raise RuntimeError(f'Executable permission lost: {name}')
        env = os.environ.copy()
        for key in ('DISCORD_TOKEN', 'BOT_DATA_DIR', 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(key, None)
        result = subprocess.run([str(binary)], cwd=tmpdir, env=env, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 or 'DISCORD_TOKEN' not in result.stderr or 'Traceback' in result.stderr:
            raise RuntimeError('Packaged no-token startup check failed: ' + result.stderr[-1000:])
        db = app / 'data' / 'backups.sqlite3'
        if not db.is_file() or (tmpdir / 'data').exists():
            raise RuntimeError('Packaged application used an incorrect data directory')
        connection = sqlite3.connect(db)
        try:
            connection.execute("INSERT INTO backups(id,owner,guild,name,created,status,details) VALUES (1,'1','2','test','today','complete','[]')")
            connection.execute("INSERT INTO objects VALUES (1,'role','1',?)", ('{"default":true}',))
            connection.commit()
        finally:
            connection.close()
        subprocess.run([str(binary), '--verify'], cwd=tmpdir, env=env, check=True, capture_output=True, text=True, timeout=60)
    os.replace(temporary, archive)
    print(f'macOS archive, checked on {machine}: {archive}', flush=True)


if __name__ == '__main__':
    main()
