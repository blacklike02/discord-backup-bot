import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from attachments import attachment_path
from verify_backup import verify


class VerifyBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with closing(sqlite3.connect(self.root / 'backups.sqlite3')) as db, db:
            db.executescript('''
                CREATE TABLE backups (id INTEGER PRIMARY KEY, status TEXT);
                CREATE TABLE objects (backup INTEGER, kind TEXT, data TEXT);
                CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT);
                INSERT INTO backups VALUES (1, 'complete');
                INSERT INTO objects VALUES (1, 'role', '{"default":true}');
            ''')
            message = {'author': 'Alice', 'created': 'today', 'content': 'hi',
                       'attachments': [{'name': 'image.png', 'path': '42.bin'}]}
            db.execute('INSERT INTO messages VALUES (1, ?, ?, ?)', ('2', '3', json.dumps(message)))
        self.attachments = self.root / 'attachments' / '1'
        self.attachments.mkdir(parents=True)
        (self.attachments / '42.bin').write_bytes(b'image')

    def test_valid_snapshot_and_missing_attachment(self):
        lines, problems = verify(self.root)
        self.assertIn('сообщений 1', lines[0])
        self.assertEqual(problems, [])
        (self.attachments / '42.bin').unlink()
        _, problems = verify(self.root)
        self.assertTrue(any('отсутствует' in p for p in problems))

    def test_corrupt_metadata_and_partial_status(self):
        with closing(sqlite3.connect(self.root / 'backups.sqlite3')) as db, db:
            db.execute("UPDATE backups SET status='partial'")
            db.execute("UPDATE messages SET data='not json'")
        _, problems = verify(self.root, 1)
        self.assertTrue(any('partial' in p for p in problems))
        self.assertTrue(any('некорректные данные' in p for p in problems))
        self.assertTrue(verify(self.root, 99)[1])

    def test_paths_cannot_escape_backup(self):
        for name in ('../secret', '..\\secret', '/absolute', 'C:\\secret', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                attachment_path(self.attachments, name)
        self.assertEqual(attachment_path(self.attachments, '42.bin'), (self.attachments / '42.bin').resolve())


if __name__ == '__main__':
    unittest.main()
