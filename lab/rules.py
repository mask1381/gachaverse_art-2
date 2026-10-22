from __future__ import annotations

import copy
from typing import Any

# Baseline. An artist entry in sources.json overrides only the keys it sets (deep merge).
DEFAULT_RULES: dict[str, Any] = {
    "nsfw_as_sfw": False,  # True = this artist's R-18 art counts as safe (album, SFW caption)
    "hashtags": {
        "max_category": 4,
        "format_tag": True,  # #ZIP / #Gallery
        "passthrough": {"sfw": True, "nsfw": True, "max": 2, "max_nsfw": 1},
    },
    # drop_last: drop the last page of every item (ads)
    # merge_singles: {"min":4,"max":10,"max_wait_days":3}
    "pages": {"drop_last": False, "merge_singles": None},
    "schedule": {"preferred_slots": None},  # ["12:30", "00:30"] = only in these slots
    "spoiler": False,  # spoiler on the zip / gallery cover photo
    "delivery": {
        "mode": "auto",  # auto | zip | album | telegraph  (forced for every post)
        "allowed": ["telegraph", "zip", "album"],  # methods the auto logic may pick
        # SFW with this many pages or more -> telegraph (bot gallery). Applies to every artist, also mode=album.
        # An artist with an explicit album_max keeps albums up to that size, the gallery starts above it.
        # null = off for this artist (needs "telegraph" in allowed).
        "sfw_telegraph_min_pages": 10,
        "nsfw_zip_min_pages": 5,  # NSFW: pages >= this -> zip, fewer -> telegraph
        # "telegraph" now means GALLERY: albums in the storage channel + bot links, cover photo in the channel
        "telegraph": {
            "min_pages_over_album": 11,  # only when zip is not allowed: >= this -> telegraph, else album
            "cover_page": "first",  # which page is the channel photo (first | last | 1-based int)
            "no_cover": False,  # NSFW gallery as a plain text post (no photo), link preview off
            "cover_from_sfw": False,  # NSFW gallery borrows the pages of a SFW post of the same artist as its cover
        },
        # what to try when a method fails (zip only for NSFW and only when zip is allowed)
        "fallback": {"telegraph": ["zip", "album"], "zip": [], "album": []},
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (dicts merge, other values replace)."""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def resolve_rules(artist: dict) -> dict:
    """Effective rules = DEFAULT_RULES deep-merged with artist['rules']."""
    return deep_merge(DEFAULT_RULES, artist.get("rules") or {})


def get_rule(rules: dict, *path: str, default: Any = None) -> Any:
    """Safe nested access: get_rule(rules, 'image_edit', 'max_edge', default=None)."""
    cur: Any = rules
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


METHODS = ("telegraph", "zip", "album")


def allowed_methods(rules: dict) -> list[str]:
    """delivery.allowed, filtered to known methods (default: all three)."""
    allowed = get_rule(rules, "delivery", "allowed", default=list(METHODS)) or []
    return [m for m in allowed if m in METHODS]


def zip_allowed(rules: dict) -> bool:
    """True only when this artist may ship a zip.

    Both gates must pass: "zip" is in delivery.allowed and zip.enabled is not false.
    Every zip-producing code path checks this, so a gallery/telegraph post can never
    accidentally build a zip for an artist that opted out of zips.
    """
    if "zip" not in allowed_methods(rules):
        return False
    return get_rule(rules, "zip", "enabled", default=True) is not False


def _page_index(spec: Any, n: int) -> int | None:
    """Resolve a page spec ('first' | 'last' | 1-based int) to a 0-based index."""
    if n <= 0:
        return None
    if spec == "last":
        return n - 1
    if spec in ("first", None):
        return 0
    if isinstance(spec, int):
        idx = spec - 1
        return idx if 0 <= idx < n else None
    if isinstance(spec, str) and str(spec).strip().isdigit():
        idx = int(spec) - 1
        return idx if 0 <= idx < n else None
    return 0


def select_page(items: list, spec: Any) -> Any:
    """Pick one item by page spec. Falls back to the first item."""
    if not items:
        return None
    idx = _page_index(spec, len(items))
    if idx is None:
        idx = 0
    return items[idx]


def select_pages(items: list, spec: Any) -> list:
    """Select/order pages for the zip: 'all' keeps everything; an int list picks those pages."""
    if not items:
        return []
    if spec == "all" or spec is None:
        return list(items)
    if isinstance(spec, int):
        spec = [spec]
    if isinstance(spec, (list, tuple)):
        out = []
        for s in spec:
            idx = _page_index(s, len(items))
            if idx is not None and items[idx] not in out:
                out.append(items[idx])
        return out
    return list(items)
