from __future__ import annotations

"""SFW title + teaser. Layout, CTA and hashtags live in lab/caption.py; teaser lines in lab/teasers.py."""

import random
import re

from lab.teasers import pick_teaser
from lab.title import emoji_tail, title_base

MIN_CLUSTER_SCORE = 3
JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
GENERIC_NAMES = {
    "unknown", "illust", "illustration", "original", "girl", "girls", "character",
    "anime", "manga", "art", "artwork", "fanart", "general", "waifu",
}

CLUSTERS = [
    {"name": "Maid", "weights": {"maid": 5, "maid headdress": 3, "maid apron": 3, "メイド": 5, "メイド服": 5}},
    {"name": "Selfies", "weights": {"selfie": 5, "selfies": 5, "mirror selfie": 4, "holding phone": 2, "自撮り": 5}},
    {"name": "Leotard", "weights": {"playboy bunny": 5, "bunnysuit": 5, "bunny girl": 4, "leotard": 4, "highleg leotard": 3, "bunny ears": 3, "rabbit ears": 3, "バニーガール": 5, "バニー": 4, "レオタード": 4}},
    {"name": "Kimono", "weights": {"kimono": 5, "yukata": 5, "japanese clothes": 4, "obi": 3, "miko": 4, "furisode": 4, "着物": 5, "浴衣": 5, "巫女": 4}},
    {"name": "Swimsuit", "weights": {"swimsuit": 4, "bikini": 4, "one-piece swimsuit": 4, "school swimsuit": 4, "beach": 2, "水着": 5, "ビキニ": 4, "スク水": 4}},
    {"name": "Uniform", "weights": {"school uniform": 5, "uniform": 4, "sailor uniform": 4, "serafuku": 4, "gym uniform": 4, "sailor collar": 3, "pleated skirt": 2, "blazer": 2, "jk": 3, "制服": 5, "セーラー服": 4, "女子高生": 4, "ブレザー": 2}},
    {"name": "Legwear", "weights": {"thighhighs": 3, "pantyhose": 4, "black pantyhose": 4, "stockings": 4, "kneehighs": 3, "zettai ryouiki": 3, "legwear": 3, "ニーソ": 3, "タイツ": 4, "黒タイツ": 4, "ストッキング": 4}},
    {"name": "Kemonomimi", "weights": {"catgirl": 5, "cat girl": 5, "fox girl": 5, "wolf girl": 5, "cat ears": 4, "fox ears": 4, "wolf ears": 4, "dog ears": 4, "animal ears": 3, "cat tail": 3, "fox tail": 3, "kemonomimi": 4, "猫耳": 5, "狐耳": 5, "獣耳": 4, "ケモミミ": 4, "狐娘": 5, "猫娘": 5}},
    {"name": "Fantasy", "weights": {"elf": 5, "demon girl": 5, "succubus": 5, "vampire": 5, "dragon girl": 5, "angel": 4, "witch": 4, "fairy": 4, "monster girl": 4, "demon horns": 4, "pointy ears": 3, "horns": 3, "エルフ": 5, "悪魔": 4, "天使": 4, "魔女": 4, "吸血鬼": 5, "サキュバス": 5}},
    {"name": "VTuber", "weights": {"virtual youtuber": 5, "vtuber": 5, "hololive": 5, "nijisanji": 5, "ホロライブ": 5, "にじさんじ": 5}},
    {"name": "Duo", "weights": {"2girls": 4, "3girls": 4, "yuri": 4, "multiple girls": 3, "百合": 4}},
]

# Only the emoji of these is used now (the first line is the artist title or "Character · Game").
TITLES = {
    "Maid": ["Cute Maid 🎀", "Yes Master 🎀💕", "Maid Cafe Vibes ☕🎀", "Sweet Maid Girl 🎀😊", "Fluffy Maid Dress 🎀✨", "Your Maid Is Here 💖🎀", "Service With A Smile 🎀😍"],
    "Uniform": ["School Days 🏫💕", "Classroom Cutie 📚😍", "After School Time 🎒✨", "Uniform Vibes 📖💕", "Study Buddy 📚😊", "Class President 🏫✨", "Cutest Student 🎒💖"],
    "Swimsuit": ["Beach Day 🏖️☀️", "Bikini Time 👙💕", "Summer Vibes ☀️👙", "Pool Party 🏊‍♀️✨", "Splash Time 💦👙", "Sun And Fun ☀️😍", "Swimsuit Season 👙🔥"],
    "Selfies": ["Mirror Selfie 📸💕", "Say Cheese 📸😊", "Selfie Time 🤳✨", "Camera Ready 📸😍", "Just Me 🤳💖", "Snap Shot 📸🔥"],
    "Leotard": ["Bunny Girl 🐰💕", "Bunny Time 🐰✨", "Hop Hop Cutie 🐰😍", "Playful Bunny 🐰🔥", "Leotard Look ✨😳", "Bunny Night 🐰🌙"],
    "Kemonomimi": ["Fluffy Vibes 🐾", "Meow Hello 🐱", "Little Fox Spirit 🦊", "Ears And Tail 🐾💕", "Wild But Sweet 🐺", "Soft Paws ✨", "Curious Kitty 😊"],
    "Fantasy": ["Otherworldly Beauty ✨", "Not Quite Human 🌙", "Forest Spirit 🍃", "Horns And Charm 😊", "Ancient Magic ✨", "Ethereal Vibes 🌙"],
    "Kimono": ["Traditional Elegance 🎋", "Festival Ready 🏮", "Old World Charm ✨", "Silk And Grace 🎐", "Timeless Look 🎋😊"],
    "Legwear": ["Long Legs Energy 🖤", "Stocking Style ✨", "Leg Day Vibes 😳", "Sheer And Pretty 🎀", "Classy Legs 🖤💕"],
    "Duo": ["Two Is Better 💕", "Double Trouble 😍😍", "Best Friends Vibe 💕✨", "Partners In Crime 😏💕", "Two Cuties 💖💖"],
    "VTuber": ["Streaming Cutie 🎥💕", "Live And Lovely ✨", "Chat Says Hi 💬😊", "Virtual Charm 🎥✨", "On Air Cutie 🎥💖"],
    "General": ["So Cute 😍", "Beautiful Girl 💕", "Pretty Look ✨", "My Favorite 💖", "Just Perfect ✨😍", "Adorable 🥰"],
}

