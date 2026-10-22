from __future__ import annotations

"""One caption builder for every delivery format.

Skeleton (same for NSFW and SFW):

    <b>title</b>
    <i>teaser</i>

    <blockquote>CTA line(s)</blockquote>

    #hashtags

Title and teaser are picked once per post (caption_parts) and reused if delivery
falls back to another format, so the caption never changes its wording mid-post.

Album posts carry no CTA (the photos are already in the channel). Zip has one link,
the gallery ("telegraph") has one "Get the album" link per storage album, each on
its own line inside the quote. There is no footer.
"""

import html
import logging
import re

from lab.caption_nsfw import nsfw_parts
from lab.caption_sfw import sfw_parts
from lab.rules import get_rule
from lab.tags import FORMAT_TAGS
log = logging.getLogger(__name__)

FORMATS = ("zip", "album", "telegraph")
CAPTION_LIMIT = 1024  # Telegram limit for photo / album captions (visible characters)

TAG_RE = re.compile(r"<[^>]+>")


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _visible_len(text: str) -> int:
    """Length Telegram counts: the text after HTML tags are parsed away."""
    return len(html.unescape(TAG_RE.sub("", text)))


def caption_parts(items: list[dict], nsfw: bool, runtime: dict, info: dict | None = None) -> dict:
    """Pick title + teaser once per post."""
    if nsfw:
        title, teaser = nsfw_parts(items, runtime, info)
    else:
        title, teaser = sfw_parts(items, runtime, info)
    return {"title": title, "teaser": teaser}


def _gallery_cta(count: int, links: list[str]) -> str:
    n = len(links)
    if n > 1:
        return "\n\n".join(
            f'📖 <a href="{html.escape(link, quote=True)}">Get the album, part {i} of {n}</a>'
            for i, link in enumerate(links, start=1)
        )
    pics = f"{count} pic{'s' if count != 1 else ''}"
    if n == 1:
        return f'📖 {pics} · <a href="{html.escape(links[0], quote=True)}">Get the album</a>'
    return f"📖 {pics}"


def _cta(fmt: str, count: int, page_url: str | list[str] | None) -> str:
    """CTA lines for one format; empty for album (the photos are in the channel)."""
    if fmt == "zip":
        # page_url = the storage bot's deep link for this zip
        if isinstance(page_url, str) and page_url:
            return f'📦 <a href="{html.escape(page_url, quote=True)}">Full quality ZIP 👉 Tap to get</a>'
        return "📦 Full quality ZIP below 👇"
    if fmt == "telegraph":
        # page_url = list of storage bot deep links, one per album part
        if isinstance(page_url, str):
            links = [page_url]
        else:
            links = list(page_url or [])
        return _gallery_cta(count, [x for x in links if x])
    return ""


def _assemble(parts: dict, cta: str, tags: list[str]) -> str:
    lines = [f"<b>{_esc(parts.get('title'))}</b>"]
    if parts.get("teaser"):
        lines.append(f"<i>{_esc(parts['teaser'])}</i>")
    if cta:
        lines += ["", f"<blockquote>{cta}</blockquote>"]
    if tags:
        lines += ["", " ".join(tags)]
    return "\n".join(lines)


def build_caption(
    parts: dict,
    fmt: str,
    count: int,
    hashtags: list[str] | None,
    rules: dict | None = None,
    info: dict | None = None,
    page_url: str | list[str] | None = None,
) -> str:
    """Caption for one delivery format (zip | album | telegraph).

    count    - number of pages in the post
    hashtags - classify_tags()["hashtags"] (franchise, characters, category, format)
    info     - classify_tags() result; used to protect important hashtags when trimming
    page_url - zip: storage bot deep link (str); telegraph: list of storage bot deep links
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown caption format {fmt!r}")

    cta = _cta(fmt, count, page_url)
    tags = [t for t in (hashtags or []) if t]
    fmt_tag = FORMAT_TAGS.get(fmt)
    if fmt_tag and get_rule(rules or {}, "hashtags", "format_tag", default=True) is not False:
        if fmt_tag.lower() not in {t.lower() for t in tags}:
            tags.insert(0, fmt_tag)
    text = _assemble(parts, cta, tags)

    # photo / album captions are limited to 1024 visible characters:
    # drop the least important hashtags first (category / pass-through), then the teaser
    protected = {h.lower() for h in FORMAT_TAGS.values()}
    if info:
        protected |= {h.lower() for h in info.get("character_hashtags") or []}
        if info.get("franchise_hashtag"):
            protected.add(info["franchise_hashtag"].lower())

    while _visible_len(text) > CAPTION_LIMIT:
        idx = next((i for i in range(len(tags) - 1, -1, -1) if tags[i].lower() not in protected), None)
        if idx is None:
            break
        log.warning("Caption too long (%s), dropping hashtag %s", _visible_len(text), tags[idx])
        tags.pop(idx)
        text = _assemble(parts, cta, tags)

    if _visible_len(text) > CAPTION_LIMIT and parts.get("teaser"):
        log.warning("Caption still too long (%s), dropping teaser", _visible_len(text))
        text = _assemble({**parts, "teaser": ""}, cta, tags)

    if _visible_len(text) > CAPTION_LIMIT:
        log.warning("Caption is still %s chars after trimming", _visible_len(text))
    return text
