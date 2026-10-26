from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab.telegram import split_balanced
from lab.userbot_zip import _resolve_entity

log = logging.getLogger(__name__)

CHUNK_GAP = 60   # seconds between albums of one post
ZIP_GAP = 120    # seconds between the cover photo and its zip (photo first, zip 2 min later)
ZIP_CAPTION = "📦 Original quality"
ATTEMPTS = 2


def _utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError("schedule time must be timezone-aware")
    return when.astimezone(timezone.utc)


def _ids(res) -> list[int]:
    items = res if isinstance(res, (list, tuple)) else [res]
    return [int(m.id) for m in items if m is not None and getattr(m, "id", None) is not None]


def _update_ids(res) -> list[int]:
    out: list[int] = []
    for u in getattr(res, "updates", None) or []:
        m = getattr(u, "message", None)
        if m is not None and getattr(m, "id", None) is not None:
            out.append(int(m.id))
    return out


@asynccontextmanager
async def _session(chat_id: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, int(chat_id))
        yield client, entity
    finally:
        await client.disconnect()


async def _delete(client, entity, ids: list[int]) -> None:
    """Best-effort removal of scheduled messages (rollback). Never raises."""
    from telethon.tl import functions

    if not ids:
        return
    try:
        await client(functions.messages.DeleteScheduledMessagesRequest(peer=entity, id=list(ids)))
        log.info("Rolled back scheduled messages %s", ids)
    except Exception as exc:
        log.error("ORPHAN scheduled messages %s could not be deleted: %s", ids, exc)


def _retry(factory, what: str):
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return asyncio.run(factory())
        except Exception as exc:
            last = exc
            text = str(exc)
            if "SCHEDULE_TOO_MUCH" in text or "SCHEDULE_DATE" in text:
                raise
            log.warning("%s failed (try %s/%s): %s", what, attempt, ATTEMPTS, exc)
            if attempt < ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{what} failed after {ATTEMPTS} attempts") from last


# ---- list ----

async def _list(chat_id: str) -> list[dict]:
    from telethon.tl import functions

    async with _session(chat_id) as (client, entity):
        res = await client(functions.messages.GetScheduledHistoryRequest(peer=entity, hash=0))
        out: list[dict] = []
        for m in getattr(res, "messages", None) or []:
            date = getattr(m, "date", None)
            if date is None:
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            out.append({"id": int(m.id), "date": date})
        return out


def list_scheduled(chat_id: str) -> list[dict]:
    """Messages currently waiting in the channel's scheduled queue: [{"id", "date" (UTC, aware)}]."""
    return asyncio.run(_list(chat_id))


# ---- album ----

async def _album(chat_id, blobs, caption, when, gap=CHUNK_GAP) -> list[int]:
    async with _session(chat_id) as (client, entity):
        ids: list[int] = []
        try:
            for i, idxs in enumerate(split_balanced(list(range(len(blobs))))):
                files = []
                for k in idxs:
                    bio = io.BytesIO(blobs[k])
                    bio.name = f"page_{k + 1}.jpg"
                    files.append(bio)
                at = _utc(when + timedelta(seconds=gap * i))
                if len(files) == 1:
                    res = await client.send_file(entity, files[0], caption=caption, parse_mode="html", schedule=at)
                else:
                    caps = [caption] + [""] * (len(files) - 1)
                    res = await client.send_file(entity, files, caption=caps, parse_mode="html", schedule=at)
                ids.extend(_ids(res))
        except Exception:
            await _delete(client, entity, ids)
            raise
        log.info("Album scheduled for %s (%s messages)", when.strftime("%m-%d %H:%M"), len(ids))
        return ids


def schedule_album(chat_id, blobs, caption, when, gap=CHUNK_GAP) -> list[int]:
    return _retry(lambda: _album(chat_id, blobs, caption, when, gap=gap), "Scheduling album")


# ---- immediate (non-scheduled) channel sends ----
# The target channel has no bot of its own, so every post reaches it through the
# userbot (TG_SESSION_STRING), never through a bot token.

async def _send_now(client, entity, bio, caption: str, spoiler: bool = False) -> int:
    res = await client.send_file(entity, bio, caption=caption, parse_mode="html", spoiler=bool(spoiler))
    ids = _ids(res)
    if not ids:
        raise RuntimeError("No message id returned for channel photo")
    return ids[0]


def _immediate(chat_id, blobs, caption, spoiler: bool = False):
    async def _run():
        async with _session(chat_id) as (client, entity):
            if len(blobs) == 1:
                bio = io.BytesIO(blobs[0])
                bio.name = "cover.jpg"
                return [_send_now(client, entity, bio, caption, spoiler=spoiler)]
            ids: list[int] = []
            try:
                for i, idxs in enumerate(split_balanced(list(range(len(blobs))))):
                    if i:
                        time.sleep(CHUNK_GAP)
                    files = []
                    for k in idxs:
                        bio = io.BytesIO(blobs[k])
                        bio.name = f"page_{k + 1}.jpg"
                        files.append(bio)
                    if len(files) == 1:
                        res = await client.send_file(entity, files[0], caption=caption, parse_mode="html",
                                                     spoiler=bool(spoiler))
                    else:
                        caps = [caption] + [""] * (len(files) - 1)
                        res = await client.send_file(entity, files, caption=caps, parse_mode="html",
                                                     spoiler=bool(spoiler))
                    ids.extend(_ids(res))
                if not ids:
                    raise RuntimeError("Album send returned no messages")
            except Exception:
                try:
                    await client.delete_messages(entity, list(ids))
                except Exception as exc:
                    log.error("ORPHAN sent messages %s could not be deleted: %s", ids, exc)
                raise
            return ids
    return asyncio.run(_run())


