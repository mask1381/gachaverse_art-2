from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from lab.images import IMG_EXT, PXIMG_HEADERS
from lab.rules import get_rule, select_pages, zip_allowed
from lab.telegram import ALBUM_MAX, split_balanced

log = logging.getLogger(__name__)

FALLBACK_ZIP_NAME = "WaifuLab"
MAX_NAME_LEN = 60
MAX_ZIP_BYTES = int(1.9 * 1024**3)  # Telethon limit is 2 GB for normal accounts
DOWNLOAD_TIMEOUT = 90.0
DOWNLOAD_ATTEMPTS = 2
UPLOAD_ATTEMPTS = 3

# storage channel + delivery bot
DEFAULT_BOT_USERNAME = "littlemask02_bot"
BOT_REPLY_TIMEOUT = 45.0   # the bot usually answers in ~5s
BOT_POLL_INTERVAL = 2.0
STORAGE_ATTEMPTS = 2
ALBUM_GAP = 2.0            # seconds between albums of one post


def userbot_configured() -> bool:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION_STRING", "").strip()
    return bool(api_id.isdigit() and api_hash and session)


def _settings_bot_delivery() -> dict:
    """settings.json -> bot_delivery (storage chat + bot username live there)."""
    try:
        from lab.config import _settings

        return _settings().get("bot_delivery") or {}
    except Exception:
        return {}


def _storage_value(chat_id: str | None = None) -> str:
    """Explicit chat_id > STORAGE_CHAT_ID env > settings.json bot_delivery.storage_chat_id."""
    if chat_id is not None and str(chat_id).strip():
        return str(chat_id).strip()
    env = os.environ.get("STORAGE_CHAT_ID", "").strip()
    if env:
        return env
    return str(_settings_bot_delivery().get("storage_chat_id") or "").strip()


def storage_configured(chat_id: str | None = None) -> bool:
    """True when a storage chat is configured (settings.json first, env as fallback)."""
    return _storage_value(chat_id).lstrip("-").isdigit()


def _storage_chat_id(chat_id: str | None = None) -> int:
    value = _storage_value(chat_id)
    if not value.lstrip("-").isdigit():
        raise RuntimeError("STORAGE_CHAT_ID missing or invalid")
    return int(value)


# ---- zip naming ----

def _camel(text) -> str:
    """'Final Fantasy X' -> 'FinalFantasyX' (ASCII only)."""
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", ascii_text)
    return "".join(w[:1].upper() + w[1:] for w in words)


def zip_name_base(info: dict | None, art_title: str | None = None) -> str:
    """Name that matches the content: characters (max 2) + franchise.

    No character -> the artist's Latin title first. Nothing at all -> first theme, then 'WaifuLab'.
    """
    info = info or {}
    chars = [c for c in (_camel(x) for x in (info.get("characters") or [])[:2]) if c]
    parts: list[str] = []
    if chars:
        parts.append("_".join(chars))
    else:
        title = _camel(art_title)
        if title:
            parts.append(title)
    franchise = _camel(info.get("franchise"))
    if franchise:
        parts.append(franchise)
    if not parts:
        theme = next((t for t in (_camel(x) for x in info.get("themes") or []) if t), "")
        if theme:
            parts.append(theme)
    name = "_".join(parts)[:MAX_NAME_LEN].strip("_")
    return name or FALLBACK_ZIP_NAME


# ---- zip building ----

def _original_urls(page: dict) -> list[str]:
    """Original-quality URLs only (no master1200 conversion), direct host first, proxy as fallback."""
    out: list[str] = []
    for raw in (page.get("image_url_hq"), page.get("image_url_std")):
        raw = str(raw or "")
        if not raw.startswith("https://"):
            continue
        direct = raw.replace("i.pixiv.re", "i.pximg.net")
        proxy = direct.replace("i.pximg.net", "i.pixiv.re")
        for url in (direct, proxy):
            if url not in out:
                out.append(url)
    return out


