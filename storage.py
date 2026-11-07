"""SQLite access helpers used by the transfer pipeline."""


class MessageWriter:
    """Buffer messages without holding a write transaction across network waits."""

    def __init__(self, connection, *, max_rows=100, max_bytes=1024 * 1024):
        self.connection = connection
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.rows = []
        self.buffered_bytes = 0
        self.failed = False

    def append(self, row):
        self.rows.append(row)
        # UTF-8 takes at most four bytes per Python character; this upper bound
        # avoids allocating a second copy of each JSON payload just to count it.
        self.buffered_bytes += len(row[3]) * 4
        if len(self.rows) >= self.max_rows or self.buffered_bytes >= self.max_bytes:
            self.flush()

    def flush(self):
        if not self.rows or self.failed:
            return
        try:
            self.connection.executemany('INSERT INTO messages VALUES(?,?,?,?)', self.rows)
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            self.failed = True
            raise
        self.rows.clear()
        self.buffered_bytes = 0


def iter_message_data(connection, backup, channel, *, page_size=100):
    """Read a bounded page at a time, releasing the cursor before yielding."""
    if page_size < 1:
        raise ValueError('page_size must be positive')
    last_id = -1
    while True:
        rows = connection.execute(
            'SELECT id, data FROM messages '
            'WHERE backup=? AND channel=? AND CAST(id AS INTEGER)>? '
            'ORDER BY CAST(id AS INTEGER) LIMIT ?',
            (backup, channel, last_id, page_size),
        ).fetchall()
        if not rows:
            return
        for message_id, data in rows:
            yield data
        last_id = int(rows[-1][0])
