from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from lab.blocklist import blocked_reason
from lab.bot_delivery import bot_cfg, deliver_via_bot
from lab.caption import build_caption, caption_parts
from lab.config import env_config
from lab.delivery import decide_delivery, fallback_chain
from lab.enrich import enrich_posts, load_dictionary_rows
from lab.images import download_image, edit_image, fit_telegram, has_valid_urls
from lab.pixiv import (
    adapt_detail_pages,
    adapt_list_item,
    fetch_illust_detail,
    fetch_user_illusts,
    pick_highest,
    refresh_access_token,
)
from lab.rules import get_rule, resolve_rules, select_page
from lab.state import load_posted, load_runtime, save_posted, save_runtime, select_artist
from lab.tags import (
    classify_tags,
    format_trace,
    load_adult_set,
    load_content_map,
    load_drop_set,
    record_tag_stats,
)
from lab.telegram import (
    message_id_of,
    send_item_albums,
    send_item_photo,
    send_photo,
    split_balanced,
)
from lab.title import clean_title
from lab.userbot_zip import (
    delete_storage_messages,
    prepare_zip,
    send_albums_to_storage,
    send_zip_to_storage,
    storage_configured,
    userbot_configured,
    zip_name_base,
)

log = logging.getLogger("waifu_lab")


ENRICH_KEYS = (
    "character",
    "character_tags",
    "franchise_tags",
    "studio_tags",
    "source_tags",
    "general_tags",
    "generalTags",
    "inferred_franchise",
    "inferred_studio",
    "franchiseList",
    "studioList",
    "characterList",
    "all_tags",
)

MAX_FAILURES = 3
DRY_GALLERY_BASE = "https://t.me/littlemask02_bot?start=dry_"  # placeholder gallery links for a dry run
DRY_ZIP_URL = "https://t.me/littlemask02_bot?start=wlart_dry"  # placeholder for the zip caption in a dry run


class ConfigError(RuntimeError):
    """Missing secrets / setup problem. Never counts toward MAX_FAILURES."""


def _enrich_album(pages: list[dict], dict_rows: list[dict]) -> list[dict]:
    if not pages:
        return []
    head = enrich_posts([pages[0]], dict_rows)[0]
    out = []
    for page in pages:
        item = {**page}
        for key in ENRICH_KEYS:
            item[key] = head[key]
        out.append(item)
    return out


def _mark_skipped(posted: dict, runtime: dict, item_id: str, reason: str) -> None:
    posted[item_id] = "skipped"
    runtime.get("failures", {}).pop(item_id, None)
    log.warning("Marked %s as skipped: %s", item_id, reason)


def _record_failure(posted: dict, runtime: dict, item_id: str) -> None:
    failures = runtime.setdefault("failures", {})
    failures[item_id] = int(failures.get(item_id, 0)) + 1
    log.warning("Failure %s/%s for %s", failures[item_id], MAX_FAILURES, item_id)
    if failures[item_id] >= MAX_FAILURES:
        _mark_skipped(posted, runtime, item_id, f"failed {MAX_FAILURES} times")


def _mark_done(posted: dict, runtime: dict, item_id: str, value: str = "") -> None:
    posted[item_id] = value
    runtime.get("failures", {}).pop(item_id, None)


def _hashtag_opts(rules: dict, nsfw: bool) -> dict:
    """rules['hashtags'] -> classify_tags options (per artist, per path)."""
    h = get_rule(rules, "hashtags", default={}) or {}
    pt = h.get("passthrough") or {}
    keys = ("max_category", "fallback", "max_len", "pass_max_len", "pass_max_words")
    opts = {k: h[k] for k in keys if k in h}
    opts["passthrough"] = bool(pt.get("nsfw" if nsfw else "sfw", True))
    limit = pt.get("max_nsfw") if nsfw and "max_nsfw" in pt else pt.get("max")
    if limit is not None:
        opts["passthrough_max"] = limit
    return opts


def _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw):
    """Decide what every Pixiv tag becomes; returns the full classify result."""
    return classify_tags(enriched, dict_rows, content_map, adult_set, drop_set, _hashtag_opts(rules, nsfw))


def _apply_page_rules(pages: list[dict], rules: dict) -> list[dict]:
    """pages.drop_last removes the last page of the post (ads). Single-page posts are untouched."""
    if get_rule(rules, "pages", "drop_last", default=False) and len(pages) > 1:
        return pages[:-1]
    return pages


