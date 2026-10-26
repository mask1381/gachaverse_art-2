from __future__ import annotations

import logging

from lab.rules import allowed_methods, get_rule, zip_allowed

log = logging.getLogger(__name__)

MODES = {"auto", "zip", "album", "telegraph"}
METHODS = ("telegraph", "zip", "album")
NONE = "none"   # the post fits no delivery method of this artist -> it is never picked


def _allowed(rules: dict) -> list[str]:
    return allowed_methods(rules)


def _law(rules: dict) -> dict:
    """The channel law (لایحه) with the shared bot template; {} when absent."""
    return get_rule(rules, "law", default={}) or {}


def _law_int(rules: dict, *path: str, old_key: str = "", default: int = 0) -> int:
    """Read one law number from the shared template; falls back to the legacy gachaverse key."""
    law = _law(rules)
    v = get_rule(law, *path, default=None)
    if v is None and old_key:
        v = get_rule(law, old_key, default=None)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def sfw_gallery_from(rules: dict, allowed: list[str]) -> int | None:
    """Page count from which a SFW post goes to the bot gallery ("telegraph"), or None = never.

    - needs "telegraph" in the artist's allowed methods
    - the channel law (law.split.full_to_channel) is the default; an explicit artist
      delivery.sfw_telegraph_min_pages overrides it; the legacy law.sfw_telegraph_min_pages
      is honoured too
    - null/0 switches it off for this artist
    - an artist with an explicit delivery.album_max keeps albums up to that size,
      the gallery starts right above it (instead of the post being dropped)
    """
    if "telegraph" not in allowed:
        return None
    law_cfg = _law_int(rules, "split", "full_to_channel", old_key="sfw_telegraph_min_pages", default=10)
    cfg = get_rule(rules, "delivery", "sfw_telegraph_min_pages", default=law_cfg)
    if not cfg:
        return None
    explicit = get_rule(rules, "delivery", "album_max", default=None)
    if explicit:
        return int(explicit) + 1
    return int(cfg)


def sfw_zip_from(rules: dict) -> int | None:
    """Page count from which a SFW post becomes a zip (law.zip.sfw_after), None = zip is off for SFW."""
    a = _law_int(rules, "zip", "sfw_after", default=0)
    return None if a <= 0 else a


def nsfw_channel_mode(rules: dict) -> str:
    """cover_only | single_to_channel | all_to_channel — the heart of the NSFW law."""
    law = _law(rules)
    mode = get_rule(law, "nsfw", "channel_mode", default=None)
    if mode is None:  # legacy gachaverse key
        mode = "all_to_channel" if str(get_rule(law, "nsfw_channel", default="cover")) == "full" else "cover_only"
    return str(mode or "cover_only")


