import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv
from transfer import message_batches, run_bounded
from attachments import attachment_path, send_bundle
import storage

BASE = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
DATA = Path(os.environ.get("BOT_DATA_DIR", BASE / "data"))
DATA.mkdir(exist_ok=True)
db = sqlite3.connect(DATA / "backups.sqlite3")
db.execute("PRAGMA foreign_keys = ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS backups (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, guild TEXT NOT NULL, name TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS objects (backup INTEGER NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (backup, kind, id), FOREIGN KEY (backup) REFERENCES backups(id));
CREATE TABLE IF NOT EXISTS messages (backup INTEGER NOT NULL, channel TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (backup, id), FOREIGN KEY (backup) REFERENCES backups(id));
CREATE INDEX IF NOT EXISTS messages_by_channel_order ON messages(backup, channel, CAST(id AS INTEGER));
CREATE TABLE IF NOT EXISTS restore_runs (id INTEGER PRIMARY KEY, backup INTEGER NOT NULL, owner TEXT NOT NULL, guild TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', FOREIGN KEY (backup) REFERENCES backups(id));
CREATE TABLE IF NOT EXISTS wipe_runs (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, guild TEXT NOT NULL, created TEXT NOT NULL, completed TEXT, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '');
""")
db.commit()
lock = asyncio.Lock()
log = logging.getLogger("backup")


def save_obj(backup, kind, ident, value):
    db.execute("INSERT INTO objects VALUES (?, ?, ?, ?)", (backup, kind, str(ident), json.dumps(value, ensure_ascii=False)))


def get_objects(backup, kind):
    return [json.loads(row[0]) for row in db.execute("SELECT data FROM objects WHERE backup=? AND kind=?", (backup, kind))]


def overwrites(channel):
    result = []
    for target, value in channel.overwrites.items():
        allow, deny = value.pair()
        is_role = isinstance(target, discord.Role) or (isinstance(target, discord.Object) and target.type is discord.Role)
        result.append({"id": str(target.id), "kind": "role" if is_role else "member", "allow": allow.value, "deny": deny.value})
    return result


async def save_history(backup, channel, attachment_dir, warnings):
    incomplete = False
    writer = storage.MessageWriter(db)
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            files = []
            for attachment in msg.attachments:
                name = f"{attachment.id}.bin"
                try:
                    await attachment.save(attachment_dir / name)
                    files.append({"path": name, "name": attachment.filename})
                except (discord.HTTPException, OSError) as exc:
                    incomplete = True
                    warnings.append(f"Вложение {attachment.id}: {type(exc).__name__}")
            is_system = getattr(msg, 'is_system', lambda: False)()
            stickers = [{"id": str(sticker.id), "name": sticker.name, "url": str(sticker.url)} for sticker in getattr(msg, 'stickers', [])]
            value = {"author": str(msg.author), "author_id": str(msg.author.id), "created": msg.created_at.isoformat(), "content": msg.content, "system_content": msg.system_content if is_system else None, "stickers": stickers, "attachments": files, "embeds": [e.to_dict() for e in msg.embeds]}
            writer.append((backup, str(channel.id), str(msg.id), json.dumps(value, ensure_ascii=False)))
    except (discord.HTTPException, OSError) as exc:
        incomplete = True
        warnings.append(f"История {channel.name}: {type(exc).__name__}")
    finally:
        writer.flush()
    return incomplete


async def save_threads(backup, guild, channel, active_threads, attachment_dir, warnings):
    incomplete = False
    threads = {thread.id: thread for thread in active_threads if thread.parent_id == channel.id}
    try:
        async for thread in channel.archived_threads(limit=None):
            threads[thread.id] = thread
        if isinstance(channel, discord.TextChannel):
            async for thread in channel.archived_threads(limit=None, private=True):
                threads[thread.id] = thread
    except discord.HTTPException as exc:
        warnings.append(f"Список архивных веток {channel.name}: {type(exc).__name__}")
        incomplete = True
    for thread in threads.values():
        item = {"id": str(thread.id), "parent": str(channel.id), "name": thread.name,
                "private": thread.type == discord.ChannelType.private_thread,
                "archived": thread.archived, "locked": thread.locked,
                "invitable": thread.invitable, "slowmode": thread.slowmode_delay,
                "auto_archive_duration": thread.auto_archive_duration,
                "tags": [str(tag.id) for tag in getattr(thread, 'applied_tags', [])]}
        save_obj(backup, "thread", thread.id, item)
        db.commit()
        if item['private']:
            warnings.append(f"Участники приватной ветки {thread.name} не переносятся; доступ сохранит только бот и модераторы.")
        incomplete |= await save_history(backup, thread, attachment_dir, warnings)
    return incomplete


async def create_backup(guild, owner):
    cursor = db.execute("INSERT INTO backups(owner,guild,name,created,status) VALUES(?,?,?,?,?)", (str(owner), str(guild.id), guild.name, datetime.now(timezone.utc).isoformat(), "running"))
    backup = cursor.lastrowid
    db.commit()
    attachment_dir = DATA / "attachments" / str(backup)
    warnings = []
    incomplete = False
    try:
        attachment_dir.mkdir(parents=True, exist_ok=True)
        for role in guild.roles:
            save_obj(backup, "role", role.id, {"id": str(role.id), "name": role.name, "permissions": role.permissions.value, "colour": role.colour.value, "hoist": role.hoist, "mentionable": role.mentionable, "position": role.position, "managed": role.managed, "default": role.is_default()})
        db.commit()
        active_threads = []
        if any(isinstance(c, (discord.TextChannel, discord.ForumChannel)) for c in guild.channels):
            try:
                active_threads = await guild.active_threads()
            except discord.HTTPException as exc:
                incomplete = True
                warnings.append(f"Активные ветки: {type(exc).__name__}")
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel): kind = "category"
            elif isinstance(channel, discord.TextChannel): kind = "news" if channel.is_news() else "text"
            elif isinstance(channel, discord.VoiceChannel): kind = "voice"
            elif isinstance(channel, discord.StageChannel): kind = "stage"
            elif isinstance(channel, discord.ForumChannel): kind = "media" if channel.type == discord.ChannelType.media else "forum"
            else:
                incomplete = True
                warnings.append(f"Неизвестный тип канала: {channel.name} ({channel.type})")
                continue
            item = {"id": str(channel.id), "name": channel.name, "kind": kind, "category": str(channel.category_id) if channel.category_id else None, "position": channel.position, "overwrites": overwrites(channel), "topic": getattr(channel, "topic", None), "nsfw": getattr(channel, "nsfw", False), "slowmode": getattr(channel, "slowmode_delay", 0), "bitrate": getattr(channel, "bitrate", None), "user_limit": getattr(channel, "user_limit", None)}
            if kind in ("forum", "media"):
                if any(tag.emoji and tag.emoji.is_custom_emoji() for tag in channel.available_tags):
                    warnings.append(f"Пользовательские эмодзи тегов форума {channel.name} не переносятся.")
                item["tags"] = [{"id": str(tag.id), "name": tag.name, "moderated": tag.moderated,
                                 "emoji": str(tag.emoji) if tag.emoji and not tag.emoji.is_custom_emoji() else None}
                                for tag in channel.available_tags]
            save_obj(backup, "channel", channel.id, item)
            db.commit()
            if kind == "category":
                continue
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.read_message_history:
                incomplete = True
                warnings.append(f"История {channel.name}: нет прав просмотра или чтения истории")
                continue
            if kind in ("text", "news", "voice", "stage"):
                incomplete |= await save_history(backup, channel, attachment_dir, warnings)
            if kind in ("text", "news", "forum", "media"):
                incomplete |= await save_threads(backup, guild, channel, active_threads, attachment_dir, warnings)
        warnings.append("Реакции, стикеры и назначения ролей участникам не копируются.")
        db.execute("UPDATE backups SET status=?, details=? WHERE id=?", ("partial" if incomplete else "complete", json.dumps(warnings, ensure_ascii=False), backup))
        db.commit()
        return backup, warnings
    except BaseException as exc:
        db.execute("UPDATE backups SET status='failed', details=? WHERE id=?", (json.dumps([f"Ошибка: {type(exc).__name__}"], ensure_ascii=False), backup))
        db.commit()
        raise


async def restore_history(guild, backup, source_id, target, warnings, *, compact=False):
    messages = (json.loads(raw) for raw in iter_message_data(backup, source_id))
    for kind, payload in message_batches(messages, compact=compact):
        if kind == 'text':
            await target.send(payload, allowed_mentions=discord.AllowedMentions.none())
            continue
        if kind == 'bundle':
            text, msg = payload
            await send_bundle(target, text, msg['attachments'], DATA / 'attachments' / str(backup), guild.filesize_limit, warnings)
            continue
        msg = payload
        for embed in msg.get('embeds', []):
            try:
                await target.send(embed=discord.Embed.from_dict(embed), allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, ValueError, TypeError) as exc:
                warnings.append(f"Embed в {source_id}: {type(exc).__name__}")
        for attachment in msg["attachments"]:
            try:
                path = attachment_path(DATA / "attachments" / str(backup), attachment["path"])
                if path.stat().st_size > guild.filesize_limit:
                    warnings.append(f"Вложение {attachment['name']} превышает лимит нового сервера")
                    continue
                file = discord.File(path, filename=attachment["name"])
                try:
                    await target.send(file=file, allowed_mentions=discord.AllowedMentions.none())
                finally:
                    file.close()
            except (discord.HTTPException, OSError, ValueError, KeyError) as exc:
                warnings.append(f"Вложение {attachment['name']}: {type(exc).__name__}")


def iter_message_data(backup, channel, *, page_size=100):
    return storage.iter_message_data(db, backup, channel, page_size=page_size)


async def restore_backup(guild, backup, existing_roles=None, *, compact=False):
    roles = sorted(get_objects(backup, "role"), key=lambda x: x["position"])
    channels = get_objects(backup, "channel")
    role_map = {}
    channel_map = {}
    tag_maps = {}
    warnings = []
    history_jobs = []
    if sum(bool(role.get("default")) for role in roles) != 1:
        raise ValueError("Архив не содержит ровно одну роль @everyone")
    for role in roles:
        if role["default"]:
            role_map[role["id"]] = guild.default_role
        elif role["managed"]:
            warnings.append(f"Интеграционная роль не создана: {role['name']}")
        else:
            new = existing_roles[role['id']] if existing_roles is not None else await guild.create_role(name=role["name"], permissions=discord.Permissions(role["permissions"]), colour=discord.Colour(role["colour"]), hoist=role["hoist"], mentionable=role["mentionable"], reason=f"Restore backup {backup}")
            role_map[role["id"]] = new
    created_roles = [role_map[r["id"]] for r in roles if not r["default"] and not r["managed"]]
    if created_roles:
        if guild.me.top_role.position <= len(created_roles):
            warnings.append("Порядок ролей не восстановлен: поднимите роль бота выше создаваемых ролей.")
        else:
            try:
                await guild.edit_role_positions(positions={role: i + 1 for i, role in enumerate(created_roles)}, reason=f"Restore backup {backup}")
            except discord.HTTPException as exc:
                warnings.append(f"Порядок ролей не восстановлен: {type(exc).__name__}")
    default = next(r for r in roles if r["default"])
    await guild.default_role.edit(permissions=discord.Permissions(default["permissions"]), reason=f"Restore backup {backup}")
    for item in sorted(channels, key=lambda x: (x["kind"] != "category", x["position"])):
        permission_map = {}
        missing = []
        for entry in item["overwrites"]:
            if entry["kind"] == "role":
                target = role_map.get(entry["id"])
            else:
                try:
                    target = await guild.fetch_member(int(entry["id"]))
                except discord.NotFound:
                    target = None
            if target is None:
                missing.append(entry["id"])
                continue
            permission_map[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(entry["allow"]), discord.Permissions(entry["deny"]))
        if missing:
            warnings.append(f"Канал {item['name']}: не перенесены права для объектов {', '.join(missing)}; проверьте доступ к каналу.")
        parent = channel_map.get(item["category"]) if item["category"] else None
        if item["category"] and parent is None:
            warnings.append(f"Канал {item['name']} пропущен: категория не создана")
            continue
        kwargs = {"name": item["name"], "overwrites": permission_map, "position": item["position"], "reason": f"Restore backup {backup}"}
        if item["kind"] != "category": kwargs["category"] = parent
        kind = item["kind"]
        if kind == "category": new = await guild.create_category(**kwargs)
        elif kind in ("text", "news"):
            new = await guild.create_text_channel(**kwargs, topic=item["topic"], nsfw=item["nsfw"], slowmode_delay=item["slowmode"], news=kind == "news")
        elif kind == "voice": new = await guild.create_voice_channel(**kwargs, bitrate=min(item["bitrate"], guild.bitrate_limit), user_limit=item["user_limit"])
        elif kind == "stage": new = await guild.create_stage_channel(**kwargs, bitrate=min(item["bitrate"], guild.bitrate_limit), user_limit=item["user_limit"])
        elif kind in ("forum", "media"):
            tags = item.get('tags', [])
            forum_tags = [discord.ForumTag(name=tag['name'], moderated=tag['moderated'], emoji=tag.get('emoji')) for tag in tags]
            new = await guild.create_forum(**kwargs, topic=item["topic"], nsfw=item["nsfw"], slowmode_delay=item["slowmode"], media=kind == "media", available_tags=forum_tags)
            tag_maps[item['id']] = {old['id']: new_tag for old, new_tag in zip(tags, new.available_tags)}
        else: continue
        channel_map[item["id"]] = new
        if kind in ("text", "news", "voice", "stage"):
            history_jobs.append((item['id'], new))
    async def restore_channel(job):
        source_id, destination = job
        await restore_history(guild, backup, source_id, destination, warnings, compact=compact)

    await run_bounded(history_jobs, restore_channel, concurrency=4)
    for thread in get_objects(backup, 'thread'):
        parent = channel_map.get(thread['parent'])
        if parent is None:
            warnings.append(f"Ветка {thread['name']} пропущена: исходный канал не восстановлен")
            continue
        try:
            if thread['parent'] in tag_maps:
                available = tag_maps[thread['parent']]
                tags = [available[tag] for tag in thread['tags'] if tag in available]
                result = await parent.create_thread(name=thread['name'], content='Восстановленная публикация', applied_tags=tags, allowed_mentions=discord.AllowedMentions.none(), auto_archive_duration=thread['auto_archive_duration'], slowmode_delay=thread['slowmode'])
                new_thread = result.thread
            else:
                new_thread = await parent.create_thread(name=thread['name'], type=discord.ChannelType.private_thread if thread['private'] else discord.ChannelType.public_thread, auto_archive_duration=thread['auto_archive_duration'], slowmode_delay=thread['slowmode'], invitable=thread['invitable'])
            await restore_history(guild, backup, thread['id'], new_thread, warnings, compact=compact)
            if thread['archived'] or thread['locked']:
                await new_thread.edit(archived=thread['archived'], locked=thread['locked'])
        except discord.HTTPException as exc:
            warnings.append(f"Ветка {thread['name']}: {type(exc).__name__}")
    return warnings


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    log.info("Бот запущен: %s", client.user)


def is_owner(interaction):
    return interaction.guild is not None and interaction.guild.owner_id == interaction.user.id


@tree.command(name="backup", description="Сохранить сервер в локальную базу (только владелец)")
async def backup_command(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    if lock.locked():
        return await interaction.response.send_message("Другая операция уже выполняется.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with lock:
        try:
            backup, warnings = await create_backup(interaction.guild, interaction.user.id)
            status = db.execute("SELECT status FROM backups WHERE id=?", (backup,)).fetchone()[0]
            log.info("Backup #%s: %s, warnings: %s", backup, status, warnings)
        except Exception:
            log.exception("Ошибка создания снимка")
            await interaction.followup.send("Ошибка создания снимка. Проверьте консоль; снимок помечен как failed.", ephemeral=True)
            return
        try:
            await interaction.followup.send(f"Снимок #{backup}: {status}. Замечаний: {len(warnings)}. Подробности: /backup_info {backup}", ephemeral=True)
        except discord.HTTPException:
            log.warning("Interaction expired; use /backup_info for backup #%s", backup)


@tree.command(name="backup_info", description="Показать свои снимки или детали снимка")
@app_commands.describe(backup_id="ID снимка; 0 показывает список")
async def backup_info(interaction: discord.Interaction, backup_id: int = 0):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    rows = db.execute("SELECT id,name,created,status,details FROM backups WHERE owner=? AND (?=0 OR id=?) ORDER BY id DESC LIMIT 20", (str(interaction.user.id), backup_id, backup_id)).fetchall()
    lines = []
    for ident, name, created, status, details in rows:
        lines.append(f"#{ident} | {name} | {created} | {status}")
        if backup_id and details:
            lines.extend(json.loads(details))
    text = "\n".join(lines) or "Снимки не найдены."
    await interaction.response.send_message(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="restore", description="Восстановить свой снимок на этом сервере")
@app_commands.describe(backup_id="ID снимка", mode="Отдельные сообщения или компактные блоки")
@app_commands.choices(mode=[app_commands.Choice(name="Отдельные сообщения", value="separate"), app_commands.Choice(name="Компактно: объединять короткие сообщения", value="compact")])
async def restore_command(interaction: discord.Interaction, backup_id: int, mode: str = 'separate'):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    if mode not in ('separate', 'compact'):
        return await interaction.response.send_message("Неизвестный режим восстановления.", ephemeral=True)
    row = db.execute("SELECT guild,status FROM backups WHERE id=? AND owner=?", (backup_id, str(interaction.user.id))).fetchone()
    if not row or row[1] not in ("complete", "partial"):
        return await interaction.response.send_message("Готовый снимок не найден или принадлежит другому пользователю.", ephemeral=True)
    if row[0] == str(interaction.guild.id):
        return await interaction.response.send_message("Восстановление на исходный сервер запрещено.", ephemeral=True)
    if lock.locked():
        return await interaction.response.send_message("Другая операция уже выполняется.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with lock:
        if db.execute("SELECT 1 FROM wipe_runs WHERE guild=? AND status='running'", (str(interaction.guild.id),)).fetchone():
            return await interaction.followup.send("Очистка сервера ещё выполняется или была прервана; проверьте её состояние.", ephemeral=True)
        if db.execute("SELECT 1 FROM restore_runs WHERE guild=? AND status='running'", (str(interaction.guild.id),)).fetchone():
            return await interaction.followup.send("На сервере уже выполняется восстановление или оно было прервано.", ephemeral=True)
        previous = db.execute("SELECT id,status,created FROM restore_runs WHERE backup=? AND guild=? ORDER BY id DESC LIMIT 1", (backup_id, str(interaction.guild.id))).fetchone()
        last_wipe = db.execute("SELECT status,completed FROM wipe_runs WHERE guild=? ORDER BY id DESC LIMIT 1", (str(interaction.guild.id),)).fetchone()
        if last_wipe and last_wipe[0] != 'complete':
            return await interaction.followup.send("Последняя очистка не завершилась успешно; восстановление заблокировано.", ephemeral=True)
        if previous and (previous[1] == 'running' or not last_wipe or last_wipe[0] != 'complete' or last_wipe[1] <= previous[2]):
            return await interaction.followup.send(f"Восстановление уже запускалось: #{previous[0]}, {previous[1]}. Повтор заблокирован во избежание дубликатов. См. README.", ephemeral=True)
        cursor = db.execute("INSERT INTO restore_runs(backup,owner,guild,created,status) VALUES(?,?,?,?,?)", (backup_id, str(interaction.user.id), str(interaction.guild.id), datetime.now(timezone.utc).isoformat(), "running"))
        run_id = cursor.lastrowid
        db.commit()
        try:
            warnings = await restore_backup(interaction.guild, backup_id, compact=(mode == 'compact'))
            db.execute("UPDATE restore_runs SET status=?, details=? WHERE id=?", ("partial" if warnings else "complete", json.dumps(warnings, ensure_ascii=False), run_id))
            db.commit()
            log.info("Restore #%s completed, warnings: %s", run_id, warnings)
        except BaseException as exc:
            db.execute("UPDATE restore_runs SET status='failed', details=? WHERE id=?", (json.dumps([f"Ошибка: {type(exc).__name__}"], ensure_ascii=False), run_id))
            db.commit()
            log.exception("Ошибка восстановления; изменения могли быть частично применены")
            if isinstance(exc, Exception):
                await interaction.followup.send(f"Восстановление #{run_id} остановлено с ошибкой; изменения могли быть частично применены. Подробности в консоли и /restore_info.", ephemeral=True)
            else:
                raise
            return
        try:
            summary = "\n".join(warnings)[:1400]
            await interaction.followup.send(f"Восстановление #{run_id} завершено. Замечаний: {len(warnings)}.\n{summary}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Interaction expired; use /restore_info for restore #%s", run_id)


@tree.command(name="restore_info", description="Показать историю восстановлений на этом сервере")
async def restore_info(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    rows = db.execute("SELECT id,backup,created,status,details FROM restore_runs WHERE owner=? AND guild=? ORDER BY id DESC LIMIT 10", (str(interaction.user.id), str(interaction.guild.id))).fetchall()
    lines = [f"#{ident} | снимок #{backup} | {created} | {status}\n{details}" for ident, backup, created, status, details in rows]
    await interaction.response.send_message(("\n".join(lines) or "Восстановлений нет.")[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def wipe_test_server(guild, control_channel_id):
    """Delete channels and manageable roles, retaining a channel for further commands."""
    errors = []
    channels = await guild.fetch_channels()
    channels.sort(key=lambda c: isinstance(c, discord.CategoryChannel))
    for channel in channels:
        if channel.id == control_channel_id:
            continue
        try:
            await channel.delete(reason="Test server wipe")
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            errors.append(f"Канал {channel.id}: {type(exc).__name__}")
    roles = await guild.fetch_roles()
    for role in roles:
        if role.is_default() or role.managed:
            continue
        try:
            await role.delete(reason="Test server wipe")
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            errors.append(f"Роль {role.id}: {type(exc).__name__}")
    remaining_channels = await guild.fetch_channels()
    remaining_roles = await guild.fetch_roles()
    if any(channel.id != control_channel_id for channel in remaining_channels):
        errors.append("Не все каналы удалены.")
    if any(not role.is_default() and not role.managed for role in remaining_roles):
        errors.append("Не все обычные роли удалены.")
    return errors


@tree.command(name="wipe_test", description="Очистить целевой тестовый сервер перед повторным восстановлением")
async def wipe_test_command(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    guild_id = str(interaction.guild.id)
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Запускайте команду из обычного текстового канала, который останется для управления.", ephemeral=True)
    if db.execute("SELECT 1 FROM backups WHERE guild=? LIMIT 1", (guild_id,)).fetchone():
        return await interaction.response.send_message("Очистка сервера с локальной резервной копией запрещена: это может быть исходный сервер.", ephemeral=True)
    if not db.execute("SELECT 1 FROM restore_runs WHERE guild=? LIMIT 1", (guild_id,)).fetchone():
        return await interaction.response.send_message("Очистка доступна только серверу, на котором уже запускалось восстановление.", ephemeral=True)
    if lock.locked():
        return await interaction.response.send_message("Другая операция уже выполняется.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with lock:
        if db.execute("SELECT 1 FROM restore_runs WHERE guild=? AND status='running'", (guild_id,)).fetchone():
            return await interaction.followup.send("Восстановление ещё выполняется или было прервано. Очистка заблокирована.", ephemeral=True)
        if db.execute("SELECT 1 FROM wipe_runs WHERE guild=? AND status='running'", (guild_id,)).fetchone():
            return await interaction.followup.send("Предыдущая очистка ещё выполняется или была прервана.", ephemeral=True)
        cursor = db.execute("INSERT INTO wipe_runs(owner,guild,created,status) VALUES(?,?,?,'running')", (str(interaction.user.id), guild_id, datetime.now(timezone.utc).isoformat()))
        wipe_id = cursor.lastrowid
        db.commit()
        try:
            errors = await wipe_test_server(interaction.guild, interaction.channel.id)
            status = 'partial' if errors else 'complete'
            db.execute("UPDATE wipe_runs SET status=?,completed=?,details=? WHERE id=?", (status, datetime.now(timezone.utc).isoformat(), json.dumps(errors, ensure_ascii=False), wipe_id))
            db.commit()
        except BaseException as exc:
            db.execute("UPDATE wipe_runs SET status='failed',details=? WHERE id=?", (json.dumps([type(exc).__name__], ensure_ascii=False), wipe_id))
            db.commit()
            raise
        log.info("Wipe #%s: %s; %s", wipe_id, status, errors)
        try:
            await interaction.followup.send(f"Очистка #{wipe_id}: {status}. Текущий канал и управляемые интеграциями роли сохранены. " + ("Повторное восстановление разрешено." if not errors else "Повторное восстановление заблокировано: " + '; '.join(errors)[:1000]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Wipe response expired; see log for wipe #%s", wipe_id)


@tree.command(name="wipe_info", description="Показать последнюю очистку тестового сервера")
async def wipe_info_command(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Команда доступна только владельцу сервера.", ephemeral=True)
    row = db.execute("SELECT id,created,status,details FROM wipe_runs WHERE guild=? ORDER BY id DESC LIMIT 1", (str(interaction.guild.id),)).fetchone()
    text = f"Очистка #{row[0]} | {row[1]} | {row[2]}\n{row[3]}" if row else "Очисток нет."
    await interaction.response.send_message(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def main():
    logging.basicConfig(level=logging.INFO)
    load_dotenv(BASE / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "вставьте_токен_бота":
        raise SystemExit("Укажите DISCORD_TOKEN в файле .env")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
