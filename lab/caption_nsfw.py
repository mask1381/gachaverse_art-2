from __future__ import annotations

"""NSFW title + teaser. Layout, CTA and hashtags live in lab/caption.py; teaser lines in lab/teasers.py."""

import re

from lab.teasers import NSFW_TO_CLUSTER, pick_teaser
from lab.title import emoji_tail, title_base

JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
GENERIC_WORDS = {
    "girl", "girls", "boy", "boys", "character", "characters", "anime", "manga",
    "illustration", "art", "artwork", "fan", "fanart", "original", "woman", "man",
    "person", "cute", "beautiful", "lady", "model",
}

JP_FRANCHISE_MAP = {
    "五等分の花嫁": "The Quintessential Quintuplets",
    "鬼滅の刃": "Demon Slayer: Kimetsu no Yaiba",
    "呪術廻戦": "Jujutsu Kaisen",
    "ぼっち・ざ・ろっく": "Bocchi The Rock!",
    "ブルーアーカイブ": "Blue Archive",
    "原神": "Genshin Impact",
}

# Only the emoji of these is used now (the first line is the artist title or "Character · Game").
TAG_TITLES = {
    "oppai": ["So Pretty 😍", "Stunning Figure ✨", "Big Beautiful Vibes 💕", "Wow, Look At Her 😳", "Curvy Cutie 💕", "Can’t Look Away 👀✨", "Golden Figure ✨😍", "Full Of Charm 💖"],
    "maid": ["Cute Maid 🎀", "Sweet Maid 💕", "Maid Girl 🎀😊", "Yes Master 🎀", "Maid Cafe Vibes ☕🎀", "Adorable Maid 💕", "Fluffy Maid Dress 🎀", "My Maid 💖"],
    "uniform": ["School Girl 📚", "Cute Uniform 😊", "Student Vibe 📖💕", "Classroom Cutie 📚💕", "After School 🎒", "Uniform Days 📖", "School Days 🏫💕", "Study Buddy 📚😊"],
    "waifu": ["So Cute 😍", "Beautiful Girl 💕", "Pretty Look ✨", "My Favorite 💖", "So Pretty 😊", "Wife Material 💍💕", "Adorable 🥰", "Just Perfect ✨", "Love At First Sight 💘", "Anime Perfection ✨💕"],
    "bathing": ["Hot Spring Time ♨️", "Bath Time 🛁", "Steamy Soak ♨️✨", "Onsen Vibes ♨️💕", "Relax Mode 🛁", "Spa Day 🛁✨", "Warm And Cozy ♨️💖", "Calm Evening 🛁😊"],
    "milf": ["Mature Beauty 😍", "Elegant Charm 💃", "Classy And Lovely ✨", "Big Sister Energy 💕", "Gentle Beauty 💖", "Graceful Vibes 😊", "Mature Charm ✨", "Elegant Lady 💐"],
    "ass": ["Nice View 👀", "Look Behind You 😊", "Back View 💕", "Sweet Angle ✨", "Pretty From Behind 😍", "Cute Angle 💖"],
    "selfies": ["Mirror Selfie 📸", "Say Cheese 📸😊", "Candid Shot 📸", "Selfie Time 🤳", "Just Me 🤳💕", "Camera Ready 📸✨", "Snap Shot 🤳", "Casual Selfie 📸😊"],
    "swimsuit": ["Beach Day 🏖️", "Bikini Time 👙", "Poolside 🏊‍♀️", "Summer Vibes ☀️👙", "Splash Time 💦", "Beach Vibes 🏖️✨", "Swimsuit Season 👙☀️", "Sun & Fun ☀️"],
}
CLUSTER_KEYWORDS = {
    "maid": ["maid", "メイド", "メイド服", "メイドさん"],
    "selfies": ["selfie", "自撮り", "鏡", "mirror selfie"],
    "swimsuit": ["swimsuit", "bikini", "水着", "ビキニ", "競泳水着", "スク水"],
    "uniform": ["school uniform", "制服", "セーラー服", "jk", "女子高生", "ブレザー"],
    "milf": ["milf", "人妻", "熟女", "お母さん", "ママ"],
    "bathing": ["onsen", "hot spring", "入浴", "お風呂", "温泉", "バスタオル", "風呂"],
    "ass": ["尻", "お尻", "ヒップ", "butt focus", "ass focus"],
    "oppai": ["oppai", "おっぱい", "巨乳", "爆乳", "谷間", "美乳", "乳", "boob", "breast"],
}
TAG_PRIORITY = list(CLUSTER_KEYWORDS)