def decide_delivery(chosen: dict, page_count: int, nsfw: bool, rules: dict) -> tuple[str, str]:
    """Returns (telegraph | zip | album | none, short reason for the log).

    Channel law (لایحه, shared template) drives the defaults; the artist's own
    delivery.* keys always override them:
      - law.split.full_to_channel        -> SFW goes to the bot gallery ("telegraph") at/above it
      - law.zip.sfw_after (default 0)    -> SFW becomes a zip at/above it (0 = off)
      - law.zip.nsfw_after (default 5)   -> NSFW becomes a zip at/above it
      - law.nsfw.channel_mode            -> cover_only (default) | all_to_channel (full NSFW album)
    Nothing is forced: a post that fits no slot is simply not used.
    """
    n = int(page_count or 1)
    mode = get_rule(rules, "delivery", "mode", default="auto")
    if mode not in MODES:
        log.warning("Unknown delivery.mode %r, using auto", mode)
        mode = "auto"
    allowed = _allowed(rules)
    album_max = int(get_rule(rules, "delivery", "album_max", default=9))
    nsfw_album = bool(get_rule(rules, "delivery", "nsfw_album", default=False))
    chain_mode = nsfw_channel_mode(rules)

    # artist force: zip is NSFW-only; a forced zip needs zip in the allowed list
    if mode == "zip" and not zip_allowed(rules):
        log.warning("delivery.mode=zip ignored: zip is not allowed for this artist; using auto")
        mode = "auto"

    if not nsfw:
        # 1) bot gallery for big SFW posts (law.split.full_to_channel / legacy)
        gal = sfw_gallery_from(rules, allowed)
        if gal and n >= gal:
            return "telegraph", f"sfw pages {n} >= {gal} (bot gallery)"
        # 2) SFW zip when the law turns it on (law.zip.sfw_after > 0)
        sfw_zip = sfw_zip_from(rules)
        if sfw_zip is not None and n >= sfw_zip and "zip" in allowed:
            return "zip", f"sfw pages {n} >= sfw_after {sfw_zip}"
        # 3) album in the channel, unless a method is forced
        if mode == "zip":
            mode = "album"  # original behaviour: forced zip still means SFW albums
        if mode in ("auto", "album", "telegraph"):
            if "album" not in allowed and mode != "telegraph":
                return NONE, "album not allowed for this artist"
            if mode == "telegraph":
                return "telegraph", "forced by rules (mode=telegraph)"
            if n > album_max:
                return NONE, f"{n} pages > album_max {album_max}"
            return "album", "sfw is always album" if mode == "auto" else "forced by rules (mode=album)"
        return mode, f"forced by rules (mode={mode})"

    # ---- NSFW ----
    # artist delivery.nsfw_zip_min_pages first, then the shared law.zip.nsfw_after (legacy law.nsfw_zip_min_pages)
    zip_min = int(get_rule(rules, "delivery", "nsfw_zip_min_pages",
                           default=_law_int(rules, "zip", "nsfw_after", old_key="nsfw_zip_min_pages", default=5)))
    tg_min = int(get_rule(rules, "delivery", "telegraph", "min_pages", default=10))
    tg_max = int(get_rule(rules, "delivery", "telegraph", "max_pages", default=20))

    if mode != "auto":
        if mode == "album":
            # mode=album is explicit: every post of this artist is an album, NSFW included
            if n > album_max:
                return NONE, f"{n} pages > album_max {album_max}"
            return "album", "forced by rules (mode=album)"
        if mode == "telegraph":
            return "telegraph", "forced by rules (mode=telegraph)"
        # mode == "zip" (and zip allowed): NSFW always zip
        return "zip", "forced by rules (mode=zip) and zip allowed"

    if not zip_allowed(rules):
        # no zip for this artist: Telegraph from min_pages_over_album pages, otherwise album when allowed
        over = int(get_rule(rules, "delivery", "telegraph", "min_pages_over_album", default=tg_min))
        if "telegraph" in allowed and n >= over:
            return "telegraph", f"nsfw, zip not allowed, pages {n} >= {over}"
        if chain_mode == "all_to_channel" and "album" in allowed and n <= album_max:
            return "album", f"nsfw all_to_channel, pages {n}"
        if "album" in allowed and nsfw_album and n <= album_max:
            return "album", f"nsfw, zip not allowed, pages {n}"
        return NONE, f"nsfw, zip not allowed, {n} pages too few for Telegraph"

    if n >= zip_min:
        return "zip", f"nsfw pages {n} >= {zip_min}"
    if "telegraph" in allowed and tg_min <= n <= tg_max:
        return "telegraph", f"nsfw pages {n} in {tg_min}-{tg_max}"
    if chain_mode == "all_to_channel" and "album" in allowed and n <= album_max:
        return "album", f"nsfw all_to_channel, pages {n}"
    return NONE, f"nsfw pages {n} fits neither Telegraph ({tg_min}-{tg_max}) nor zip (>= {zip_min})"


def fallback_chain(method: str, nsfw: bool, rules: dict) -> list[str]:
    """Methods to try, in order, after `method` fails. Zip is NSFW-only; album is SFW-only unless allowed."""
    table = get_rule(rules, "delivery", "fallback", default={}) or {}
    if not isinstance(table, dict):
        return []
    allowed = _allowed(rules)
    nsfw_album = bool(get_rule(rules, "delivery", "nsfw_album", default=False))
    out: list[str] = []
    for m in table.get(method) or []:
        if m == "zip" and (not nsfw or not zip_allowed(rules)):
            continue
        if m == "album" and nsfw and not nsfw_album:
            continue
        if m in allowed and m != method and m not in out:
            out.append(m)
    return out
