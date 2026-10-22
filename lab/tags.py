from __future__ import annotations
import logging

import csv
import random
import re
import unicodedata
from datetime import date
from pathlib import Path

from lab.enrich import PAREN_JUNK, _lookup_tag, clean_value, franchise_total, get_maps, norm_key
from lab.title import title_parts
from lab.config import DATA_DIR

log = logging.getLogger(__name__)

BRAND_TAG = "#GachaVerse"
FORMAT_TAGS = {"zip": "#ZIP", "telegraph": "#Gallery"}  # replaces the old #18Plus: the post format is the label
JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
PAREN_RE = re.compile(r"^(?P<head>[^()]+?)\s*\((?P<inside>[^()]+)\)\s*$")
GENERIC_NAMES = {"unknown", "illust", "illustration", "original", "girl", "girls", "character", "anime", "manga", "art", "artwork", "fanart", "general", "waifu"}

SINGLE_FRANCHISE_MIN = 150  # a one-word dictionary franchise needs this many total sightings (or a character from it)
SINGLE_CHAR_MIN_SEEN = 100  # a one-word dictionary character needs this many sightings (or a matching franchise tag)
REDUNDANT_MIN = 4           # shortest hashtag body compared when checking pass-through redundancy

# Overridable per artist through rules["hashtags"] (see rules.py).
DEFAULT_OPTS = {
    "max_category": 4,        # themes + pass-through together
    "passthrough": True,      # artist's own tags that nobody classified
    "passthrough_max": 2,
    "fallback": ["#GachaArt"],   # only when no theme/pass-through tag survived
    "max_len": 30,            # longest hashtag body we accept
    "pass_max_len": 25,       # longest pass-through hashtag body
    "pass_max_words": 3,      # longest source text (words) for a pass-through
    "game_hashtag": None,     # e.g. "#GenshinImpact": always the franchise hashtag (the post passed the game gate)
    "game_name": None,
    "fixed": []
}


