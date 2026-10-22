from __future__ import annotations

import re
import unicodedata

from lab.games import franchise_display

MAX_TITLE_LEN = 40

GENERIC_TITLES = {
    "untitled", "illustration", "illust", "original", "girl", "girls", "anime girl",
    "anime", "art", "artwork", "fanart", "fan art", "no title", "title", "drawing",
    "sketch", "waifu", "oc", "commission",
}
# Always rejected, regardless of adult_tags.csv (the channel is monetized)
EXPLICIT_WORDS = {
    "nsfw", "r18", "xxx", "porn", "lewd", "nude", "naked", "sex", "hentai",
    "boobs", "tits", "nipple", "nipples", "cum", "fuck", "slut", "whore", "creampie",
}
URLISH = re.compile(r"https?:|www\.|[#@]")
NUMERIC = re.compile(r"^(?:img|image|illust|illustration|pixiv|art|no)?[\s_\-#]*\d+$", re.I)
EMOJI_RE = re.compile(r"[\u2190-\u2bff\U0001F000-\U0001FFFF\u200d\ufe0f]")
EMOJI_TAIL = re.compile(r"[\u2190-\u2bff\U0001F000-\U0001FFFF].*$")
TITLE_SPLIT = re.compile(r"\s*[|/／・,，×]\s*|\s+[-–—xX]\s+")


def _only_latin_letters(s: str) -> bool:
    for ch in s:
        if ch.isalpha() and "LATIN" not in unicodedata.name(ch, ""):
            return False
    return True


def clean_title(raw, blocked: set[str] | None = None) -> str | None:
    """The artist's own title if it is safe to print in a caption, else None.

    Rejected: non-Latin text, links/hashtags, generic or numeric titles, too long,
    anything with an explicit word (built-in list + adult_tags.csv).
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    if not text.strip() or URLISH.search(text):
        return None
    if not _only_latin_letters(text):
        return None
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"[^\w\s\-'’.,!?&:+()~]", " ", text)
    text = re.sub(r"[_\s]+", " ", text).strip(" -.,:~")
    if len([c for c in text if c.isalpha()]) < 3 or len(text) > MAX_TITLE_LEN:
        return None
    low = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    if low in GENERIC_TITLES or NUMERIC.match(text):
        return None
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    if words & EXPLICIT_WORDS:
        return None
    if blocked and (any(w in blocked for w in words) or low.replace(" ", "_") in blocked):
        return None
    return text


def title_parts(raw, limit: int = 6) -> list[str]:
    """Whole title + its pieces ('Name | Work', 'A / B', 'A - B'), for character/franchise lookup."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return []
    parts = [text]
    pieces = [p.strip() for p in TITLE_SPLIT.split(text) if p and p.strip()]
    if len(pieces) > 1:
        parts += [p for p in pieces if p not in parts]
    return parts[:limit]


def emoji_tail(s: str) -> str:
    """'Beach Day 🏖️☀️' -> '🏖️☀️' (default ✨)."""
    m = EMOJI_TAIL.search(str(s or ""))
    return m.group(0).strip() if m else "✨"


def title_base(art_title, characters, franchise) -> str:
    """First line of the caption, without emoji.

    1. the artist's own clean title, if there is one
    2. one or two characters: 'Char · Game' / 'Char1 & Char2 · Game'
    3. three or more characters, or none: just the game
    '' when nothing is known (the caption then has no title line).
    """
    if art_title:
        return str(art_title).strip()
    game = franchise_display(franchise) if franchise else ""
    names = [str(c).strip() for c in (characters or []) if str(c or "").strip()]
    if 1 <= len(names) <= 2:
        who = " & ".join(names)
        return f"{who} · {game}" if game else who
    return game
