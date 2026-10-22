from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from lab.config import _settings
from lab.images import download_image, edit_image, fit_telegram
from lab.rules import deep_merge, get_rule, select_page
from lab.telegram import send_photo
from lab.userbot_schedule import schedule_album
from lab.userbot_zip import (
    delete_storage_messages,
    download_original,
    prepare_zip,
    send_albums_to_storage,
    send_zip_to_storage,
)

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "apply_to": ["zip", "telegraph"],
    "bot_username": "",
    "slug": "",
    "storage_chat_id": "",
    "chunk_size": 10,
    "quality": "master",
}


def bot_cfg(method: str, rules: dict) -> dict | None:
    """Merged bot settings if this method must go through the bot, else None."""
    base = _settings().get("bot_delivery") or {}
    over = get_rule(rules, "bot_delivery", default={}) or {}
    b = deep_merge(DEFAULTS, deep_merge(base, over))
    if not b["enabled"] or method not in b["apply_to"]:
        return None
    missing = [k for k in ("bot_username", "slug", "storage_chat_id") if not b.get(k)]
    if missing:
        raise RuntimeError(f"bot_delivery: missing {missing} in settings.json")
    return b


def deliver_via_bot(
    method, client, cfg, chosen, enriched, make_caption, rules, b, when=None, zip_name: str | None = None
) -> None:
    """Upload to the storage channel, read the bot's deep links, then post/schedule the cover.

    Merges the old bot_delivery path with the gallery path: settings.json decides the
    storage chat, bot, chunk size and quality; the links are the ones the storage bot
    actually replied with. The caption (CTA included) is built by lab.caption.
    """
    chat_id = b["storage_chat_id"]
    bot_username = b["bot_username"]
    storage_ids: list[int] = []

    if method == "zip":
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = prepare_zip(client, enriched, chosen["id01"], Path(tmp), rules, name_hint=zip_name)
            link = send_zip_to_storage(zip_path, chat_id=chat_id, bot_username=bot_username) if zip_path else None
        cover_spec = get_rule(rules, "cover", "page")
        links_out = [link] if link else []
        caption = make_caption("zip", link)
    else:
        cover_spec = get_rule(rules, "delivery", "telegraph", "cover_page", default="first")
        original = str(b.get("quality") or "master").lower() == "original"
        items = []
        for k, it in enumerate(enriched):
            try:
                if original:
                    items.append(download_original(client, it))
                else:
                    items.append((f"page_{k + 1}.jpg", fit_telegram(download_image(client, it))))
            except Exception as exc:
                log.warning("Skipping page %s: %s", it.get("id"), exc)
        if not items:
            raise RuntimeError("bot album: no images downloaded")
        links, storage_ids = send_albums_to_storage(
            items, chat_id=chat_id, bot_username=bot_username, chunk_size=int(b.get("chunk_size") or 10)
        )
        links_out = links
        caption = make_caption("telegraph", links)

    log.info("Bot links (%s): %s", method, links_out)
    cover = select_page(enriched, cover_spec)
    try:
        blob = download_image(client, cover)
        if get_rule(rules, "image_edit", "enabled", default=False):
            blob = edit_image(blob, rules)
        blob = fit_telegram(blob)
        spoiler = bool(get_rule(rules, "spoiler", default=False))
        if when is None:
            send_photo(
                client, cfg["telegram_bot_token"], cfg["telegram_chat_id"], caption,
                photo_bytes=blob, has_spoiler=spoiler,
            )
        else:
            schedule_album(cfg["telegram_chat_id"], [blob], caption, when)
    except Exception:
        delete_storage_messages(storage_ids, chat_id=chat_id)
        raise
