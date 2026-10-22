from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from lab.config import (
    PIXIV_API_BASE,
    PIXIV_CLIENT_ID,
    PIXIV_CLIENT_SECRET,
    PIXIV_OAUTH_URL,
    PIXIV_USER_AGENT,
    REQUEST_TIMEOUT,
    MAX_PIXIV_PAGES,
)

log = logging.getLogger(__name__)


def is_nsfw_raw(p: dict) -> bool:
    """R-18 or R-18G, or sanity level 6 (the same rule for the list and the detail)."""
    return int(p.get("x_restrict") or 0) >= 1 or int(p.get("sanity_level") or 0) >= 6


def pixiv_headers(token: str, *, accept_language: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "App-OS": "android",
        "App-OS-Version": "11",
        "User-Agent": PIXIV_USER_AGENT,
    }
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers


def refresh_access_token(client: httpx.Client, refresh_token: str) -> str:
    files = {
        "client_id": (None, PIXIV_CLIENT_ID),
        "client_secret": (None, PIXIV_CLIENT_SECRET),
        "grant_type": (None, "refresh_token"),
        "refresh_token": (None, refresh_token),
    }
    r = client.post(
        PIXIV_OAUTH_URL,
        files=files,
        headers={"User-Agent": PIXIV_USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("Pixiv did not return access_token")
    log.info("Pixiv token refreshed")
    return token


def fetch_user_illusts(
    client: httpx.Client,
    token: str,
    user_id: str,
    posted_ids: set[str] | None = None,
    *,
    max_pages: int | None = None,
    early_break: bool = True,
    max_illusts: int | None = None,
) -> list[dict]:
    """Fetch a user's illusts.

    The pipeline stops early once it has seen both NSFW and SFW content (to pick
    one of each). The harvest script sets early_break=False and max_illusts to
    collect a fixed recent window instead.
    """
    url = f"{PIXIV_API_BASE}/user/illusts"
    params: dict[str, Any] | None = {"user_id": user_id, "type": "illust"}
    illusts: list[dict] = []
    posted_ids = posted_ids or set()
    have_nsfw = have_sfw = False
    page_limit = max_pages if max_pages is not None else MAX_PIXIV_PAGES
    for page in range(page_limit):
        r = client.get(url, params=params, headers=pixiv_headers(token), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        batch = data.get("illusts") or []
        illusts.extend(batch)
        for raw in batch:
            item = adapt_list_item(raw)
            if not item.get("id") or item["id"] in posted_ids:
                continue
            if item.get("is_nsfw"):
                have_nsfw = True
            else:
                have_sfw = True
        next_url = data.get("next_url")
        log.info("Pixiv page %s: +%s illusts (next=%s)", page + 1, len(batch), bool(next_url))
        if max_illusts is not None and len(illusts) >= max_illusts:
            break
        if not next_url:
            break
        if early_break and page >= 1 and have_nsfw and have_sfw:
            break
        url = next_url
        params = None
    if max_illusts is not None:
        illusts = illusts[:max_illusts]
    return illusts


def fetch_illust_detail(client: httpx.Client, token: str, illust_id: str | int) -> dict:
    r = client.get(
        f"{PIXIV_API_BASE}/illust/detail",
        params={"illust_id": illust_id},
        headers=pixiv_headers(token, accept_language="en-US"),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    illust = r.json().get("illust")
    if not illust:
        raise RuntimeError(f"No illust detail for {illust_id}")
    return illust


def adapt_list_item(p: dict) -> dict:
    caption_text = (p.get("caption") or "").replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    caption_lines = [ln.strip() for ln in caption_text.split("\n") if ln.strip()]
    character_list: list[str] = []
    for line in caption_lines:
        if "/" in line and not re.search(r"https?://", line):
            candidates = [s.strip() for s in line.split("/") if s.strip()]
            if candidates and all(0 < len(c) <= 20 for c in candidates):
                character_list = candidates
                break

    all_tags = [t.get("name") for t in (p.get("tags") or []) if t and t.get("name")]
    general_tags = [t for t in all_tags if t != "R-18" and t not in character_list]
    franchise_tag = next((t for t in all_tags if t != "R-18" and t not in character_list), None)

    pages = p.get("meta_pages") if isinstance(p.get("meta_pages"), list) and p.get("meta_pages") else None
    if pages:
        hq_url = (pages[0].get("image_urls") or {}).get("original")
    elif (p.get("meta_single_page") or {}).get("original_image_url"):
        hq_url = p["meta_single_page"]["original_image_url"]
    else:
        hq_url = (p.get("image_urls") or {}).get("large") or (p.get("image_urls") or {}).get("medium")

    image_urls = p.get("image_urls") or {}
    std_url = image_urls.get("large") or image_urls.get("medium") or hq_url

    return {
        "id": f"pixiv:{p['id']}" if p.get("id") is not None else None,
        "id01": p.get("id"),
        "create_date": p.get("create_date"),
        "image_url_hq": hq_url,
        "image_url_std": std_url,
        "is_nsfw": is_nsfw_raw(p),
        "x_restrict": int(p.get("x_restrict") or 0),
        "title": p.get("title") or "",
        "tag_pairs": [(t["name"], str(t.get("translated_name") or "").strip()) for t in (p.get("tags") or []) if t and t.get("name")],
        "user_id": str((p.get("user") or {}).get("id") or ""),
        "score": p.get("total_bookmarks") or 0,
        "total_view": p.get("total_view") or 0,
        "character": character_list[0] if character_list else "unknown",
        "characterList": character_list,
        "generalTags": general_tags,
        "tagsRaw": all_tags,
        "source": "pixiv",
        "byte_size": None,
        "author": (p.get("user") or {}).get("account") or (p.get("user") or {}).get("name") or "unknown",
        "authorList": [(p.get("user") or {}).get("account")] if (p.get("user") or {}).get("account") else [],
        "branch": franchise_tag,
        "category": p.get("type"),
        "width": p.get("width"),
        "height": p.get("height"),
        "page_count": int(p.get("page_count") or 1),
    }


def adapt_detail_pages(illust: dict) -> list[dict]:
    raw_tags = illust.get("tags") if isinstance(illust.get("tags"), list) else []
    tags = [t["name"] for t in raw_tags if t.get("name")]
    tag_translations = {}
    for t in raw_tags:
        en = str(t.get("translated_name") or "").strip()
        if t.get("name") and en and en != t["name"]:
            tag_translations[t["name"]] = en
    tags_raw = list(dict.fromkeys([*tags, *tag_translations.values()]))
    pixiv_tags = [
        {"name": t["name"], "en": str(t.get("translated_name") or "").strip()}
        for t in raw_tags
        if t.get("name")
    ]
    is_nsfw = is_nsfw_raw(illust)

    if illust.get("meta_pages"):
        pages = illust["meta_pages"]
    else:
        pages = [{"image_urls": illust.get("image_urls") or {}}]

    items = []
    for idx, page in enumerate(pages):
        urls = page.get("image_urls") or {}
        items.append(
            {
                "id": f"{illust['id']}_p{idx}",
                "illust_id": illust["id"],
                "title": illust.get("title"),
                "image_url_hq": urls.get("original") or urls.get("large"),
                "image_url_std": urls.get("medium"),
                "character": "unknown",
                "generalTags": tags,
                "tagsRaw": tags_raw,
                "tagTranslations": tag_translations,
                "pixivTags": pixiv_tags,
                "source": "pixiv",
                "branch": "pixiv",
                "category": "illust",
                "is_nsfw": is_nsfw,
                "x_restrict": int(illust.get("x_restrict") or 0),
                "slotIndex": idx,
            }
        )
    return items


def pick_highest(items: list[dict], *, nsfw_only: bool | None = None) -> dict | None:
    pool = items
    if nsfw_only is True:
        pool = [it for it in items if it.get("is_nsfw") is True]
    elif nsfw_only is False:
        pool = [
            it
            for it in items
            if it.get("is_nsfw") is not True and str(it.get("is_nsfw")).lower() != "true"
        ]
    if not pool:
        return None

    def views(it: dict) -> int:
        return int(it.get("total_view") or it.get("score") or 0)

    return max(pool, key=views)