def _unify_tags(pages: list[dict]) -> None:
    """Merged post: every page carries the union of tags of all source illusts; titles are dropped."""
    tags: list[str] = []
    translations: dict[str, str] = {}
    pixiv_tags: list[dict] = []
    seen: set[str] = set()
    for p in pages:
        for t in p.get("generalTags") or []:
            if t not in tags:
                tags.append(t)
        translations.update(p.get("tagTranslations") or {})
        for pt in p.get("pixivTags") or []:
            if pt["name"] not in seen:
                seen.add(pt["name"])
                pixiv_tags.append(pt)
    raw = list(dict.fromkeys([*tags, *translations.values()]))
    for p in pages:
        p["generalTags"], p["tagsRaw"] = tags, raw
        p["tagTranslations"], p["pixivTags"] = translations, pixiv_tags
        p["title"] = None


# ---- one prepared post (shared by the immediate "post" mode and the nightly "plan" mode) ----

@dataclass
class PreparedPost:
    chosen: dict
    nsfw: bool
    rules: dict
    valid: list
    enriched: list
    classified: dict
    method: str
    why: str
    chain: list
    parts: dict
    dry: bool = False

    @property
    def item_id(self) -> str:
        return self.chosen["id"]

    @property
    def zip_name(self) -> str:
        """Zip / file-name prefix that matches the content (characters + franchise)."""
        art_title = next((it.get("art_title") for it in self.enriched if it.get("art_title")), None)
        return zip_name_base(self.classified, art_title)

    def make_caption(self, m: str, page_url: str | list[str] | None = None) -> str:
        """Caption for exactly one delivery method. Title and teaser are the same for every method.

        page_url: list of storage bot deep links (telegraph = gallery) or one deep link (zip).
        """
        if page_url is None and self.dry:
            if m == "telegraph":
                n = len(split_balanced(self.enriched))
                page_url = [f"{DRY_GALLERY_BASE}{i}" for i in range(1, n + 1)]
            elif m == "zip":
                page_url = DRY_ZIP_URL
        return build_caption(
            self.parts, m, len(self.enriched), self.classified["hashtags"], self.rules, self.classified, page_url,
        )


def prepare_post(
    client, token, chosen, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw: bool, dry: bool = False
) -> PreparedPost | None:
    """Fetch the post details and build everything a delivery needs. None = nothing to send.

    No side effects on Telegram. Marks the item as skipped (not in dry runs) when it has no usable images.
    Errors propagate: the caller decides whether that counts as a failure.
    A merged item (chosen["_merge"]) is several single-page posts sent as one album.
    """
    kind = "NSFW" if nsfw else "SFW"
    item_id = chosen["id"]

    merged = bool(chosen.get("_merge"))
    if merged:
        pages = []
        for comp in chosen["_merge"]:
            detail = fetch_illust_detail(client, token, comp["id01"])
            pages += _apply_page_rules(adapt_detail_pages(detail), rules)
        _unify_tags(pages)
    else:
        detail = fetch_illust_detail(client, token, chosen["id01"])
        pages = _apply_page_rules(adapt_detail_pages(detail), rules)
    valid = [p for p in pages if has_valid_urls(p)]
    if not valid:
        log.warning("%s: no valid image URLs for %s", kind, item_id)
        if not dry:
            ids = [c["id"] for c in chosen["_merge"]] if merged else [item_id]
            for mid in ids:
                _mark_skipped(posted, runtime, mid, "no valid image URLs")
        return None

    enriched = _enrich_album(valid, dict_rows)
    blocked = next((r for it in enriched if (r := blocked_reason(it))), None)
    if blocked:
        log.warning("%s: blocked content %r; skipping", item_id, blocked)
        if not dry:
            ids = [c["id"] for c in chosen["_merge"]] if merged else [item_id]
            for mid in ids:
                _mark_skipped(posted, runtime, mid, f"blocked: {blocked}")
        return None
    art_title = None if merged else clean_title(valid[0].get("title"), adult_set)
    for it in enriched:
        it["art_title"] = art_title
    log.info("Artist title for %s: raw=%r -> caption=%r", item_id, valid[0].get("title"), art_title)
    classified = _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw=nsfw)

    method, why = decide_delivery(chosen, len(valid), nsfw, rules)
    if method == "none":
        log.info("No delivery for %s (%s); skipping", item_id, why)
        if not dry:
            _mark_skipped(posted, runtime, item_id, why)
        return None
    chain = [method, *fallback_chain(method, nsfw, rules)]
    log.info("Delivery for %s: %s (%s); chain=%s", item_id, method, why, chain)

    # title + teaser are picked once per post, so a fallback to another
    # delivery method keeps the same wording and only swaps the CTA / footer
    parts = caption_parts(enriched, nsfw, runtime, classified)

    return PreparedPost(
        chosen=chosen,
        nsfw=nsfw,
        rules=rules,
        valid=valid,
        enriched=enriched,
        classified=classified,
        method=method,
        why=why,
        chain=chain,
        parts=parts,
        dry=dry,
    )


