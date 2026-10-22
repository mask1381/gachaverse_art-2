from __future__ import annotations

import io
import logging
import re

import httpx

from lab.config import JPEG_QUALITY, MAX_IMAGE_EDGE, PIXIV_USER_AGENT, REQUEST_TIMEOUT, TELEGRAM_MAX_BYTES

log = logging.getLogger(__name__)
IMG_EXT = re.compile(r"\.(jpg|jpeg|png|webp)$", re.I)
PXIMG_HEADERS = {
    "Referer": "https://www.pixiv.net/",
    "User-Agent": PIXIV_USER_AGENT,
}


def has_valid_urls(item: dict) -> bool:
    hq = str(item.get("image_url_hq") or "")
    std = str(item.get("image_url_std") or "")
    return (
        hq.startswith("https://")
        and std.startswith("https://")
        and bool(IMG_EXT.search(hq))
        and bool(IMG_EXT.search(std))
    )


def to_proxy_master(url: str) -> str:
    u = url or ""
    if "/img-original/" in u:
        u = u.replace("/img-original/", "/img-master/")
        u = re.sub(r"\.(png|jpg|jpeg)$", "_master1200.jpg", u, flags=re.I)
    return u.replace("i.pximg.net", "i.pixiv.re")


def media_url(item: dict) -> str:
    return to_proxy_master(item.get("image_url_hq") or item.get("image_url_std") or "")


def _candidate_urls(item: dict) -> list[str]:
    raws = [item.get("image_url_hq") or "", item.get("image_url_std") or ""]
    out: list[str] = []
    for raw in raws:
        if not str(raw).startswith("https://"):
            continue
        proxy = to_proxy_master(raw)
        pximg_master = proxy.replace("i.pixiv.re", "i.pximg.net")
        pximg = str(raw).replace("i.pixiv.re", "i.pximg.net")
        for url in (pximg_master, pximg, proxy, raw):
            if url and url not in out:
                out.append(url)
    return out


def download_image(client: httpx.Client, item: dict) -> bytes:
    last_error: Exception | None = None
    for url in _candidate_urls(item):
        headers = PXIMG_HEADERS if "i.pximg.net" in url else None
        try:
            r = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            r.raise_for_status()
            if r.content:
                log.info("Downloaded image via %s (%s bytes)", url.split("/")[2], len(r.content))
                return r.content
        except Exception as exc:
            last_error = exc
            log.warning("Image fetch failed (%s): %s", url, exc)
    raise RuntimeError(f"Could not download image {item.get('id')}") from last_error


def fit_telegram(data: bytes) -> bytes:
    """Re-encode only when Telegram would reject the file. master1200 is already small."""
    if len(data) <= TELEGRAM_MAX_BYTES:
        return data
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    log.info("Compressed oversized image %s -> %s bytes", len(data), out.tell())
    return out.getvalue()


def edit_image(data: bytes, rules: dict) -> bytes:
    """Apply per-artist image edits (max_edge resize, optional watermark text).

    No-op when image_edit.enabled is falsy. Returns (possibly unchanged) bytes.
    """
    from lab.rules import get_rule
    from PIL import Image, ImageDraw

    if not get_rule(rules, "image_edit", "enabled", default=False):
        return data

    img = Image.open(io.BytesIO(data)).convert("RGB")
    max_edge = get_rule(rules, "image_edit", "max_edge")
    if max_edge:
        img.thumbnail((int(max_edge), int(max_edge)))

    watermark = get_rule(rules, "image_edit", "watermark_text")
    if watermark:
        # simple bottom-right text stamp; small and semi-transparent
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        try:
            from PIL import ImageFont

            font = ImageFont.load_default()
        except Exception:
            font = None
        text = str(watermark)
        bbox = d.textbbox((0, 0), text, font=font) if font else None
        tw = bbox[2] - bbox[0] if bbox else len(text) * 6
        th = bbox[3] - bbox[1] if bbox else 12
        pad = 8
        pos = (img.size[0] - tw - pad, img.size[1] - th - pad)
        d.text(pos, text, fill=(255, 255, 255, 160), font=font)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    log.info("Image edited (max_edge=%s, watermark=%s) %s -> %s bytes",
             max_edge, bool(watermark), len(data), out.tell())
    return out.getvalue()
