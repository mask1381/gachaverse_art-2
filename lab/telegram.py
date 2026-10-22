from __future__ import annotations

import json
import logging
import time

import httpx

from lab.config import REQUEST_TIMEOUT, TELEGRAM_API_BASE
from lab.images import download_image, fit_telegram, media_url

log = logging.getLogger(__name__)


def _url(token: str, method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


def _clean_caption(caption: str | None) -> str:
    """Never let a missing caption crash sending or error reporting."""
    return (caption or "").strip()


def _raise_with_detail(r: httpx.Response, method: str, extra: str = "") -> None:
    """Log Telegram's own error description before raising (raise_for_status alone hides it)."""
    if r.is_success:
        return
    log.error("Telegram %s HTTP %s %s | %s", method, r.status_code, extra, r.text[:500])
    r.raise_for_status()


def send_photo(
    client: httpx.Client,
    token: str,
    chat_id: str,
    caption: str,
    *,
    photo_url: str | None = None,
    photo_bytes: bytes | None = None,
    has_spoiler: bool = False,
    button_url: str | None = None,
) -> dict:
    caption = _clean_caption(caption)
    data = {
        "chat_id": chat_id,
        "has_spoiler": "true" if has_spoiler else "false",
    }
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    if button_url:
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "♡ ♡ Hen.ART ♡ ♡", "url": button_url}]]}
        )
    files = None
    if photo_bytes is not None:
        files = {"photo": ("photo.jpg", fit_telegram(photo_bytes), "image/jpeg")}
    else:
        data["photo"] = photo_url
    r = client.post(_url(token, "sendPhoto"), data=data, files=files, timeout=REQUEST_TIMEOUT)
    _raise_with_detail(r, "sendPhoto", f"chat_id={chat_id} caption_len={len(caption)}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {payload}")
    log.info("Telegram photo sent")
    return payload


def send_media_group(
    client: httpx.Client,
    token: str,
    chat_id: str,
    caption: str,
    *,
    image_urls: list[str] | None = None,
    images: list[bytes] | None = None,
) -> dict:
    caption = _clean_caption(caption)
    urls = image_urls or []
    blobs = images or []
    if len(blobs) == 1:
        return send_photo(client, token, chat_id, caption, photo_bytes=blobs[0])
    if len(urls) == 1 and not blobs:
        return send_photo(client, token, chat_id, caption, photo_url=urls[0])

    media = []
    files = None
    if blobs:
        files = {}
        for idx, blob in enumerate(blobs):
            field = "data" if idx == 0 else f"data_{idx}"
            media.append(
                {
                    "type": "photo",
                    "media": f"attach://{field}",
                    **({"caption": caption, "parse_mode": "HTML"} if idx == 0 and caption else {}),
                }
            )
            files[field] = (f"{field}.jpg", fit_telegram(blob), "image/jpeg")
    else:
        for idx, src in enumerate(urls):
            media.append(
                {
                    "type": "photo",
                    "media": src,
                    **({"caption": caption, "parse_mode": "HTML"} if idx == 0 and caption else {}),
                }
            )

    data = {"chat_id": chat_id, "media": json.dumps(media)}
    r = client.post(_url(token, "sendMediaGroup"), data=data, files=files, timeout=60.0)
    _raise_with_detail(r, "sendMediaGroup", f"chat_id={chat_id} caption_len={len(caption)} items={len(media)}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMediaGroup failed: {payload}")
    log.info("Telegram album sent (%s photos)", len(blobs) or len(urls))
    return payload


def send_item_photo(client, token, chat_id, item: dict, caption: str, **kwargs) -> dict:
    url = media_url(item)
    try:
        return send_photo(client, token, chat_id, caption, photo_url=url, **kwargs)
    except Exception as exc:
        log.warning("URL send failed, downloading: %s", exc)
    blob = download_image(client, item)
    return send_photo(client, token, chat_id, caption, photo_bytes=blob, **kwargs)


def send_item_album(client, token, chat_id, items: list[dict], caption: str) -> dict:
    urls = [media_url(it) for it in items]
    try:
        return send_media_group(client, token, chat_id, caption, image_urls=urls)
    except Exception as exc:
        log.warning("URL album failed, downloading: %s", exc)

    blobs: list[bytes] = []
    for it in items:
        try:
            blobs.append(download_image(client, it))
        except Exception as exc:
            log.warning("Skipping album page %s: %s", it.get("id"), exc)
    if not blobs:
        raise RuntimeError("No album images could be downloaded")
    return send_media_group(client, token, chat_id, caption, images=blobs)


def message_id_of(payload: dict) -> int:
    result = payload["result"]
    if isinstance(result, list):
        result = result[0]
    return int(result["message_id"])


