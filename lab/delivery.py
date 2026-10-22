from __future__ import annotations

import logging

from lab.rules import allowed_methods, get_rule, zip_allowed

log = logging.getLogger(__name__)

MODES = {"auto", "zip", "album", "telegraph"}
METHODS = ("telegraph", "zip", "album")
NONE = "none"   # the post fits no delivery method of this artist -> it is never picked


def _allowed(rules: dict) -> list[str]:
    return allowed_methods(rules)


def sfw_gallery_from(rules: dict, allowed: list[str]) -> int | None:
    """Page count from which a SFW post goes to the bot gallery ("telegraph"), or None = never.

    - needs "telegraph" in the artist's allowed methods
    - delivery.sfw_telegraph_min_pages (default 10); null/0 switches it off for this artist
    - an artist with an explicit delivery.album_max keeps albums up to that size,
      the gallery starts right above it (instead of the post being dropped)
    """
    if "telegraph" not in allowed:
        return None
    cfg = get_rule(rules, "delivery", "sfw_telegraph_min_pages", default=None)
    if not cfg:
        return None
    explicit = get_rule(rules, "delivery", "album_max", default=None)
    if explicit:
        return int(explicit) + 1
    return int(cfg)


def decide_delivery(chosen: dict, page_count: int, nsfw: bool, rules: dict) -> tuple[str, str]:
    """Returns (telegraph | zip | album | none, short reason for the log).

    SFW  -> album up to the limit, above it the bot gallery (telegraph) - also for mode=album artists
    NSFW -> zip (>= nsfw_zip_min_pages) | Telegraph (min..max pages) | none
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

    if mode == "zip":
        if not nsfw:
            mode = "album"                       # zip is NSFW-only
        elif not zip_allowed(rules):
            # a forced zip cannot override the artist's allowed list / zip.enabled
            log.warning("delivery.mode=zip ignored: zip is not allowed for this artist; using auto")
            mode = "auto"

    if not nsfw and mode in ("auto", "album"):
        gal = sfw_gallery_from(rules, allowed)
        if gal and n >= gal:
            return "telegraph", f"sfw pages {n} >= {gal} (bot gallery)"

    if mode != "auto":
        if mode == "album":
            # mode=album is explicit: every post of this artist is an album, NSFW included
            if n > album_max:
                return NONE, f"{n} pages > album_max {album_max}"
        return mode, f"forced by rules (mode={mode})"

    if not nsfw:
        if "album" not in allowed:
            return NONE, "album not allowed for this artist"
        if n > album_max:
            return NONE, f"{n} pages > album_max {album_max}"
        return "album", "sfw is always album"

    zip_min = int(get_rule(rules, "delivery", "nsfw_zip_min_pages", default=21))
    tg_min = int(get_rule(rules, "delivery", "telegraph", "min_pages", default=10))
    tg_max = int(get_rule(rules, "delivery", "telegraph", "max_pages", default=20))

    if not zip_allowed(rules):
        # no zip for this artist: Telegraph from min_pages_over_album pages, otherwise album when allowed
        over = int(get_rule(rules, "delivery", "telegraph", "min_pages_over_album", default=tg_min))
        if "telegraph" in allowed and n >= over:
            return "telegraph", f"nsfw, zip not allowed, pages {n} >= {over}"
        if "album" in allowed and nsfw_album and n <= album_max:
            return "album", f"nsfw, zip not allowed, pages {n}"
        return NONE, f"nsfw, zip not allowed, {n} pages too few for Telegraph"

    if n >= zip_min:
        return "zip", f"nsfw pages {n} >= {zip_min}"
    if "telegraph" in allowed and tg_min <= n <= tg_max:
        return "telegraph", f"nsfw pages {n} in {tg_min}-{tg_max}"
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