def to_hashtag(name, max_len: int | None = None) -> str | None:
    """'Genshin Impact' -> '#GenshinImpact'. Accents are folded; non-Latin text yields None."""
    if not name:
        return None
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\([^)]*\)", " ", text)
    parts = [w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", text) if w]
    joined = "".join(parts)
    if len(joined) < 3 or joined.isdigit():
        return None
    if max_len and len(joined) > max_len:
        return None
    return f"#{joined}"


def _redundant(ht: str, anchors: list[str]) -> bool:
    """True when a pass-through hashtag only repeats a franchise/character hashtag.

    '#FrierenAtTheFuneral' next to '#Frieren', '#YunaFFX' next to '#Yuna': one contains the other.
    Very short bodies (< REDUNDANT_MIN) are never treated as a match, so '#Rin' survives next to '#Karina'.
    """
    body = ht.lstrip("#").lower()
    for anchor in anchors:
        ab = anchor.lstrip("#").lower()
        if not ab:
            continue
        if len(ab) >= REDUNDANT_MIN and ab in body:
            return True
        if len(body) >= REDUNDANT_MIN and body in ab:
            return True
    return False


def _read_tag_column(path: Path) -> set[str]:
    out: set[str] = set()
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = norm_key((row.get("tag") or "").strip())
            if key:
                out.add(key)
    return out


def load_content_map(path: Path) -> dict[str, str]:
    """content_tags.csv -> {normalized tag: hashtag}. Row order = theme priority."""
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tag = (row.get("tag") or "").strip()
            ht = (row.get("hashtag") or "").strip()
            key = norm_key(tag)
            if key and ht:
                mapping[key] = ht
    return mapping


def load_adult_set(path: Path) -> set[str]:
    """adult_tags.csv -> explicit tags (flag the post as adult, never printed)."""
    return _read_tag_column(path)


def load_drop_set(path: Path) -> set[str]:
    """drop_tags.csv -> junk tags that are silently removed (AI, generic, R-18 ...)."""
    return _read_tag_column(path)


_SEP = re.compile(r"[\s_\-./:'’]+")
_FILTERS: dict | None = None


def load_filters(path: Path) -> dict:
    """hashtag_filters.csv (kind,value) -> {"stems": [...], "regexes": [...]}."""
    out: dict = {"stems": [], "regexes": []}
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            kind = (row.get("kind") or "").strip().lower()
            value = (row.get("value") or "").strip()
            if not value:
                continue
            if kind == "stem":
                out["stems"].append(_SEP.sub("", value.lower()))
            elif kind == "regex":
                try:
                    out["regexes"].append(re.compile(value, re.I))
                except re.error as exc:
                    log.warning("Bad regex in hashtag_filters.csv %r: %s", value, exc)
    return out


def get_filters() -> dict:
    global _FILTERS
    if _FILTERS is None:
        _FILTERS = load_filters(DATA_DIR / "hashtag_filters.csv")
    return _FILTERS


def pass_blocked(ht: str, text: str, name: str, en: str, opts: dict) -> bool:
    """True when an artist's own tag must not be printed as a pass-through hashtag."""
    body = ht.lstrip("#")
    if len(body) > opts["pass_max_len"]:
        return True
    if len(re.findall(r"[A-Za-z0-9]+", text)) > opts["pass_max_words"]:
        return True
    if re.search(r"\d{3,}", body):
        return True
    raw = " ".join(str(x or "") for x in (name, en, text)).lower().replace("_", " ")
    compact = _SEP.sub("", raw)
    f = get_filters()
    return any(s in compact for s in f["stems"]) or any(rx.search(raw) for rx in f["regexes"])


def _ns_order(maps):
    return [
        ("character", maps["character"]),
        ("franchise", maps["franchise"]),
        ("studio", maps["studio"]),
        ("source", maps["source"]),
    ]


def _unique_raw_tags(items: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        for key in ("all_tags", "tagsRaw", "generalTags", "tags"):
            val = it.get(key)
            if not isinstance(val, list):
                continue
            for t in val:
                s = str(t).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
    return out


def _tag_pairs(items: list[dict]) -> list[tuple[str, str]]:
    """[(pixiv tag, its English translation or '')]. Falls back to flat raw tags."""
    for it in items:
        src = it.get("pixivTags")
        if isinstance(src, list) and src:
            seen: set[str] = set()
            pairs = []
            for t in src:
                name = str(t.get("name") or "").strip()
                en = str(t.get("en") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    pairs.append((name, "" if en == name else en))
            return pairs
    return [(t, "") for t in _unique_raw_tags(items)]


def _latin(s: str) -> bool:
    return bool(s) and not JP_RE.search(s)


def _name_ok(s: str) -> bool:
    s = clean_value(s)
    return bool(s) and len(s) <= 40 and _latin(s) and norm_key(s).replace("_", " ") not in GENERIC_NAMES


def _row_franchise_norms(row: dict) -> set[str]:
    return {norm_key(clean_value(row.get(c))) for c in ("franchise", "franchise_en", "franchise_jp")} - {""}


def _row_franchise_name(row: dict) -> str:
    return clean_value(row.get("franchise_en")) or clean_value(row.get("franchise")) or clean_value(row.get("franchise_jp"))


def _candidates(name: str, en: str, ns, content_map: dict[str, str], opts: dict) -> list[tuple]:
    """Every possible reading of one Pixiv tag, in priority order. Context checks happen later."""
    forms = [f for f in (name, en) if f]
    norms = [norm_key(f) for f in forms]
    cands: list[tuple] = []

    for n in norms:
        hit = _lookup_tag(n, ns, allow_fuzzy=False)
        if hit:
            ns_hit, rec = hit
            row = rec["row"]
            if ns_hit == "character":
                cands.append(("char", clean_value(row.get("character")), row))
            elif ns_hit == "franchise":
                cands.append(("franchise", _row_franchise_name(row), row))
            else:
                cands.append(("dict_other",))
            break

    for f in forms:  # 'Name (Work)' is how Pixiv writes characters, even ones the dictionary lacks
        m = PAREN_RE.match(f.replace("_", " "))
        if not m:
            continue
        head, inside = m.group("head").strip(), m.group("inside").strip()
        if _name_ok(head) and _latin(inside) and not PAREN_JUNK.search(norm_key(inside)):
            cands.append(("paren_char", head, inside))
            break

    for n in norms:
        ht = content_map.get(n)
        if ht:
            cands.append(("theme", ht))
            break

    text = en if en else (name if _latin(name) else "")
    if text:
        ht = to_hashtag(text, opts["max_len"])
        if ht:
            if pass_blocked(ht, text, name, en, opts):
                cands.append(("blocked",))
            else:
                cands.append(("pass", ht))
    return cands


def classify_tags(
    items: list[dict],
    dict_rows: list[dict],
    content_map: dict[str, str],
    adult_set: set[str],
    drop_set: set[str] | None = None,
    opts: dict | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Decide what every Pixiv tag of one post becomes.

    Order: adult/drop (removed) -> dictionary equivalent -> theme from content_tags.csv ->
    otherwise the artist's own tag passes through (English translation preferred).
    The dictionary is a supplement: a character written as 'Name (Work)' is accepted even
    when neither is in the dictionary. The post title is also scanned for characters/franchise.
    A pass-through hashtag that only repeats the franchise or a character hashtag is skipped.
    """
    rng = rng or random
    o = {**DEFAULT_OPTS, **(opts or {})}
    drop_set = drop_set or set()
    maps, _ = get_maps(dict_rows)
    ns = _ns_order(maps)

    pairs = _tag_pairs(items)
    entries = []
    adult = False
    for name, en in pairs:
        norms = [norm_key(f) for f in (name, en) if f]
        if any(n in adult_set for n in norms):
            adult = True
            entries.append({"tag": name, "en": en, "status": "adult", "ht": "", "cands": []})
        elif any(n in drop_set for n in norms):
            entries.append({"tag": name, "en": en, "status": "drop", "ht": "", "cands": []})
        else:
            entries.append({"tag": name, "en": en, "status": "", "ht": "", "cands": _candidates(name, en, ns, content_map, o)})

    # title-derived candidates: characters/franchise only (dictionary or 'Name (Work)'),
    # any script; never themes/pass-through, never counted in stats
    first_title = next((str(it.get("title")) for it in items if it.get("title")), "")
    for part in title_parts(first_title):
        cands = [c for c in _candidates(part, "", ns, content_map, o) if c[0] in ("char", "paren_char", "franchise")]
        entries.append({"tag": part, "en": "", "status": "" if cands else "skip", "ht": "", "cands": cands, "from_title": True})

    # context for confidence checks
    franchise_norms: set[str] = set()
    for e in entries:
        for c in e["cands"]:
            if c[0] == "franchise":
                franchise_norms.add(norm_key(c[1]))
            elif c[0] == "paren_char":
                franchise_norms.add(norm_key(c[2]))

    # stage A: characters
    characters: list[dict] = []  # {"name","ht","row","inside"}
    char_franchise_norms: set[str] = set()
    for e in entries:
        if e["status"]:
            continue
        for c in e["cands"]:
            if c[0] == "char":
                name, row = c[1], c[2]
                ht = to_hashtag(name, o["max_len"])
                if not ht or not _name_ok(name):
                    continue
                if " " not in name.strip():
                    seen = int(row.get("seen_count") or 0)
                    if seen < SINGLE_CHAR_MIN_SEEN and not (_row_franchise_norms(row) & franchise_norms):
                        continue
                characters.append({"name": name, "ht": ht, "row": row, "inside": None, "tag": e["tag"]})
                char_franchise_norms |= _row_franchise_norms(row)
                e["status"], e["ht"] = "character", ht
                break
            if c[0] == "paren_char":
                ht = to_hashtag(c[1], o["max_len"])
                if not ht:
                    continue
                characters.append({"name": c[1], "ht": ht, "row": None, "inside": c[2], "tag": e["tag"]})
                e["status"], e["ht"] = "character", ht
                break

    # stage B: everything else
    franchise_hits: list[tuple[str, str]] = []  # (name, tag)
    for e in entries:
        if e["status"]:
            continue
        for c in e["cands"]:
            kind = c[0]
            if kind in ("char", "paren_char"):
                continue
            if kind == "blocked":
                e["status"] = "filtered"
                break
            if kind == "franchise":
                fname = c[1]
                ht = to_hashtag(fname, o["max_len"])
                multi = "_" in norm_key(fname)
                if ht and _name_ok(fname) and (multi or franchise_total(fname) >= SINGLE_FRANCHISE_MIN or norm_key(fname) in char_franchise_norms):
                    franchise_hits.append((fname, e["tag"]))
                    e["status"], e["ht"] = "franchise", ht
                    break
                continue
            if kind == "dict_other":
                e["status"] = "dict"
                break
            if kind == "theme":
                e["status"], e["ht"] = "theme", c[1]
                break
            if kind == "pass":
                e["status"], e["ht"] = "pass", c[1]
                break
        if not e["status"]:
            e["status"] = "skip"

    # franchise: explicit dictionary tag -> franchise of a dictionary character -> 'Name (Work)' inside
    franchise_name = franchise_ht = None
    franchise_tag = None
    options: list[tuple[str, str | None]] = [(n, t) for n, t in franchise_hits]
    options += [(_row_franchise_name(ch["row"]), ch["tag"]) for ch in characters if ch["row"] is not None]
    options += [(ch["inside"], ch["tag"]) for ch in characters if ch["inside"]]
    for fname, tag in options:
        if not _name_ok(fname):
            continue
        ht = to_hashtag(fname, o["max_len"])
        if ht:
            franchise_name, franchise_ht, franchise_tag = fname, ht, tag
            break

    if o.get("game_hashtag"):
        franchise_name, franchise_ht = o.get("game_name") or franchise_name, o["game_hashtag"]

    # assemble
    taken = {x.lower() for x in [BRAND_TAG, franchise_ht or ""]}
    char_hts: list[str] = []
    char_names: list[str] = []
    for ch in characters:
        if ch["ht"].lower() not in taken:
            taken.add(ch["ht"].lower())
            char_hts.append(ch["ht"])
            char_names.append(ch["name"])

    rank: dict[str, int] = {}
    for ht in content_map.values():
        rank.setdefault(ht, len(rank))
    themes: list[str] = []
    for e in entries:
        if e["status"] == "theme" and e["ht"] not in themes and e["ht"].lower() not in taken:
            themes.append(e["ht"])
    themes.sort(key=lambda h: rank.get(h, 10**6))
    themes = themes[: o["max_category"]]
    taken |= {t.lower() for t in themes}

    passthrough: list[str] = []
    if o["passthrough"]:
        room = max(0, o["max_category"] - len(themes))
        limit = min(room, int(o["passthrough_max"]))
        anchors = [h for h in [franchise_ht, *char_hts] if h]
        for e in entries:
            if len(passthrough) >= limit:
                break
            if e["status"] != "pass" or e["ht"].lower() in taken:
                continue
            if _redundant(e["ht"], anchors):
                continue
            taken.add(e["ht"].lower())
            passthrough.append(e["ht"])

    category = themes + passthrough
    category = [f for f in o["fixed"] if f.lower() not in taken] + category
    if not category:
        category = [f for f in o["fallback"] if f.lower() not in taken]

    hashtags: list[str] = []
    if franchise_ht:
        hashtags.append(franchise_ht)
    hashtags += char_hts
    hashtags += category

    final = {h.lower() for h in hashtags}
    decisions = []
    for e in entries:
        if e.get("from_title"):
            continue
        used = bool(e["ht"]) and e["ht"].lower() in final
        if e["status"] == "franchise":
            used = e["tag"] == franchise_tag
        elif e["status"] == "theme":
            used = e["ht"] in themes
        elif e["status"] == "pass":
            # only the tag that really got printed; a repeat of the franchise/character does not count
            used = e["ht"] in passthrough
        decisions.append({"tag": e["tag"], "en": e["en"], "status": e["status"], "hashtag": e["ht"], "used": used})

    return {
        "adult": adult,
        "characters": char_names,
        "character_hashtags": char_hts,
        "franchise": franchise_name,
        "franchise_hashtag": franchise_ht,
        "themes": themes,
        "passthrough": passthrough,
        "hashtags": hashtags,
        "decisions": decisions,
        "unknown": [],  # kept for old callers; tag_stats.csv replaced unknown_tags.csv
    }


def format_trace(result: dict) -> str:
    """Dry-run text: what each Pixiv tag became (✓ = printed in the caption)."""
    lines = ["TAGS " + " ".join(result["hashtags"])]
    for d in result["decisions"]:
        en = f" ({d['en']})" if d["en"] else ""
        ht = f" -> {d['hashtag']}" if d["hashtag"] else ""
        lines.append(f"  {'✓' if d['used'] else '·'} {d['status']:9} {d['tag']!r}{en}{ht}")
    return "\n".join(lines)


# ---- tag statistics (data/tag_stats.csv): the dataset for tuning the rules ----

STATS_FIELDS = ["tag", "translation", "count", "nsfw_count", "status", "used_count", "last_seen"]


def load_stats(path: Path) -> dict[str, dict]:
    data: dict[str, dict] = {}
    if not path.exists():
        return data
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tag = (row.get("tag") or "").strip()
            if not tag:
                continue
            data[tag] = {
                "translation": (row.get("translation") or "").strip(),
                "count": int(row.get("count") or 0),
                "nsfw_count": int(row.get("nsfw_count") or 0),
                "status": (row.get("status") or "").strip(),
                "used_count": int(row.get("used_count") or 0),
                "last_seen": (row.get("last_seen") or "").strip(),
            }
    return data


def save_stats(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(data.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STATS_FIELDS)
        writer.writeheader()
        for tag, meta in rows:
            writer.writerow({"tag": tag, **meta})


def record_tag_stats(path: Path, decisions: list[dict], nsfw: bool, today: str | None = None) -> None:
    """One post = one count per tag. used_count = times the tag reached the caption."""
    today = today or date.today().isoformat()
    data = load_stats(path)
    for d in decisions:
        row = data.setdefault(
            d["tag"],
            {"translation": "", "count": 0, "nsfw_count": 0, "status": d["status"], "used_count": 0, "last_seen": today},
        )
        row["translation"] = d["en"] or row["translation"]
        row["count"] += 1
        row["nsfw_count"] += 1 if nsfw else 0
        row["status"] = d["status"]
        row["used_count"] += 1 if d["used"] else 0
        row["last_seen"] = today
    save_stats(path, data)
