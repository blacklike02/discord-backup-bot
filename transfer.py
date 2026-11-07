"""Helpers for bounded, ordered-per-channel transfers."""

import asyncio

import discord


def split_text(text, limit=1900):
    """Split on Unicode character boundaries, limiting UTF-16 code units."""
    if limit < 2:
        raise ValueError('limit must be at least 2')
    if len(text) <= limit // 2 or (len(text) <= limit and text.isascii()):
        if text:
            yield text
        return
    start = units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield text[start:index]
            start, units = index, 0
        units += width
    if start < len(text):
        yield text[start:]


def message_batches(messages, *, compact=False, limit=1900):
    """Yield text and media in source order without crossing media boundaries."""
    pending = ''
    pending_units = 0
    for message in messages:
        content = message.get('system_content') or message['content']
        for sticker in message.get('stickers', []):
            name = discord.utils.escape_markdown(sticker['name'])
            content += f"\n[Стикер: {name}] {sticker['url']}"
        text = f"**{discord.utils.escape_markdown(message['author'])}** · {message['created']}\n{content}"
        media = bool(message.get('embeds') or message.get('attachments'))
        text_units = (len(text) if text.isascii() else len(text.encode('utf-16-le')) // 2) if compact else 0
        if not compact or media or text_units > limit:
            if pending:
                yield 'text', pending
                pending, pending_units = '', 0
            if compact and message.get('attachments') and not message.get('embeds') and text_units <= limit:
                yield 'bundle', (text, message)
                continue
            for chunk in split_text(text, limit):
                yield 'text', chunk
            if media:
                yield 'media', message
            continue
        separator = '\n\n' if pending else ''
        if pending_units + len(separator) + text_units > limit:
            yield 'text', pending
            pending, pending_units, separator = '', 0, ''
        pending += separator + text
        pending_units += len(separator) + text_units
    if pending:
        yield 'text', pending


async def run_bounded(items, worker, *, concurrency=4):
    """Run independent work concurrently; cancel and join workers on failure."""
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    iterator = iter(items)

    async def consume():
        for item in iterator:
            await worker(item)

    tasks = [asyncio.create_task(consume()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
