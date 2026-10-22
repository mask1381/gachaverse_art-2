from __future__ import annotations

import asyncio
import logging
import os

from lab.rules import get_rule
from lab.userbot_zip import userbot_configured

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "enforce": False,        # False = never blocks a post (a failed check only picks the best page)
    "min_photo_ratio": 0.0,  # photos Telegram cached / photos we put in the page; raise once logs show real numbers
    "retries": 3,            # polls per page
    "wait": 3.0,             # seconds between polls (Instant View is built lazily)
}


def _settings(rules: dict) -> dict:
    cfg = get_rule(rules, "delivery", "verify", "telegraph", default={}) or {}
    return {**DEFAULTS, **cfg}


def _state_of(wp) -> dict:
    cached = getattr(wp, "cached_page", None)
    photos = len(getattr(cached, "photos", None) or []) if cached is not None else 0
    return {"has_iv": cached is not None, "photos": photos, "state": type(wp).__name__}


def _better(a: dict | None, b: dict | None) -> dict | None:
    if a is None:
        return b
    if b is None:
        return a
    return b if (b["has_iv"], b["photos"]) > (a["has_iv"], a["photos"]) else a


async def _probe(page_url: str, expected: int, s: dict) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import GetWebPagePreviewRequest, GetWebPageRequest

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("userbot session is not authorized")
        last = {"has_iv": False, "photos": 0, "state": "none", "attempts": 0}
        retries = max(1, int(s["retries"]))
        for attempt in range(1, retries + 1):
            seen: list[str] = []
            best: dict | None = None

            # 1) ask Telegram to build the preview, like typing the link in a chat does
            try:
                res = await client(GetWebPagePreviewRequest(message=page_url))
                media = getattr(res, "media", res)
                wp = getattr(media, "webpage", None)
                if wp is not None:
                    st = _state_of(wp)
                    seen.append(f"preview={st['state']}")
                    best = _better(best, st)
            except Exception as exc:
                seen.append(f"preview=error({type(exc).__name__})")

            # 2) ask for the Instant View page itself
            try:
                res = await client(GetWebPageRequest(url=page_url, hash=0))
                wp = getattr(res, "webpage", res)  # newer Telethon wraps it in messages.WebPage
                st = _state_of(wp)
                seen.append(f"iv={st['state']}")
                best = _better(best, st)
            except Exception as exc:
                seen.append(f"iv=error({type(exc).__name__})")

            best = best or {"has_iv": False, "photos": 0, "state": "none"}
            last = {"has_iv": best["has_iv"], "photos": best["photos"], "state": ",".join(seen) or best["state"], "attempts": attempt}
            ratio = best["photos"] / expected if expected else 1.0
            if best["has_iv"] and ratio >= float(s["min_photo_ratio"]):
                return last
            if attempt < retries:
                await asyncio.sleep(float(s["wait"]))
        return last
    finally:
        await client.disconnect()


def verify_telegraph(page_url: str, expected: int, rules: dict) -> dict:
    """Ask Telegram whether Instant View exists for the page and how many photos it cached.

    Returns {"ok", "passed", "checked", "enforced", "has_iv", "photos", "expected", "reason"}.
    passed: the check ran and the page meets the criterion (None when the check could not run).
    ok: False only when the check ran, failed, and enforce is on.
    Any problem with the check itself (no userbot, API error) never blocks the post.
    """
    s = _settings(rules)
    out = {"ok": True, "passed": None, "checked": False, "enforced": bool(s["enforce"]), "has_iv": None,
           "photos": 0, "expected": expected, "reason": ""}
    if not s["enabled"]:
        out["reason"] = "verification disabled"
        return out
    if not userbot_configured():
        out["reason"] = "userbot not configured"
        return out
    try:
        probe = asyncio.run(_probe(page_url, expected, s))
    except Exception as exc:
        out["reason"] = f"check error: {exc}"
        log.warning("Telegraph check could not run: %s", exc)
        return out

    out.update(checked=True, has_iv=probe["has_iv"], photos=probe["photos"])
    ratio = probe["photos"] / expected if expected else 1.0
    passed = bool(probe["has_iv"]) and ratio >= float(s["min_photo_ratio"])
    out["passed"] = passed
    if passed:
        out["reason"] = f"instant view ok ({probe['photos']}/{expected} photos, {probe['state']})"
    else:
        out["reason"] = (
            f"instant view missing or incomplete (has_iv={probe['has_iv']}, "
            f"photos={probe['photos']}/{expected}, state={probe['state']}, tries={probe['attempts']})"
        )
        out["ok"] = not s["enforce"]
    return out
