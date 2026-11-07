"""Compare SQLite insertion methods without network or project data."""
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import MessageWriter


def run(batch_method, count=30000):
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT, PRIMARY KEY (backup,id))')
    rows = [(1, str(i % 20), str(i), '{"content":"sample"}') for i in range(count)]
    started = time.perf_counter()
    if batch_method:
        writer = MessageWriter(connection)
        for row in rows:
            writer.append(row)
        writer.flush()
    else:
        for offset in range(0, count, 100):
            group = rows[offset:offset + 100]
            for row in group:
                connection.execute('INSERT INTO messages VALUES(?,?,?,?)', row)
            connection.commit()
    duration = time.perf_counter() - started
    assert connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == count
    connection.close()
    return duration


if __name__ == '__main__':
    for mode in (False, True):
        samples = [run(mode) for _ in range(5)]
        print(('buffered_writer' if mode else 'individual'), 'median_seconds', round(statistics.median(samples), 4), 'samples', [round(x, 4) for x in samples])