def _prepare_gallery(client, enriched: list[dict], cover_item: dict, rules: dict) -> tuple[list[str], bytes, list[int]]:
    """Upload every page to the storage channel as balanced albums and collect the bot links.

    Pages go in the artist's original order (story order); the cover only decides the channel photo.
    Returns (links, cover photo bytes, storage message ids for rollback).
    Raises ConfigError when the userbot / storage channel is not configured.
    """
    if not userbot_configured():
        raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
    if not storage_configured():
        raise ConfigError("STORAGE_CHAT_ID missing or invalid")

    blobs: list[bytes] = []
    cover_blob: bytes | None = None
    for it in enriched:
        try:
            blob = fit_telegram(download_image(client, it))
        except Exception as exc:
            log.warning("Skipping gallery page %s: %s", it.get("id"), exc)
            continue
        blobs.append(blob)
        if it is cover_item:
            cover_blob = blob
    if not blobs:
        raise RuntimeError("No gallery images could be downloaded")
    if cover_blob is None:
        cover_blob = blobs[0]
    if get_rule(rules, "image_edit", "enabled", default=False):
        cover_blob = fit_telegram(edit_image(cover_blob, rules))

    links, storage_ids = send_albums_to_storage(blobs)
    return links, cover_blob, storage_ids


def _deliver(method: str, client, cfg, chosen, enriched, make_caption, rules, zip_name: str | None = None) -> str:
    """Send one post with one method. Returns the value stored in posted.csv.

    make_caption(method, page_url=None) builds the caption for exactly this method.
    zip_name: content-based prefix for the zip and the files inside it.
    """
    bot_token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    cover_item = select_page(enriched, get_rule(rules, "cover", "page"))

    # settings.json -> bot_delivery: zip / telegraph go through storage + bot links
    b = bot_cfg(method, rules)
    if b:
        if not userbot_configured():
            raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
        if not storage_configured(b["storage_chat_id"]):
            raise ConfigError("STORAGE_CHAT_ID missing or invalid")
        deliver_via_bot(method, client, cfg, chosen, enriched, make_caption, rules, b, zip_name=zip_name)
        return f"bot:{method}"

    if method == "telegraph":
        # "telegraph" = gallery: albums in storage + bot links, cover photo in the channel
        gallery_cover = select_page(enriched, get_rule(rules, "delivery", "telegraph", "cover_page", default="first"))
        spoiler = bool(get_rule(rules, "spoiler", default=False))
        links, cover_blob, storage_ids = _prepare_gallery(client, enriched, gallery_cover, rules)
        try:
            caption = make_caption("telegraph", links)
            payload = send_photo(client, bot_token, chat_id, caption, photo_bytes=cover_blob, has_spoiler=spoiler)
        except Exception:
            delete_storage_messages(storage_ids)
            raise
        return f"gallery:{message_id_of(payload)}"

    if method == "album":
        caption = make_caption("album")
        ordered = [cover_item] + [it for it in enriched if it is not cover_item]
        send_item_albums(client, bot_token, chat_id, ordered, caption)
        return "album"

    if method == "zip":
        if not userbot_configured():
            raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
        if not storage_configured():
            raise ConfigError("STORAGE_CHAT_ID missing or invalid")
        spoiler = bool(get_rule(rules, "spoiler", default=False))
        with tempfile.TemporaryDirectory() as tmp:
            # 1) everything that can fail first: nothing is posted in the channel until the link exists
            zip_path = prepare_zip(client, enriched, chosen["id01"], Path(tmp), rules, name_hint=zip_name)
            link = send_zip_to_storage(zip_path) if zip_path else None
            caption = make_caption("zip", link)

            # 2) cover photo with the bot link in the caption
            if get_rule(rules, "image_edit", "enabled", default=False):
                blob = download_image(client, cover_item)
                blob = edit_image(blob, rules)
                photo_payload = send_photo(
                    client, bot_token, chat_id, caption, photo_bytes=blob, has_spoiler=spoiler
                )
            else:
                photo_payload = send_item_photo(
                    client, bot_token, chat_id, cover_item, caption, has_spoiler=spoiler
                )
            photo_id = message_id_of(photo_payload)
            if zip_path is None:
                log.info("Zip skipped by rules; photo only")
        return f"{'zip' if zip_path else 'photo'}:{photo_id}"

    raise RuntimeError(f"Unknown delivery method {method!r}")