def delete_message(client: httpx.Client, token: str, chat_id: str, message_id: int) -> bool:
    """Best-effort delete (used to roll back a photo when the zip could not be sent). Never raises."""
    try:
        r = client.post(
            _url(token, "deleteMessage"),
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=REQUEST_TIMEOUT,
        )
        ok = bool(r.json().get("ok"))
    except Exception as exc:
        log.error("deleteMessage failed for %s: %s", message_id, exc)
        return False
    if ok:
        log.info("Rolled back photo message %s", message_id)
    else:
        log.error("deleteMessage refused for %s: %s", message_id, r.text)
    return ok


# ---- multi-album sending (SFW posts with more than 10 pages) ----

ALBUM_MAX = 10          # Telegram limit per sendMediaGroup
ALBUM_DELAY = 1.5       # seconds between albums
RATE_LIMIT_RETRIES = 3


def split_balanced(items: list, max_size: int = ALBUM_MAX) -> list[list]:
    """Split into ceil(n/max_size) near-equal chunks, order preserved (11 -> 6+5, 23 -> 8+8+7)."""
    n = len(items)
    if n <= max_size:
        return [items]
    parts = -(-n // max_size)
    base, extra = divmod(n, parts)
    out, i = [], 0
    for j in range(parts):
        size = base + (1 if j < extra else 0)
        out.append(items[i : i + size])
        i += size
    return out


def message_ids_of(payload: dict) -> list[int]:
    result = payload["result"]
    if isinstance(result, dict):
        result = [result]
    return [int(m["message_id"]) for m in result]


def _send_album_with_rate_limit(client, token, chat_id, chunk, caption) -> dict:
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            return send_item_album(client, token, chat_id, chunk, caption)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                raise
            try:
                wait = int(exc.response.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            wait = min(wait + 1, 120)
            log.warning("Telegram 429, sleeping %ss (try %s/%s)", wait, attempt, RATE_LIMIT_RETRIES)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def send_item_albums(client, token, chat_id, items: list[dict], caption: str) -> list[int]:
    """Send all pages as one or more balanced albums, same caption on the first photo of each.

    All or nothing: if any album fails, the albums already sent for this post are deleted
    and the error is raised. Returns the message ids that were sent.
    """
    chunks = split_balanced(items)
    log.info("Sending %s pages as %s album(s): %s", len(items), len(chunks), [len(c) for c in chunks])
    sent: list[int] = []
    try:
        for idx, chunk in enumerate(chunks):
            if idx:
                time.sleep(ALBUM_DELAY)
            payload = _send_album_with_rate_limit(client, token, chat_id, chunk, caption)
            sent.extend(message_ids_of(payload))
    except Exception:
        if sent:
            log.error("Album %s/%s failed, rolling back %s message(s)", idx + 1, len(chunks), len(sent))
            for mid in sent:
                if not delete_message(client, token, chat_id, mid):
                    log.error("ORPHAN album message left in channel: %s", mid)
        raise
    return sent

# ---- Telegraph delivery (Instant View inside Telegram) ----

TELEGRAPH_SEND_AS = {"preview_message", "caption_link", "button"}


def send_text_with_preview(
    client: httpx.Client,
    token: str,
    chat_id: str,
    text: str,
    page_url: str,
    *,
    prefer_large_media: bool = True,
    show_above_text: bool = False,
) -> dict:
    """Text message whose link preview is the Telegraph page (Instant View button)."""
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps(
            {
                "is_disabled": False,
                "url": page_url,
                "prefer_large_media": prefer_large_media,
                "show_above_text": show_above_text,
            }
        ),
    }
    r = client.post(_url(token, "sendMessage"), data=data, timeout=REQUEST_TIMEOUT)
    _raise_with_detail(r, "sendMessage", f"chat_id={chat_id} text_len={len(text)}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")
    log.info("Telegram preview message sent")
    return payload


def send_telegraph_post(
    client: httpx.Client,
    token: str,
    chat_id: str,
    caption: str,
    page_url: str,
    send_as: str,
    cover_item: dict | None = None,
) -> int:
    """Send one Telegraph post. Returns the message id.

    The caption already contains the "Tap to view" link (built by lab/caption.py).

    send_as:
      preview_message - text + link preview (Instant View button)
      caption_link    - cover photo, link inside the caption, no button
      button          - cover photo with a URL button (old behavior)
    """
    if send_as not in TELEGRAPH_SEND_AS:
        log.warning("Unknown telegraph.send_as %r, using preview_message", send_as)
        send_as = "preview_message"

    if send_as == "preview_message":
        payload = send_text_with_preview(client, token, chat_id, caption, page_url)
    elif cover_item is None:
        raise RuntimeError(f"send_as={send_as} needs a cover item")
    elif send_as == "caption_link":
        payload = send_item_photo(client, token, chat_id, cover_item, caption)
    else:  # button
        payload = send_item_photo(client, token, chat_id, cover_item, caption, button_url=page_url)
    return message_id_of(payload)
