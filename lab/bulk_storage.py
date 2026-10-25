from __future__ import annotations

"""Bulk upload to the storage chat: /bulk_start -> files (no per-album links) -> /bulk_end -> ONE link.

GV delivery: a gallery is no longer a "part i of n" list - every gallery collapses into a
single /bulk_start.../bulk_end deep link, exactly like Waifu Lab's bulk_storage.
Usage: link = bulk_upload(batches)   # batches = list of lists of page paths (or bytes-albums)
"""

import asyncio
import logging
import os
import time

from lab.userbot_zip import _resolve_entity  # telethon entity resolution (+ dialog cache)

log = logging.getLogger(__name__)

SETTLE_SECONDS = 6   # let the bot register the last sent part before /bulk_end
BETWEEN_ALBUMS = 2
BOT_REPLY_TIMEOUT = 45.0
BOT_POLL_INTERVAL = 2.0


def _bot_link_html() -> str:
    """Only the first deep link of the bot is taken (a bulk link is a single URL)."""
    import re

    from lab.userbot_zip import DEFAULT_BOT_USERNAME

    bot = (os.environ.get("STORAGE_BOT_USERNAME") or DEFAULT_BOT_USERNAME).strip().lstrip("@")
    return rf"https?://t\.me/{re.escape(bot)}\?start=[A-Za-z0-9_\-]+"


async def _cmd(client, entity, cmd: str, want_link: bool) -> str:
    import re

    pattern = re.compile(_bot_link_html(), re.I)
    sent = await client.send_message(entity, cmd)
    deadline = time.monotonic() + BOT_REPLY_TIMEOUT
    while time.monotonic() < deadline:
        async for m in client.iter_messages(entity, min_id=sent.id, limit=30):
            if getattr(m, "reply_to_msg_id", None) != sent.id:
                continue
            if not want_link:
                return ""
            link = pattern.search(getattr(m, "message", "") or "")
            if link:
                return link.group(0)
            raise RuntimeError(f"bulk end failed: {(getattr(m, 'message', '') or '')[:120]}")
        await asyncio.sleep(BOT_POLL_INTERVAL)
    raise RuntimeError(f"bot did not answer {cmd}")


async def _bulk_all(batches: list[list], force_document: bool = False) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    chat = os.environ.get("STORAGE_CHAT_ID", "").strip()
    if not chat.lstrip("-").isdigit():
        raise RuntimeError("STORAGE_CHAT_ID missing or invalid")

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, int(chat))
        await _cmd(client, entity, "/bulk_start", False)   # wait until the bot turned bulk ON
        ids: list[int] = []
        try:
            for batch in batches:
                # each item is a (name, bytes) pair (album bytes) or a Path (downloaded file)
                if batch and isinstance(batch[0], (str, os.PathLike)):
                    payload = str(batch[0]) if len(batch) == 1 else [str(p) for p in batch]
                    as_doc = force_document or (len(batch) == 1 and str(batch[0]).lower().endswith(".zip"))
                    sent = await client.send_file(entity, payload, force_document=as_doc)
                else:
                    files = []
                    for name, data in batch:
                        import io

                        bio = io.BytesIO(data)
                        bio.name = str(name or "page.jpg")
                        files.append(bio)
                    sent = await client.send_file(entity, files)
                msgs = sent if isinstance(sent, (list, tuple)) else [sent]
                ids += [int(m.id) for m in msgs if m is not None and getattr(m, "id", None) is not None]
                await asyncio.sleep(BETWEEN_ALBUMS)
            await asyncio.sleep(SETTLE_SECONDS)
            link = await _cmd(client, entity, "/bulk_end", True)
        except Exception:
            try:                                            # never leave bulk mode ON
                await _cmd(client, entity, "/bulk_end", False)
            except Exception as exc:
                log.error("could not close bulk mode: %s", exc)
            if ids:
                try:
                    await client.delete_messages(entity, ids)
                except Exception as exc:
                    log.error("ORPHAN storage messages %s: %s", ids, exc)
            raise
        log.info("Bulk link ready (%s messages)", len(ids))
        return link
    finally:
        await client.disconnect()


def bulk_upload(batches: list[list], force_document: bool = False) -> str:
    if not batches:
        raise ValueError("Nothing to upload")
    return asyncio.run(_bulk_all(batches, force_document))