def run_post(client, cfg, token, chosen, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw: bool) -> None:
    kind = "NSFW" if nsfw else "SFW"
    item_id = chosen["id"]
    dry = cfg["dry_run"]
    log.info("%s path: %s (score=%s)", kind, item_id, chosen.get("score"))
    try:
        prep = prepare_post(
            client, token, chosen, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw, dry
        )
        if prep is None:
            return

        if dry:
            log.info(
                "DRY_RUN %s skipped (%s pages, delivery=%s, zip_name=%s)\n%s\n%s",
                kind, len(prep.valid), prep.method, prep.zip_name, prep.make_caption(prep.method), format_trace(prep.classified),
            )
            return

        value = None
        errors: list[Exception] = []
        for m in prep.chain:
            try:
                value = _deliver(
                    m, client, cfg, chosen, prep.enriched, prep.make_caption, rules, zip_name=prep.zip_name
                )
                break
            except Exception as exc:
                errors.append(exc)
                log.warning("Delivery %s failed for %s: %s", m, item_id, exc)
        if value is None:
            real = [e for e in errors if not isinstance(e, ConfigError)]
            raise (real[0] if real else errors[0])

        _mark_done(posted, runtime, item_id, value)
        _persist_stats(cfg, prep.classified, nsfw=nsfw)
    except ConfigError:
        raise
    except Exception:
        if not dry:
            _record_failure(posted, runtime, item_id)
        raise


def _persist_stats(cfg, classified, nsfw: bool) -> None:
    """Count every Pixiv tag of a posted item into State sink or csv."""
    if cfg["dry_run"]:
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].add_stats(classified["decisions"], nsfw)
        return
    if "tag_stats_path" in cfg and cfg["tag_stats_path"]:
        record_tag_stats(cfg["tag_stats_path"], classified["decisions"], nsfw)


def _save_state(cfg, posted, runtime) -> None:
    """Persist state files or send to State sink; a dry run never writes state."""
    if cfg["dry_run"]:
        log.info("DRY_RUN: state not saved")
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].save(posted, runtime)
        return
    if posted is not None and "posted_path" in cfg and cfg["posted_path"]:
        save_posted(cfg["posted_path"], posted)
    if "runtime_path" in cfg and cfg["runtime_path"]:
        save_runtime(cfg["runtime_path"], runtime)


def run(dry_run: bool | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = env_config(dry_run)

    # WAIFU_MODE: plan = schedule the next 24h in the channel (nightly), post = send one round right now.
    # The workflow sets it; running the module by hand without it behaves like "post".
    mode = (os.getenv("WAIFU_MODE") or "post").strip().lower()
    log.info("Mode=%r dry_run=%s", mode, cfg["dry_run"])
    if mode == "plan":
        from lab.plan import run_plan  # imported here: plan.py imports this module

        return run_plan(cfg, cfg["dry_run"])
    if mode != "post":
        log.warning("Unknown WAIFU_MODE %r, using post", mode)

    runtime = load_runtime(cfg["runtime_path"])
    posted = load_posted(cfg["posted_path"])
    artist = select_artist(cfg["artists"], runtime)
    rules = resolve_rules(artist)
    log.info("Selected artist %s (%s); posted=%s", artist.get("name"), artist["user_id"], len(posted))

    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        token = refresh_access_token(client, cfg["pixiv_refresh_token"])
        illusts = fetch_user_illusts(client, token, artist["user_id"], set(posted))
        candidates = [adapt_list_item(i) for i in illusts if i]
        fresh = [c for c in candidates if c.get("id") and c["id"] not in posted]
        if get_rule(rules, "nsfw_as_sfw", default=False):
            for c in fresh:
                c["is_nsfw"] = False
        log.info("Illusts=%s fresh=%s", len(candidates), len(fresh))
        if not fresh:
            log.info("Nothing new to post")
            _save_state(cfg, None, runtime)
            return 0

        dict_rows = load_dictionary_rows(cfg["dictionary_path"])
        content_map = load_content_map(cfg["content_tags_path"])
        adult_set = load_adult_set(cfg["adult_tags_path"])
        drop_set = load_drop_set(cfg["drop_tags_path"])
        nsfw_pick = pick_highest(fresh, nsfw_only=True)
        sfw_pick = pick_highest(fresh, nsfw_only=False)
        errors = []

        def persist() -> None:
            _save_state(cfg, posted, runtime)

        for pick, nsfw in ((nsfw_pick, True), (sfw_pick, False)):
            if not pick:
                continue
            try:
                run_post(client, cfg, token, pick, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw)
            except Exception as exc:
                log.exception("%s path failed", "NSFW" if nsfw else "SFW")
                errors.append(exc)
            finally:
                persist()
        if errors:
            raise errors[0]
        log.info("Done. posted=%s", len(posted))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Waifu Lab pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and caption, do not send")
    args = parser.parse_args(argv)
    try:
        return run(dry_run=args.dry_run)
    except Exception:
        log.exception("Pipeline failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
