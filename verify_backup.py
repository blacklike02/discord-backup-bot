"""Read-only checks for a local Discord Backup Bot data directory."""

import argparse
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path

from attachments import attachment_path


def verify(data_dir, backup_id=None):
    """Return summary lines and problems without changing the database or files."""
    data_dir = Path(data_dir).resolve()
    db_path = data_dir / 'backups.sqlite3'
    if not db_path.is_file():
        raise FileNotFoundError(f'База не найдена: {db_path}')
    problems, lines = [], []
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        integrity = db.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            problems.append('Ошибка целостности SQLite: ' + '; '.join(row[0] for row in integrity))
        rows = db.execute('SELECT id, status FROM backups WHERE (? IS NULL OR id=?) ORDER BY id', (backup_id, backup_id)).fetchall()
        if not rows:
            problems.append('Снимки не найдены.')
        for ident, status in rows:
            if status != 'complete':
                problems.append(f'Снимок #{ident}: статус {status}.')
            objects = db.execute('SELECT kind, data FROM objects WHERE backup=?', (ident,)).fetchall()
            defaults = 0
            for kind, raw in objects:
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError('object is not a JSON object')
                    if kind == 'role' and value.get('default'):
                        defaults += 1
                except (ValueError, TypeError):
                    problems.append(f'Снимок #{ident}: некорректные данные объекта {kind}.')
            if defaults != 1:
                problems.append(f'Снимок #{ident}: требуется ровно одна роль @everyone, найдено {defaults}.')
            count = files = 0
            for channel, message_id, raw in db.execute('SELECT channel, id, data FROM messages WHERE backup=?', (ident,)):
                count += 1
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict) or not isinstance(message.get('attachments', []), list):
                        raise ValueError('invalid message')
                    for key in ('author', 'created', 'content'):
                        if not isinstance(message.get(key), str):
                            raise ValueError('missing message field')
                    for item in message.get('attachments', []):
                        files += 1
                        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                            raise ValueError('invalid attachment metadata')
                        path = attachment_path(data_dir / 'attachments' / str(ident), item.get('path'))
                        if not path.is_file():
                            problems.append(f'Снимок #{ident}, сообщение {message_id}: файл вложения отсутствует.')
                except (ValueError, TypeError, OSError):
                    problems.append(f'Снимок #{ident}, сообщение {message_id}: некорректные данные или путь вложения.')
            lines.append(f'Снимок #{ident}: {status}, объектов {len(objects)}, сообщений {count}, записей вложений {files}.')
    return lines, problems


def main():
    parser = argparse.ArgumentParser(description='Проверить локальные снимки без изменения данных.')
    base = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    parser.add_argument('--data-dir', type=Path, default=base / 'data')
    parser.add_argument('--backup-id', type=int, help='Проверить только выбранный снимок')
    args = parser.parse_args()
    try:
        lines, problems = verify(args.data_dir, args.backup_id)
    except (OSError, sqlite3.Error) as exc:
        print(f'Не удалось выполнить проверку: {exc}')
        return 2
    for line in lines:
        print(line)
    for problem in problems:
        print('ПРОБЛЕМА:', problem)
    print('Проверка не подтверждает содержимое файлов без сохранённых контрольных сумм и не проверяет восстановление в Discord.')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