def send_photo_now(chat_id, blob: bytes, caption: str, spoiler: bool = False) -> list[int]:
    """Immediately post one cover photo to the target channel as the userbot."""
    return _retry(lambda: _immediate(chat_id, [blob], caption, spoiler=spoiler), "Sending cover photo")


def send_album_now(chat_id, blobs: list[bytes], caption: str, spoiler: bool = False) -> list[int]:
    """Immediately post an album to the target channel as the userbot."""
    return _retry(lambda: _immediate(chat_id, blobs, caption, spoiler=spoiler), "Sending album")


# ---- plain text, no photo and no link preview ----

async def _plain_text(chat_id: str, caption: str, when: datetime) -> list[int]:
    async with _session(chat_id) as (client, entity):
        res = await client.send_message(
            entity, caption, parse_mode="html", link_preview=False, schedule=_utc(when)
        )
        log.info("Plain text scheduled for %s", when.strftime("%m-%d %H:%M"))
        return _ids(res)


def schedule_plain_text(chat_id: str, caption: str, when: datetime) -> list[int]:
    """Scheduled text-only message (no photo, link preview off), used for a gallery without a cover."""
    return _retry(lambda: _plain_text(chat_id, caption, when), "Scheduling plain text")


# ---- photo + zip (two scheduled messages, zip two minutes after the photo) ----

async def _zip_post(chat_id: str, photo_blob: bytes | None, zip_path: Path | None, caption: str, when: datetime) -> list[int]:
    async with _session(chat_id) as (client, entity):
        zip_ids: list[int] = []
        if photo_blob is None:
            # no cover photo: the zip alone at `when`, the full caption sits on the zip
            if zip_path is None:
                raise RuntimeError("zip without cover needs a zip file")
            res = await client.send_file(entity, str(zip_path), force_document=True, caption=caption,
                                         parse_mode="html", schedule=_utc(when))
            ids = _ids(res)
            log.info("Zip (no cover) scheduled for %s", when.strftime("%m-%d %H:%M"))
            return ids
        if zip_path is not None:
            last = {"v": -25}

            def progress(sent: int, total: int) -> None:
                pct = int(sent * 100 / total) if total else 100
                if pct - last["v"] >= 25:
                    last["v"] = pct
                    log.info("Zip upload %s%%", pct)

            res = await client.send_file(
                entity,
                str(zip_path),
                force_document=True,
                caption=ZIP_CAPTION,
                schedule=_utc(when + timedelta(seconds=ZIP_GAP)),
                progress_callback=progress,
            )
            zip_ids = _ids(res)
        try:
            bio = io.BytesIO(photo_blob)
            bio.name = "cover.jpg"
            res = await client.send_file(entity, bio, caption=caption, parse_mode="html", schedule=_utc(when))
            photo_ids = _ids(res)
        except Exception:
            await _delete(client, entity, zip_ids)
            raise
        log.info("Photo%s scheduled for %s", " + zip" if zip_ids else "", when.strftime("%m-%d %H:%M"))
        return photo_ids + zip_ids


def schedule_zip_post(chat_id: str, photo_blob: bytes | None, zip_path: Path | None, caption: str, when: datetime) -> list[int]:
    """Cover photo with caption at `when`, the zip as a separate message two minutes later. Both or neither."""
    return _retry(lambda: _zip_post(chat_id, photo_blob, zip_path, caption, when), "Scheduling zip post")


# ---- text with a Telegraph link preview ----

async def _text(chat_id: str, caption: str, page_url: str, when: datetime) -> list[int]:
    """The caption already contains the Telegraph link (built by caption.py) as the first link of the message."""
    from telethon.extensions import html as tl_html
    from telethon.tl import functions, types

    text = caption
    async with _session(chat_id) as (client, entity):
        try:
            message, entities = tl_html.parse(text)
            media = types.InputMediaWebPage(url=page_url, force_large_media=True, optional=True)
            res = await client(
                functions.messages.SendMediaRequest(
                    peer=entity,
                    media=media,
                    message=message,
                    entities=entities,
                    random_id=int.from_bytes(os.urandom(8), "big", signed=True),
                    schedule_date=_utc(when),
                )
            )
            log.info("Telegraph text scheduled for %s (explicit large link preview)", when.strftime("%m-%d %H:%M"))
            return _update_ids(res)
        except Exception as exc:
            log.warning("Explicit link preview failed (%s); falling back to plain scheduled text", exc)
        res = await client.send_message(entity, text, parse_mode="html", link_preview=True, schedule=_utc(when))
        log.info("Telegraph text scheduled for %s (plain link preview)", when.strftime("%m-%d %H:%M"))
        return _ids(res)


def schedule_text(chat_id: str, caption: str, page_url: str, when: datetime) -> list[int]:
    """Scheduled text message whose link preview is the Telegraph page (Instant View)."""
    return _retry(lambda: _text(chat_id, caption, page_url, when), "Scheduling Telegraph text")
