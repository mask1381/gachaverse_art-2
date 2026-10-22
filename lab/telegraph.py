from __future__ import annotations

import json
import logging

import httpx

from lab.config import REQUEST_TIMEOUT, TELEGRAPH_API_BASE
from lab.images import to_proxy_master

log = logging.getLogger(__name__)

DEFAULT_HOST = "i.pixiv.re"


def _image_src(item: dict, host: str) -> str:
    src = to_proxy_master(item.get("image_url_hq") or "") or item.get("image_url_std") or ""
    if host and host != DEFAULT_HOST:
        src = src.replace(f"https://{DEFAULT_HOST}/", f"https://{host}/", 1)
    return src


def build_telegraph_content(items: list[dict], image_host: str = DEFAULT_HOST) -> dict:
    """Page content in the order given (the caller puts the cover first)."""
    pages = list(items)
    content = [{"tag": "img", "attrs": {"src": _image_src(it, image_host)}} for it in pages]
    first = pages[0].get("character") if pages else None
    if first and first != "unknown":
        first_character = " ".join(w[:1].upper() + w[1:] for w in first.split("_") if w)
    else:
        first_character = None
    return {
        "content": content,
        "firstCharacter": first_character,
        "albumCount": len(pages),
        "itemIds": [it.get("id") for it in pages],
    }


def create_page(client: httpx.Client, token: str, payload: dict) -> str:
    r = client.post(
        f"{TELEGRAPH_API_BASE}/createPage",
        data={
            "access_token": token,
            "title": f"{payload.get('firstCharacter') or 'GachaVerse'} Gallery",
            "author_name": "GachaVerse Art",
            "content": json.dumps(payload["content"], ensure_ascii=False),
            "return_content": "false",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph error: {data}")
    url = data["result"]["url"]
    log.info("Telegraph page: %s", url)
    return url