def download_original(client: httpx.Client, page: dict) -> tuple[str, bytes]:
    last_error: Exception | None = None
    for url in _original_urls(page):
        headers = PXIMG_HEADERS if "i.pximg.net" in url else None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                r = client.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if not r.content or not ctype.startswith("image/"):
                    raise RuntimeError(f"unexpected response ({ctype or 'no content-type'}, {len(r.content)} bytes)")
                name = Path(urlparse(url).path).name
                if not IMG_EXT.search(name):
                    name = f"{page.get('id') or 'image'}.jpg"
                log.info("Original downloaded %s via %s (%s bytes)", name, url.split("/")[2], len(r.content))
                return name, r.content
            except Exception as exc:
                last_error = exc
                log.warning("Original fetch failed (%s, try %s/%s): %s", url, attempt, DOWNLOAD_ATTEMPTS, exc)
                if attempt < DOWNLOAD_ATTEMPTS:
                    time.sleep(1.5 * attempt)
    raise RuntimeError(f"Could not download original for page {page.get('id')}") from last_error


def prepare_zip(
    client: httpx.Client,
    pages: list[dict],
    illust_id: str,
    workdir: Path,
    rules: dict | None = None,
    name_hint: str | None = None,
) -> Path | None:
    """Download pages at original quality into one uncompressed zip. All pages or nothing.

    Files keep the artist's page order (story order) and are numbered with zero padding:
    Lulu_FinalFantasyX_01.png, ..._02.png ... The cover never changes the order inside the zip.
    The zip is named <name_hint>_<illust_id>.zip.

    Refuses to build anything unless lab.rules.zip_allowed(rules) is true:
      - delivery.allowed must contain "zip"
      - zip.enabled must not be False
    zip.pages ('all' | 1-based int list) picks which pages to include.
    """
    rules = rules or {}
    if not zip_allowed(rules):
        log.info("Zip not allowed by rules (delivery.allowed has no 'zip' or zip.enabled=false); skipping zip")
        return None

    chosen_pages = select_pages(pages, get_rule(rules, "zip", "pages", default="all"))
    base = re.sub(r"[^A-Za-z0-9_]", "", name_hint or "") or FALLBACK_ZIP_NAME
    width = max(2, len(str(len(chosen_pages))))

    tmp_path = workdir / "building.zip"
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for index, page in enumerate(chosen_pages, start=1):
            name, blob = download_original(client, page)
            ext = Path(name).suffix.lower() or ".jpg"
            zf.writestr(f"{base}_{index:0{width}d}{ext}", blob)

    final_path = workdir / f"{base}_{illust_id}.zip"
    tmp_path.rename(final_path)

    size = final_path.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise RuntimeError(f"Zip too large for upload: {size} bytes")
    log.info("Zip ready: %s (%s files, %.1f MB)", final_path.name, len(chosen_pages), size / 1024**2)
    return final_path


# ---- telethon helpers ----

async def _resolve_entity(client, chat_id: int):
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        # fresh session has no cached channels; load dialogs once, then retry
        await client.get_dialogs()
        return await client.get_entity(chat_id)


def _new_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )


async def _upload(zip_path: Path, chat_id: str, reply_to: int | None) -> int:
    client = _new_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, int(chat_id))

        last_pct = {"v": -25}

        def progress(sent: int, total: int) -> None:
            pct = int(sent * 100 / total) if total else 100
            if pct - last_pct["v"] >= 25:
                last_pct["v"] = pct
                log.info("Zip upload %s%%", pct)

        msg = await client.send_file(
            entity,
            str(zip_path),
            force_document=True,
            reply_to=reply_to,
            progress_callback=progress,
        )
        return int(msg.id)
    finally:
        await client.disconnect()


def upload_zip_with_retry(zip_path: Path, chat_id: str, reply_to: int | None) -> int:
    last_error: Exception | None = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            message_id = asyncio.run(_upload(zip_path, chat_id, reply_to))
            log.info("Zip sent (message %s)", message_id)
            return message_id
        except Exception as exc:
            last_error = exc
            log.warning("Zip upload failed (try %s/%s): %s", attempt, UPLOAD_ATTEMPTS, exc)
            if attempt < UPLOAD_ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Zip upload failed after {UPLOAD_ATTEMPTS} attempts") from last_error


# ---- storage channel: userbot uploads, the bot replies with a deep link ----