def is_acceptable(en: str | None) -> bool:
    if not en:
        return False
    s = en.strip()
    if not s or JP_RE.search(s):
        return False
    words = [w for w in s.split() if w]
    if not words or len(words) > 3 or len(s) > 25:
        return False
    if any(w.lower() in GENERIC_WORDS for w in words):
        return False
    return True


def to_title(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in s.strip().split())


def match_needle(tag: str, needle: str) -> bool:
    if re.match(r"^[a-z0-9][a-z0-9\-]*$", needle, re.I):
        return bool(re.search(rf"(^|[^a-z0-9]){re.escape(needle)}([^a-z0-9]|$)", tag, re.I))
    return needle in tag


def hash_string(s: str) -> int:
    h = 0
    for ch in s:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    return abs(h)


def get_item_tags(it: dict) -> list[str]:
    tags = it.get("generalTags") if isinstance(it.get("generalTags"), list) else (it.get("tags") or [])
    return [str(t).lower() for t in tags]


def _title_teaser(items: list[dict], translation_map: dict[str, str | None], runtime: dict, info: dict | None) -> tuple[str, str]:
    def lookup_jp(raw) -> str | None:
        s = str(raw or "").strip()
        if not s:
            return None
        if not JP_RE.search(s):
            return s
        return translation_map.get(s)

    all_tags = set()
    for it in items:
        all_tags.update(get_item_tags(it))

    dominant = next(
        (
            cluster
            for cluster in TAG_PRIORITY
            if any(any(match_needle(t, k) for k in CLUSTER_KEYWORDS[cluster]) for t in all_tags)
        ),
        "waifu",
    )
    id_seed = ",".join(sorted(str(it.get("id") or it.get("image_url_std") or "") for it in items))
    base_seed = f"{id_seed}|{','.join(sorted(all_tags))}"

    def pick(options, salt):
        if not options:
            return None
        return options[hash_string(base_seed + "::" + salt) % len(options)]

    art_title = next((it.get("art_title") for it in items if it.get("art_title")), None)

    franchise_en = None
    for it in items:
        raws = [*(it.get("generalTags") or []), *(it.get("tagsRaw") or [])]
        for raw in raws:
            if raw in JP_FRANCHISE_MAP:
                franchise_en = JP_FRANCHISE_MAP[raw]
                break
        if franchise_en:
            break
    if not franchise_en:
        raw = next(
            (
                v
                for it in items
                for v in [
                    it.get("inferred_franchise"),
                    (it.get("franchise_tags") or [None])[0] if isinstance(it.get("franchise_tags"), list) else it.get("franchise_tags"),
                ]
                if v and str(v).strip()
            ),
            None,
        )
        if raw:
            franchise_en = lookup_jp(raw)

    unique_chars: list[str] = []
    seen_c = set()
    for it in items:
        c = it.get("character") or ((it.get("character_tags") or [None])[0] if isinstance(it.get("character_tags"), list) else None)
        if c and c != "unknown":
            mapped = lookup_jp(c)
            if mapped and mapped not in seen_c:
                seen_c.add(mapped)
                unique_chars.append(mapped)

    if info:  # names resolved by classify_tags
        unique_chars = list(info.get("characters") or [])
        if info.get("franchise"):
            franchise_en = info["franchise"]

    base = title_base(art_title, unique_chars, franchise_en)
    emoji = emoji_tail(pick(TAG_TITLES.get(dominant) or TAG_TITLES["waifu"], "title"))
    title = f"{base} {emoji}" if base else ""
    teaser = pick_teaser(NSFW_TO_CLUSTER.get(dominant), runtime.setdefault("recentCaptions", {}))
    return title, teaser


def nsfw_parts(items: list[dict], runtime: dict | None, info: dict | None = None) -> tuple[str, str]:
    """(title, teaser) for an NSFW post. Plain text, escaped later by caption.py."""
    translation_map: dict[str, str | None] = {}
    for it in items:
        for jp, en in (it.get("tagTranslations") or {}).items():
            if is_acceptable(en):
                translation_map[jp] = to_title(en)
    return _title_teaser(items, translation_map, runtime if runtime is not None else {}, info)
