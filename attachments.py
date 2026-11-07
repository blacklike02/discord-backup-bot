"""Send attachments in bounded groups without reordering source messages."""

import discord
from pathlib import Path, PureWindowsPath


def attachment_path(directory, name):
    """Resolve an archived attachment without escaping its backup directory."""
    if not isinstance(name, str) or not name or Path(name).name != name or PureWindowsPath(name).name != name:
        raise ValueError('invalid attachment path')
    base = Path(directory).resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise ValueError('attachment path escapes backup directory')
    return path


async def send_bundle(target, text, attachments, directory, size_limit, warnings):
    eligible = []
    for item in attachments:
        try:
            path = attachment_path(directory, item['path'])
            size = path.stat().st_size
        except (OSError, ValueError, KeyError) as exc:
            warnings.append(f"Вложение {item['name']}: {type(exc).__name__}")
            continue
        if size > size_limit:
            warnings.append(f"Вложение {item['name']} превышает лимит нового сервера")
            continue
        eligible.append((item, path, size))

    groups = []
    group = []
    group_size = 0
    for entry in eligible:
        if group and (len(group) >= 10 or group_size + entry[2] > size_limit):
            groups.append(group)
            group, group_size = [], 0
        group.append(entry)
        group_size += entry[2]
    if group:
        groups.append(group)
    if not groups:
        await target.send(text, allowed_mentions=discord.AllowedMentions.none())
        return

    for index, group in enumerate(groups):
        content = text if index == 0 else None
        files = []
        valid = []
        try:
            for item, path, _ in group:
                try:
                    files.append(discord.File(path, filename=item['name']))
                    valid.append((item, path))
                except OSError as exc:
                    warnings.append(f"Вложение {item['name']}: {type(exc).__name__}")
            if not files:
                if content is not None:
                    await target.send(content, allowed_mentions=discord.AllowedMentions.none())
                continue
            try:
                await target.send(content, files=files, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                if exc.status not in (400, 413):
                    raise
                # A rejected request cannot have created a message. Retry each file
                # separately, so a bad file does not discard the others.
                for file in files:
                    file.close()
                if content is not None:
                    await target.send(content, allowed_mentions=discord.AllowedMentions.none())
                for item, path in valid:
                    try:
                        file = discord.File(path, filename=item['name'])
                        try:
                            await target.send(file=file, allowed_mentions=discord.AllowedMentions.none())
                        finally:
                            file.close()
                    except (discord.HTTPException, OSError) as file_error:
                        warnings.append(f"Вложение {item['name']}: {type(file_error).__name__}")
        finally:
            for file in files:
                file.close()