def _bot_link_pattern(bot_username: str | None = None) -> re.Pattern:
    bot = (
        bot_username
        or os.environ.get("STORAGE_BOT_USERNAME")
        or _settings_bot_delivery().get("bot_username")
        or DEFAULT_BOT_USERNAME
    ).strip().lstrip("@")
    return re.compile(rf"https?://t\.me/{re.escape(bot)}\?start=[A-Za-z0-9_\-]+", re.I)


def _message_haystack(m) -> str:
    """Message text plus every link entity (the bot may hide the link behind text)."""
    parts = [getattr(m, "raw_text", None) or getattr(m, "message", "") or ""]
    for ent in getattr(m, "entities", None) or []:
        url = getattr(ent, "url", None)
        if url:
            parts.append(url)
    return " ".join(parts)


async def _wait_for_bot_reply(client, entity, sent_ids, pattern: re.Pattern) -> tuple[str, int] | None:
    """Wait for the bot's reply to any of the given messages. Returns (link, reply message id)."""
    ids = {int(sent_ids)} if isinstance(sent_ids, int) else {int(i) for i in sent_ids}
    floor = min(ids)
    deadline = time.monotonic() + BOT_REPLY_TIMEOUT
    while time.monotonic() < deadline:
        async for m in client.iter_messages(entity, min_id=floor, limit=40):
            if getattr(m, "reply_to_msg_id", None) not in ids:
                continue
            found = pattern.search(_message_haystack(m))
            if found:
                return found.group(0), int(m.id)
        await asyncio.sleep(BOT_POLL_INTERVAL)
    return None


async def _wait_for_bot_link(client, entity, sent_id: int, pattern: re.Pattern) -> str | None:
    res = await _wait_for_bot_reply(client, entity, sent_id, pattern)
    return res[0] if res else None


async def _to_storage(zip_path: Path, chat_id: str | None = None, bot_username: str | None = None) -> tuple[str, int]:
    client = _new_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, _storage_chat_id(chat_id))

        last_pct = {"v": -25}

        def progress(sent: int, total: int) -> None:
            pct = int(sent * 100 / total) if total else 100
            if pct - last_pct["v"] >= 25:
                last_pct["v"] = pct
                log.info("Storage upload %s%%", pct)

        msg = await client.send_file(entity, str(zip_path), force_document=True, progress_callback=progress)
        link = await _wait_for_bot_link(client, entity, int(msg.id), _bot_link_pattern(bot_username))
        if not link:
            try:
                await client.delete_messages(entity, [msg.id])
            except Exception as exc:
                log.error("ORPHAN storage message %s could not be deleted: %s", msg.id, exc)
            raise RuntimeError(f"Storage bot did not reply with a link within {BOT_REPLY_TIMEOUT:.0f}s")
        log.info("Storage link ready: %s (message %s)", link, msg.id)
        return link, int(msg.id)
    finally:
        await client.disconnect()


def send_zip_to_storage_with_id(
    zip_path: Path, *, chat_id: str | None = None, bot_username: str | None = None
) -> tuple[str, int]:
    """Upload the zip to storage; returns (deep link, storage message id) so it can be rolled back."""
    last_error: Exception | None = None
    for attempt in range(1, STORAGE_ATTEMPTS + 1):
        try:
            return asyncio.run(_to_storage(zip_path, chat_id=chat_id, bot_username=bot_username))
        except Exception as exc:
            last_error = exc
            log.warning("Storage upload failed (try %s/%s): %s", attempt, STORAGE_ATTEMPTS, exc)
            if attempt < STORAGE_ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Storage upload failed after {STORAGE_ATTEMPTS} attempts") from last_error


def send_zip_to_storage(
    zip_path: Path, *, chat_id: str | None = None, bot_username: str | None = None
) -> str:
    """Upload the zip to the storage channel as the userbot and return the bot's deep link."""
    link, _ = send_zip_to_storage_with_id(zip_path, chat_id=chat_id, bot_username=bot_username)
    return link


# ---- storage channel: photo albums (the "Get the album" links) ----

