import asyncio
import atexit
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
_import_data = tempfile.TemporaryDirectory()
_previous_data = os.environ.get('BOT_DATA_DIR')
os.environ['BOT_DATA_DIR'] = _import_data.name
import bot
from transfer import message_batches, run_bounded, split_text
atexit.register(_import_data.cleanup)
atexit.register(bot.db.close)
if _previous_data is None:
    del os.environ['BOT_DATA_DIR']
else:
    os.environ['BOT_DATA_DIR'] = _previous_data


class BackupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_db, self.old_data = bot.db, bot.DATA
        self.tmp = tempfile.TemporaryDirectory()
        bot.DATA = Path(self.tmp.name)
        bot.db = sqlite3.connect(':memory:')
        bot.db.executescript('''
            CREATE TABLE backups (id INTEGER PRIMARY KEY, owner TEXT, guild TEXT, name TEXT, created TEXT, status TEXT, details TEXT DEFAULT '');
            CREATE TABLE objects (backup INTEGER, kind TEXT, id TEXT, data TEXT);
            CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT);
            CREATE TABLE restore_runs (id INTEGER PRIMARY KEY, backup INTEGER, owner TEXT, guild TEXT, created TEXT, status TEXT, details TEXT DEFAULT '');
            CREATE TABLE wipe_runs (id INTEGER PRIMARY KEY, owner TEXT, guild TEXT, created TEXT, completed TEXT, status TEXT, details TEXT DEFAULT '');
        ''')
        self.addCleanup(self.cleanup_resources)
        self.default_role = SimpleNamespace(id=10, name='@everyone', permissions=SimpleNamespace(value=1024), colour=SimpleNamespace(value=0), hoist=False, mentionable=False, position=0, managed=False, is_default=lambda: True)
        self.source = SimpleNamespace(id=10, name='source', roles=[self.default_role], channels=[], me=object(), active_threads=AsyncMock(return_value=[]))

    def cleanup_resources(self):
        bot.db.close()
        bot.db, bot.DATA = self.old_db, self.old_data
        self.tmp.cleanup()

    async def test_snapshot_and_restore_default_permissions(self):
        backup, _ = await bot.create_backup(self.source, 123)
        self.assertEqual(bot.db.execute('SELECT status FROM backups').fetchone()[0], 'complete')
        target_role = SimpleNamespace(edit=AsyncMock())
        await bot.restore_backup(SimpleNamespace(default_role=target_role), backup)
        self.assertEqual(target_role.edit.await_args.kwargs['permissions'].value, 1024)

    async def test_messages_and_attachments_round_trip(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.name, channel.category_id, channel.position = 20, 'chat', None, 0
        channel.topic, channel.nsfw, channel.slowmode_delay = 'topic', False, 0
        channel.is_news.return_value = False
        channel.overwrites = {}
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=True)

        async def save_file(path):
            path.write_bytes(b'file contents')

        attachment = SimpleNamespace(id=42, filename='hello.txt', save=save_file)
        message = SimpleNamespace(id=25, author=SimpleNamespace(id=77), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content='hello @everyone', attachments=[attachment], embeds=[])

        async def history(**kwargs):
            yield message

        async def archived_threads(**kwargs):
            if False:
                yield None

        channel.history = history
        channel.archived_threads = archived_threads
        self.source.channels = [channel]
        backup, _ = await bot.create_backup(self.source, 123)
        self.assertEqual((bot.DATA / 'attachments' / str(backup) / '42.bin').read_bytes(), b'file contents')
        sent_channel = SimpleNamespace(send=AsyncMock())
        target = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), create_text_channel=AsyncMock(return_value=sent_channel), filesize_limit=1000)
        self.assertEqual(await bot.restore_backup(target, backup), [])
        self.assertEqual(sent_channel.send.await_count, 2)
        first, second = sent_channel.send.await_args_list
        self.assertIn('hello @everyone', first.args[0])
        self.assertFalse(first.kwargs['allowed_mentions'].everyone)
        self.assertEqual(second.kwargs['file'].filename, 'hello.txt')
        second.kwargs['file'].close()

    async def test_restore_rejects_attachment_outside_backup_in_both_modes(self):
        secret = bot.DATA / 'secret.txt'
        secret.write_text('private', encoding='utf-8')
        message = {'author': 'Alice', 'created': 'today', 'content': 'hello',
                   'attachments': [{'name': 'secret.txt', 'path': '../../secret.txt'}], 'embeds': []}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (1, '2', '3', json.dumps(message)))
        for compact in (False, True):
            with self.subTest(compact=compact):
                target, warnings = SimpleNamespace(send=AsyncMock()), []
                await bot.restore_history(SimpleNamespace(filesize_limit=1000), 1, '2', target, warnings, compact=compact)
                self.assertTrue(warnings)
                self.assertEqual(target.send.await_count, 1)
                self.assertNotIn('file', target.send.await_args.kwargs)
                self.assertNotIn('files', target.send.await_args.kwargs)

    async def test_restore_runs_independent_channel_histories_concurrently(self):
        backup, _ = await bot.create_backup(self.source, 123)
        for i in range(4):
            bot.save_obj(backup, 'channel', str(20 + i), {'id':str(20 + i), 'name':f'chat-{i}', 'kind':'text', 'category':None, 'position':i, 'overwrites':[], 'topic':None, 'nsfw':False, 'slowmode':0})
            msg = {'author':'author', 'created':'2026-01-01', 'content':f'content-{i}', 'attachments':[], 'embeds':[]}
            bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, str(20 + i), str(100 + i), json.dumps(msg)))
        sends = []
        async def create_channel(**kwargs):
            async def send(content, **kwargs):
                sends.append(content)
                await asyncio.sleep(0.1)
            return SimpleNamespace(send=send)
        target = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), create_text_channel=create_channel, filesize_limit=1000)
        started = time.perf_counter()
        await bot.restore_backup(target, backup)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(sends), 4)
        self.assertLess(elapsed, 0.30, f'Independent channels were processed too slowly: {elapsed:.3f}s')

    async def test_message_pages_keep_numeric_order(self):
        for ident in ('10', '2', '1', '21', '3'):
            bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (1, '20', ident, ident))
        self.assertEqual(list(bot.iter_message_data(1, '20', page_size=2)), ['1', '2', '3', '10', '21'])

    async def test_message_page_does_not_hold_read_lock_during_transfer(self):
        old_db = bot.db
        path = bot.DATA / 'lock-test.sqlite3'
        bot.db = sqlite3.connect(path, timeout=0.05)
        try:
            bot.db.execute('CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT)')
            bot.db.executemany('INSERT INTO messages VALUES(1, ?, ?, ?)', [('20', str(i), str(i)) for i in range(1, 4)])
            bot.db.commit()
            iterator = bot.iter_message_data(1, '20', page_size=2)
            self.assertEqual(next(iterator), '1')
            other = sqlite3.connect(path, timeout=0.05)
            try:
                other.execute('INSERT INTO messages VALUES(1, ?, ?, ?)', ('21', '4', '4'))
                other.commit()
            finally:
                other.close()
            self.assertEqual(list(iterator), ['2', '3'])
        finally:
            bot.db.close()
            bot.db = old_db

    async def test_backup_does_not_hold_write_lock_while_waiting_for_history(self):
        old_db = bot.db
        path = bot.DATA / 'write-lock-test.sqlite3'
        bot.db = sqlite3.connect(path, timeout=0.05)
        bot.db.execute('CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT)')
        bot.db.execute('CREATE TABLE probe (value INTEGER)')
        bot.db.commit()
        paused = asyncio.Event()
        resume = asyncio.Event()
        msg = SimpleNamespace(id=1, author=SimpleNamespace(id=2), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content='hi', attachments=[], embeds=[])
        async def history(**kwargs):
            yield msg
            paused.set()
            await resume.wait()
        channel = SimpleNamespace(id=20, name='chat', history=history)
        task = asyncio.create_task(bot.save_history(1, channel, bot.DATA, []))
        try:
            await asyncio.wait_for(paused.wait(), 1)
            other = sqlite3.connect(path, timeout=0.05)
            try:
                other.execute('INSERT INTO probe VALUES (1)')
                other.commit()
            finally:
                other.close()
        finally:
            resume.set()
            await task
            bot.db.close()
            bot.db = old_db

    async def test_backup_flushes_received_messages_if_history_fails(self):
        msg = SimpleNamespace(id=1, author=SimpleNamespace(id=2), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content='hi', attachments=[], embeds=[])
        async def history(**kwargs):
            yield msg
            raise OSError('connection lost')
        channel = SimpleNamespace(id=20, name='chat', history=history)
        warnings = []
        incomplete = await bot.save_history(1, channel, bot.DATA, warnings)
        self.assertTrue(incomplete)
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(len(warnings), 1)

    async def test_backup_saves_system_text_and_sticker_metadata(self):
        sticker = SimpleNamespace(id=88, name='wave', url='https://example.invalid/sticker.png')
        system = SimpleNamespace(id=1, author=SimpleNamespace(id=2), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content='', attachments=[], embeds=[], stickers=[], is_system=lambda: True, system_content='Member joined the server')
        sticker_message = SimpleNamespace(id=2, author=SimpleNamespace(id=3), created_at=system.created_at, content='', attachments=[], embeds=[], stickers=[sticker], is_system=lambda: False)
        async def history(**kwargs):
            yield system
            yield sticker_message
        await bot.save_history(1, SimpleNamespace(id=20, name='chat', history=history), bot.DATA, [])
        messages = [json.loads(raw) for raw in bot.iter_message_data(1, '20')]
        self.assertEqual(messages[0]['system_content'], 'Member joined the server')
        self.assertEqual(messages[1]['stickers'][0]['id'], '88')
        batches = list(message_batches(messages, compact=True))
        text = ''.join(data for kind, data in batches if kind == 'text')
        self.assertIn('Member joined the server', text)
        self.assertIn('wave', text)
        self.assertIn('https://example.invalid/sticker.png', text)

    def test_message_writer_rolls_back_failed_batch(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT UNIQUE, data TEXT)')
        writer = bot.storage.MessageWriter(connection, max_rows=2)
        writer.append((1, '20', '1', 'one'))
        with self.assertRaises(sqlite3.IntegrityError):
            writer.append((1, '20', '1', 'duplicate'))
        writer.flush()
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_message_writer_flushes_at_bounded_size(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE messages (backup INTEGER, channel TEXT, id TEXT, data TEXT)')
        writer = bot.storage.MessageWriter(connection, max_rows=100, max_bytes=20)
        writer.append((1, '20', '1', 'abc'))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        writer.append((1, '20', '2', 'def'))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        self.assertEqual(writer.buffered_bytes, 0)

    async def test_cancelled_history_flushes_received_messages(self):
        paused = asyncio.Event()
        msg = SimpleNamespace(id=1, author=SimpleNamespace(id=2), created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), content='hi', attachments=[], embeds=[])
        async def history(**kwargs):
            yield msg
            paused.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(bot.save_history(1, SimpleNamespace(id=20, name='chat', history=history), bot.DATA, []))
        await asyncio.wait_for(paused.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(bot.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)

    async def test_worker_failure_cancels_other_workers(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def worker(item):
            if item == 0:
                started.set()
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            else:
                await started.wait()
                raise ValueError('failed')
        with self.assertRaises(ValueError):
            await run_bounded([0, 1], worker, concurrency=2)
        self.assertTrue(cancelled.is_set())

    async def test_worker_concurrency_is_bounded(self):
        active = peak = 0
        async def worker(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
        await run_bounded(range(12), worker, concurrency=3)
        self.assertEqual(peak, 3)

    async def test_history_keeps_message_order_in_channel(self):
        backup, _ = await bot.create_backup(self.source, 123)
        for ident in ('10', '2', '3'):
            message = {'author':'a', 'created':'date', 'content':f'message-{ident}', 'attachments':[], 'embeds':[]}
            bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', ident, json.dumps(message)))
        target = SimpleNamespace(send=AsyncMock())
        await bot.restore_history(SimpleNamespace(filesize_limit=1000), backup, '20', target, [])
        sent = [call.args[0].splitlines()[-1] for call in target.send.await_args_list]
        self.assertEqual(sent, ['message-2', 'message-3', 'message-10'])

    async def test_compact_mode_reduces_requests_and_preserves_order(self):
        backup, _ = await bot.create_backup(self.source, 123)
        for ident in range(1, 11):
            message = {'author': f'user-{ident}', 'created':'date', 'content':f'message-{ident}', 'attachments':[], 'embeds':[]}
            bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', str(ident), json.dumps(message)))
        target = SimpleNamespace(send=AsyncMock())
        await bot.restore_history(SimpleNamespace(filesize_limit=1000), backup, '20', target, [], compact=True)
        self.assertEqual(target.send.await_count, 1)
        text = target.send.await_args.args[0]
        offsets = [text.index(f'**user-{ident}**') for ident in range(1, 11)]
        self.assertEqual(offsets, sorted(offsets))
        self.assertFalse(target.send.await_args.kwargs['allowed_mentions'].everyone)

    def test_compact_mode_keeps_media_between_messages(self):
        def msg(value, media=False):
            return {'author':'a', 'created':'date', 'content':value, 'attachments':[{'path':'x','name':'x'}] if media else [], 'embeds':[]}
        batches = list(message_batches([msg('one'), msg('two', True), msg('three')], compact=True))
        self.assertEqual([kind for kind, _ in batches], ['text', 'bundle', 'text'])
        self.assertIn('one', batches[0][1])
        self.assertIn('two', batches[1][1][0])
        self.assertIn('three', batches[2][1])

    async def test_compact_mode_sends_text_and_files_together(self):
        backup, _ = await bot.create_backup(self.source, 123)
        directory = bot.DATA / 'attachments' / str(backup)
        attachments = []
        for number in range(3):
            filename = f'{number}.bin'
            (directory / filename).write_bytes(b'content')
            attachments.append({'path': filename, 'name': filename})
        msg = {'author':'a', 'created':'date', 'content':'with files', 'attachments':attachments, 'embeds':[]}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', '30', json.dumps(msg)))
        target = SimpleNamespace(send=AsyncMock())
        warnings = []
        await bot.restore_history(SimpleNamespace(filesize_limit=1000), backup, '20', target, warnings, compact=True)
        self.assertEqual(target.send.await_count, 1)
        self.assertIn('with files', target.send.await_args.args[0])
        self.assertEqual(len(target.send.await_args.kwargs['files']), 3)
        self.assertEqual(warnings, [])

    async def test_bundle_skips_large_or_missing_files_without_losing_text(self):
        backup, _ = await bot.create_backup(self.source, 123)
        directory = bot.DATA / 'attachments' / str(backup)
        (directory / 'large.bin').write_bytes(b'too large')
        msg = {'author':'a', 'created':'date', 'content':'message', 'attachments':[{'path':'large.bin','name':'large'}, {'path':'missing.bin','name':'missing'}], 'embeds':[]}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', '30', json.dumps(msg)))
        target = SimpleNamespace(send=AsyncMock())
        warnings = []
        await bot.restore_history(SimpleNamespace(filesize_limit=3), backup, '20', target, warnings, compact=True)
        self.assertEqual(target.send.await_count, 1)
        self.assertIn('message', target.send.await_args.args[0])
        self.assertEqual(len(warnings), 2)

    async def test_rejected_bundle_retries_files_individually(self):
        backup, _ = await bot.create_backup(self.source, 123)
        directory = bot.DATA / 'attachments' / str(backup)
        entries = []
        for number in range(2):
            name = f'{number}.bin'
            (directory / name).write_bytes(b'content')
            entries.append({'path':name, 'name':name})
        msg = {'author':'a', 'created':'date', 'content':'message', 'attachments':entries, 'embeds':[]}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', '30', json.dumps(msg)))
        rejection = discord.HTTPException(SimpleNamespace(status=400, reason='Bad Request'), {'code': 50035, 'message': 'Invalid'})
        target = SimpleNamespace(send=AsyncMock(side_effect=[rejection, None, None, None]))
        warnings = []
        await bot.restore_history(SimpleNamespace(filesize_limit=1000), backup, '20', target, warnings, compact=True)
        self.assertEqual(target.send.await_count, 4)
        self.assertIn('message', target.send.await_args_list[1].args[0])
        self.assertIn('file', target.send.await_args_list[2].kwargs)
        self.assertIn('file', target.send.await_args_list[3].kwargs)
        self.assertEqual(warnings, [])

    async def test_bundle_observes_total_size_limit(self):
        backup, _ = await bot.create_backup(self.source, 123)
        directory = bot.DATA / 'attachments' / str(backup)
        entries = []
        for number in range(3):
            name = f'{number}.bin'
            (directory / name).write_bytes(b'1234')
            entries.append({'path':name, 'name':name})
        msg = {'author':'a', 'created':'date', 'content':'message', 'attachments':entries, 'embeds':[]}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '20', '30', json.dumps(msg)))
        target = SimpleNamespace(send=AsyncMock())
        await bot.restore_history(SimpleNamespace(filesize_limit=8), backup, '20', target, [], compact=True)
        self.assertEqual([len(call.kwargs['files']) for call in target.send.await_args_list], [2, 1])

    def test_compact_mode_respects_utf16_limit_and_long_messages(self):
        content = '😀' * 2100
        message = {'author':'a', 'created':'date', 'content':content, 'attachments':[], 'embeds':[]}
        texts = [data for kind, data in message_batches([message], compact=True) if kind == 'text']
        self.assertTrue(all(len(text.encode('utf-16-le')) // 2 <= 1900 for text in texts))
        self.assertTrue(''.join(texts).endswith(content))
        self.assertEqual(''.join(split_text(content)), content)

    async def test_missing_overwrite_preserves_category_channel_and_history(self):
        backup, _ = await bot.create_backup(self.source, 123)
        missing = [{'kind': 'role', 'id': '999', 'allow': 0, 'deny': 1024}]
        bot.save_obj(backup, 'channel', '54', {'id': '54', 'name': 'category', 'kind': 'category', 'category': None, 'position': 0, 'overwrites': missing})
        bot.save_obj(backup, 'channel', '55', {'id': '55', 'name': 'private', 'kind': 'text', 'category': '54', 'position': 0, 'overwrites': missing, 'topic': None, 'nsfw': False, 'slowmode': 0})
        message = {'author': 'Alice', 'created': 'today', 'content': 'archived', 'attachments': [], 'embeds': []}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '55', '1', json.dumps(message)))
        category = object()
        channel = SimpleNamespace(send=AsyncMock())
        target = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), create_category=AsyncMock(return_value=category), create_text_channel=AsyncMock(return_value=channel))
        warnings = await bot.restore_backup(target, backup)
        target.create_category.assert_awaited_once()
        target.create_text_channel.assert_awaited_once()
        self.assertIs(target.create_text_channel.await_args.kwargs['category'], category)
        self.assertEqual(target.create_text_channel.await_args.kwargs['overwrites'], {})
        self.assertIn('archived', channel.send.await_args.args[0])
        self.assertTrue(any('category' in warning and '999' in warning for warning in warnings))
        self.assertTrue(any('private' in warning and '999' in warning for warning in warnings))

    def test_uncached_role_overwrite_remains_role(self):
        target = discord.Object(id=123, type=discord.Role)
        channel = SimpleNamespace(overwrites={target: discord.PermissionOverwrite(view_channel=False)})
        self.assertEqual(bot.overwrites(channel)[0]['kind'], 'role')

    async def test_active_and_archived_threads_are_deduplicated(self):
        backup, _ = await bot.create_backup(self.source, 123)
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.name = 20, 'parent'
        thread = SimpleNamespace(id=30, parent_id=20, name='thread', type=discord.ChannelType.public_thread, archived=True, locked=False, invitable=True, slowmode_delay=0, auto_archive_duration=1440, applied_tags=[])

        async def history(**kwargs):
            if False:
                yield None

        async def archived(**kwargs):
            if not kwargs.get('private'):
                yield thread

        thread.history = history
        channel.archived_threads = archived
        warnings = []
        incomplete = await bot.save_threads(backup, self.source, channel, [thread], bot.DATA, warnings)
        self.assertFalse(incomplete)
        self.assertEqual(len(bot.get_objects(backup, 'thread')), 1)

    async def test_private_thread_restores_with_private_type(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.save_obj(backup, 'channel', '20', {'id':'20', 'name':'chat', 'kind':'text', 'category':None, 'position':0, 'overwrites':[], 'topic':None, 'nsfw':False, 'slowmode':0})
        bot.save_obj(backup, 'thread', '30', {'id':'30', 'parent':'20', 'name':'private', 'private':True, 'archived':False, 'locked':False, 'invitable':False, 'slowmode':0, 'auto_archive_duration':1440, 'tags':[]})
        new_thread = SimpleNamespace(send=AsyncMock(), edit=AsyncMock())
        new_channel = SimpleNamespace(send=AsyncMock(), create_thread=AsyncMock(return_value=new_thread))
        target = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), create_text_channel=AsyncMock(return_value=new_channel), filesize_limit=1000)
        await bot.restore_backup(target, backup)
        self.assertEqual(new_channel.create_thread.await_args.kwargs['type'], discord.ChannelType.private_thread)
        self.assertFalse(new_channel.create_thread.await_args.kwargs['invitable'])

    async def test_forum_tags_and_thread_messages_are_restored(self):
        backup, _ = await bot.create_backup(self.source, 123)
        tag = {'id':'8', 'name':'support', 'moderated':False, 'emoji':None}
        bot.save_obj(backup, 'channel', '20', {'id':'20', 'name':'forum', 'kind':'forum', 'category':None, 'position':0, 'overwrites':[], 'topic':None, 'nsfw':False, 'slowmode':0, 'tags':[tag]})
        bot.save_obj(backup, 'thread', '30', {'id':'30', 'parent':'20', 'name':'post', 'private':False, 'archived':True, 'locked':False, 'invitable':True, 'slowmode':0, 'auto_archive_duration':1440, 'tags':['8']})
        msg = {'author':'author', 'created':'2026-01-01', 'content':'thread text', 'attachments':[], 'embeds':[]}
        bot.db.execute('INSERT INTO messages VALUES(?,?,?,?)', (backup, '30', '31', json.dumps(msg)))
        new_thread = SimpleNamespace(send=AsyncMock(), edit=AsyncMock())
        new_tag = SimpleNamespace(name='support')
        forum = SimpleNamespace(available_tags=[new_tag], create_thread=AsyncMock(return_value=SimpleNamespace(thread=new_thread)))
        target = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), create_forum=AsyncMock(return_value=forum), filesize_limit=1000)
        await bot.restore_backup(target, backup)
        self.assertEqual(forum.create_thread.await_args.kwargs['applied_tags'], [new_tag])
        self.assertIn('thread text', new_thread.send.await_args.args[0])
        self.assertTrue(new_thread.edit.await_args.kwargs['archived'])

    async def test_missing_history_permission_is_partial(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id, channel.name, channel.category_id, channel.position = 20, 'private', None, 0
        channel.topic, channel.nsfw, channel.slowmode_delay = None, False, 0
        channel.is_news.return_value = False
        channel.overwrites = {}
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=False)
        self.source.channels = [channel]
        backup, warnings = await bot.create_backup(self.source, 123)
        self.assertEqual(bot.db.execute('SELECT status FROM backups WHERE id=?', (backup,)).fetchone()[0], 'partial')
        self.assertTrue(any('private' in warning for warning in warnings))
        channel.history.assert_not_called()

    async def test_corrupt_backup_fails_before_creating_roles(self):
        bot.db.execute("INSERT INTO backups (id, owner, guild, name, created, status) VALUES (1, '1', '1', 'x', 'x', 'complete')")
        guild = SimpleNamespace(create_role=AsyncMock())
        with self.assertRaises(ValueError):
            await bot.restore_backup(guild, 1)
        guild.create_role.assert_not_awaited()

    async def test_role_position_cannot_exceed_bot_hierarchy(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.save_obj(backup, 'role', '11', {'id':'11', 'name':'member', 'permissions':0, 'colour':0, 'hoist':False, 'mentionable':False, 'position':1, 'managed':False, 'default':False})
        created = Mock(spec=discord.Role)
        guild = SimpleNamespace(default_role=SimpleNamespace(edit=AsyncMock()), me=SimpleNamespace(top_role=SimpleNamespace(position=1)), create_role=AsyncMock(return_value=created), edit_role_positions=AsyncMock())
        warnings = await bot.restore_backup(guild, backup)
        guild.edit_role_positions.assert_not_awaited()
        self.assertTrue(any('Порядок ролей' in warning for warning in warnings))

    async def test_snapshot_failure_is_recorded(self):
        self.source.roles = [SimpleNamespace()]
        with self.assertRaises(AttributeError):
            await bot.create_backup(self.source, 123)
        status, details = bot.db.execute('SELECT status, details FROM backups').fetchone()
        self.assertEqual(status, 'failed')
        self.assertIn('AttributeError', json.loads(details)[0])

    async def test_restore_duplicate_is_blocked(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.db.execute("INSERT INTO restore_runs(backup, owner, guild, created, status) VALUES (?, '123', '20', 'now', 'failed')", (backup,))
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'restore_backup', new_callable=AsyncMock) as restore:
            await bot.restore_command.callback(interaction, backup)
            restore.assert_not_awaited()
        self.assertIn('заблокирован', interaction.followup.send.await_args.args[0])

    async def test_successful_wipe_allows_restore_again(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.db.execute("INSERT INTO restore_runs(backup, owner, guild, created, status) VALUES (?, '123', '20', '2026-01-01', 'failed')", (backup,))
        bot.db.execute("INSERT INTO wipe_runs(owner, guild, created, completed, status) VALUES ('123', '20', '2026-01-02', '2026-01-02', 'complete')")
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'restore_backup', new_callable=AsyncMock, return_value=[]) as restore:
            await bot.restore_command.callback(interaction, backup)
            restore.assert_awaited_once()

    async def test_restore_command_passes_compact_mode(self):
        backup, _ = await bot.create_backup(self.source, 123)
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'restore_backup', new_callable=AsyncMock, return_value=[]) as restore:
            await bot.restore_command.callback(interaction, backup, 'compact')
        self.assertTrue(restore.await_args.kwargs['compact'])

    async def test_wipe_keeps_control_channel_and_managed_roles(self):
        control = SimpleNamespace(id=1, delete=AsyncMock())
        other = SimpleNamespace(id=2, delete=AsyncMock())
        default = SimpleNamespace(is_default=lambda: True, managed=False, delete=AsyncMock())
        managed = SimpleNamespace(is_default=lambda: False, managed=True, delete=AsyncMock())
        ordinary = SimpleNamespace(is_default=lambda: False, managed=False, delete=AsyncMock())
        guild = SimpleNamespace(fetch_channels=AsyncMock(side_effect=[[control, other], [control]]), fetch_roles=AsyncMock(side_effect=[[default, managed, ordinary], [default, managed]]))
        self.assertEqual(await bot.wipe_test_server(guild, 1), [])
        control.delete.assert_not_awaited()
        managed.delete.assert_not_awaited()
        other.delete.assert_awaited_once()
        ordinary.delete.assert_awaited_once()

    async def test_wipe_protects_source_server(self):
        await bot.create_backup(self.source, 123)
        channel = Mock(spec=discord.TextChannel)
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=10), channel=channel, user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))
        await bot.wipe_test_command.callback(interaction)
        interaction.response.defer.assert_not_awaited()
        self.assertIn('запрещена', interaction.response.send_message.await_args.args[0])

    def test_guild_wipe_command_dispatches_to_global_handler(self):
        command, options = bot.tree._get_app_command_options({'name':'wipe_test', 'guild_id':'123456789012345678', 'options':[]})
        self.assertEqual(command.name, 'wipe_test')
        self.assertEqual(options, [])

    async def test_partial_wipe_blocks_restore(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.db.execute("INSERT INTO wipe_runs(owner, guild, created, completed, status) VALUES ('123', '20', '2026-01-02', '2026-01-02', 'partial')")
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'restore_backup', new_callable=AsyncMock) as restore:
            await bot.restore_command.callback(interaction, backup)
            restore.assert_not_awaited()
        self.assertIn('очистка', interaction.followup.send.await_args.args[0])

    async def test_restore_in_progress_blocks_wipe(self):
        bot.db.execute("INSERT INTO restore_runs(backup, owner, guild, created, status) VALUES (1, '123', '20', 'now', 'running')")
        channel = Mock(spec=discord.TextChannel)
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), channel=channel, user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'wipe_test_server', new_callable=AsyncMock) as wipe:
            await bot.wipe_test_command.callback(interaction)
            wipe.assert_not_awaited()
        self.assertIn('выполняется', interaction.followup.send.await_args.args[0])

    async def test_partial_backup_restores_without_confirmation(self):
        backup, _ = await bot.create_backup(self.source, 123)
        bot.db.execute("UPDATE backups SET status='partial' WHERE id=?", (backup,))
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=123, id=20), user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'restore_backup', new_callable=AsyncMock, return_value=[]) as restore:
            await bot.restore_command.callback(interaction, backup)
            restore.assert_awaited_once_with(interaction.guild, backup, compact=False)

    def test_command_parameters_do_not_require_confirmation_or_server_id(self):
        self.assertEqual([param.name for param in bot.wipe_test_command.parameters], [])
        self.assertEqual([param.name for param in bot.restore_command.parameters], ['backup_id', 'mode'])

    async def test_wipe_runs_on_current_server_without_parameters(self):
        bot.db.execute("INSERT INTO restore_runs(backup, owner, guild, created, status) VALUES (1, '123', '20', 'now', 'failed')")
        channel = Mock(spec=discord.TextChannel)
        channel.id = 42
        guild = SimpleNamespace(owner_id=123, id=20)
        interaction = SimpleNamespace(guild=guild, channel=channel, user=SimpleNamespace(id=123), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(bot, 'wipe_test_server', new_callable=AsyncMock, return_value=[]) as wipe:
            await bot.wipe_test_command.callback(interaction)
            wipe.assert_awaited_once_with(guild, 42)


if __name__ == '__main__':
    unittest.main()