RARITY = [c["name"] for c in CLUSTERS]


def _norm_tag(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").lower().replace("_", " ")).strip()


def _make_matcher(needle: str):
    n = _norm_tag(needle)
    if re.match(r"^[a-z0-9][a-z0-9 \-]*$", n):
        safe = re.escape(n)
        regex = re.compile(rf"(^|[^a-z0-9]){safe}([^a-z0-9]|$)")
        return lambda tag: bool(regex.search(tag))
    return lambda tag: n in tag


COMPILED = [
    {
        "name": c["name"],
        "matchers": [(_make_matcher(needle), w) for needle, w in c["weights"].items()],
    }
    for c in CLUSTERS
]


def _tags_of(j: dict) -> list[str]:
    tr = list((j.get("tagTranslations") or {}).values()) if isinstance(j.get("tagTranslations"), dict) else []
    raw = [
        *(j.get("all_tags") or []),
        *(j.get("generalTags") or []),
        *(j.get("general_tags") or []),
        *(j.get("tagsRaw") or []),
        *tr,
    ]
    out = []
    seen = set()
    for t in raw:
        n = _norm_tag(t)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _pretty_name(s) -> str:
    text = str(s or "").strip()
    if "_" in text:
        return " ".join(w[:1].upper() + w[1:] for w in text.split("_") if w)
    return text


def _ok_name(s: str) -> bool:
    return bool(s) and len(s) <= 40 and not JP_RE.search(s) and _norm_tag(s) not in GENERIC_NAMES


def _pick_fresh(pool: list[str], slot: str, memory: dict) -> str:
    mem = list(memory.get(slot) or [])
    fresh = [x for x in pool if x not in mem] or list(pool)
    chosen = random.choice(fresh)
    mem.append(chosen)
    limit = min(4, max(0, len(pool) - 1))
    if limit and len(mem) > limit:
        mem = mem[-limit:]
    memory[slot] = mem
    return chosen


def sfw_parts(items: list[dict], runtime: dict, info: dict | None = None) -> tuple[str, str]:
    """(title, teaser) for an SFW post. Plain text, escaped later by caption.py.

    title  = artist title or "Character · Game", plus one emoji of the detected cluster
    teaser = a name-free line of the detected cluster ('' when no cluster was detected)

    Call once per post: it records the choices in runtime["recentCaptions"] so the
    next posts avoid repeating the same wording.
    """
    if not items:
        return "", ""
    memory = runtime.setdefault("recentCaptions", {})
    best: dict[str, int] = {}
    for it in items:
        tags = _tags_of(it)
        for c in COMPILED:
            score = sum(w for test, w in c["matchers"] if any(test(t) for t in tags))
            if score:
                best[c["name"]] = max(best.get(c["name"], 0), score)
    ranked = sorted(
        ({"name": name, "score": score} for name, score in best.items()),
        key=lambda r: (-r["score"], RARITY.index(r["name"]) if r["name"] in RARITY else 99),
    )
    cluster = ranked[0]["name"] if ranked and ranked[0]["score"] >= MIN_CLUSTER_SCORE else "General"
    if cluster in {"Duo", "VTuber"}:
        sec = next((r for r in ranked if r["name"] not in {"Duo", "VTuber"} and r["score"] >= MIN_CLUSTER_SCORE), None)
        cluster = sec["name"] if sec and sec["name"] in TITLES else "General"
    key = cluster if cluster in TITLES else "General"

    characters: list[str] = []
    for it in items:
        for c in it.get("character_tags") or []:
            name = _pretty_name(c)
            if _ok_name(name) and not any(x.lower() == name.lower() for x in characters):
                characters.append(name)

    franchise = None
    for it in items:
        cand = _pretty_name((it.get("franchise_tags") or [None])[0] or it.get("inferred_franchise") or "")
        if _ok_name(cand):
            franchise = cand
            break

    if info:  # names resolved by classify_tags (dictionary + 'Name (Work)' tags)
        characters = [c for c in info.get("characters") or [] if _ok_name(c)]
        franchise = info["franchise"] if _ok_name(info.get("franchise") or "") else None

    # the artist's own Latin title (set by the pipeline) beats "Character · Game"
    art_title = next((it.get("art_title") for it in items if it.get("art_title")), None)
    base = title_base(art_title, characters, franchise)
    emoji = emoji_tail(_pick_fresh(TITLES[key], f"t:{key}", memory))
    title = f"{base} {emoji}" if base else ""
    teaser = pick_teaser(None if key == "General" else key, memory)
    return title, teaser