async def _delete_in_storage(client, entity, ids: list[int]) -> None:
    """Best-effort removal of storage messages. Never raises."""
    if not ids:
        return
    try:
        await client.delete_messages(entity, list(ids))
        log.info("Removed storage messages %s", ids)
    except Exception as exc:
        log.error("ORPHAN storage messages %s could not be deleted: %s", ids, exc)


def _album_item(item, index: int) -> tuple[str, bytes]:
    """bytes -> a page name; (name, bytes) keeps the original filename."""
    if isinstance(item, (bytes, bytearray)):
        return f"page_{index + 1}.jpg", bytes(item)
    name, data = item
    return str(name or f"page_{index + 1}.jpg"), bytes(data)


async def _to_storage_albums(
    items: list, chat_id: str | None = None, bot_username: str | None = None, chunk_size: int | None = None
) -> tuple[list[str], list[int]]:
    client = _new_client()
    await client.connect()
    stored: list[int] = []  # every message we caused in storage (albums + bot replies), for rollback
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, _storage_chat_id(chat_id))
        pattern = _bot_link_pattern(bot_username)
        chunks = split_balanced(list(range(len(items))), int(chunk_size or ALBUM_MAX))
        links: list[str] = []
        try:
            for part, idxs in enumerate(chunks, start=1):
                if part > 1:
                    await asyncio.sleep(ALBUM_GAP)
                files = []
                for k in idxs:
                    name, data = _album_item(items[k], k)
                    bio = io.BytesIO(data)
                    bio.name = name
                    files.append(bio)
                res = await client.send_file(entity, files)
                msgs = res if isinstance(res, (list, tuple)) else [res]
                ids = [int(m.id) for m in msgs if m is not None and getattr(m, "id", None) is not None]
                if not ids:
                    raise RuntimeError("Storage upload returned no messages")
                stored.extend(ids)
                found = await _wait_for_bot_reply(client, entity, ids, pattern)
                if not found:
                    raise RuntimeError(
                        f"Storage bot did not reply with a link for album part {part}/{len(chunks)} "
                        f"within {BOT_REPLY_TIMEOUT:.0f}s"
                    )
                link, reply_id = found
                stored.append(reply_id)
                links.append(link)
                log.info("Album part %s/%s ready: %s (%s photos)", part, len(chunks), link, len(idxs))
        except Exception:
            await _delete_in_storage(client, entity, stored)
            raise
        return links, stored
    finally:
        await client.disconnect()


def send_albums_to_storage(
    items: list,
    *,
    chat_id: str | None = None,
    bot_username: str | None = None,
    chunk_size: int | None = None,
) -> tuple[list[str], list[int]]:
    """Upload photos to the storage channel as balanced albums (chunk_size each, order kept).

    items are bytes or (name, bytes) pairs, so original files keep their filename.
    The bot answers each album with one deep link. Returns (links, storage message ids).
    All or nothing: on failure everything sent for this call is removed again.
    """
    if not items:
        raise RuntimeError("No images to upload to storage")
    last_error: Exception | None = None
    for attempt in range(1, STORAGE_ATTEMPTS + 1):
        try:
            return asyncio.run(
                _to_storage_albums(items, chat_id=chat_id, bot_username=bot_username, chunk_size=chunk_size)
            )
        except Exception as exc:
            last_error = exc
            log.warning("Storage album upload failed (try %s/%s): %s", attempt, STORAGE_ATTEMPTS, exc)
            if attempt < STORAGE_ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Storage album upload failed after {STORAGE_ATTEMPTS} attempts") from last_error


async def _delete_storage(ids: list[int], chat_id: str | None = None) -> None:
    client = _new_client()
    await client.connect()
    try:
        entity = await _resolve_entity(client, _storage_chat_id(chat_id))
        await _delete_in_storage(client, entity, ids)
    finally:
        await client.disconnect()


def delete_storage_messages(ids: list[int], chat_id: str | None = None) -> None:
    """Roll back storage messages when the channel post could not be created. Never raises."""
    if not ids:
        return
    try:
        asyncio.run(_delete_storage(ids, chat_id=chat_id))
    except Exception as exc:
        log.error("ORPHAN storage messages %s could not be deleted: %s", ids, exc)