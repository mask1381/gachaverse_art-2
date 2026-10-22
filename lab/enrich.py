from __future__ import annotations

import csv
import re
from pathlib import Path
from urllib.parse import unquote

MIN_SEEN_SINGLE = 3
MIN_SEEN_WEAK_NS = 2
DENY = {
    "shade", "shadow", "pudding", "chair", "flower", "flowers", "smile", "star", "stars",
    "ribbon", "dress", "skirt", "bell", "heart", "cake", "feet", "wings", "halo", "gem",
    "ring", "boots", "hat", "jacket", "necklace", "earrings", "gloves", "tights", "fruit",
    "navel", "bird",
    # generic words that collide with franchise names in the dictionary
    "girl", "girls", "woman", "female", "original", "lady", "person", "character", "solo",
}
PAREN_JUNK = re.compile(r"cosplay|meme|outfit|alternate|default|wallpaper")


# category values that are a content type, not a franchise (e.g. Pixiv always uses "illust")
GENERIC_CATEGORIES = {"illust", "manga", "ugoira", "pixiv", "best_of_site"}

def norm_key(s) -> str:
    text = str(s or "").strip()
    try:
        text = unquote(text)
    except Exception:
        pass
    text = text.lower().replace(":", "").replace("+", "_")
    text = re.sub(r"[\s_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def split_paren(norm: str):
    i = norm.find("_(")
    if i < 0 or not norm.endswith(")"):
        return None
    inside = re.sub(r"[()]", "_", norm[i + 2 : -1])
    inside = re.sub(r"_+", "_", inside).strip("_")
    return {"head": norm[:i], "inside": inside}


def pretty(head: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in head.split("_") if w)


NULL_VALUES = {"", "null", "none", "unknown", "n/a", "nan"}


def clean_value(v) -> str:
    """Dictionary cells may hold the literal string 'null'; treat those as empty."""
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in NULL_VALUES else s


def _sorted_key(k: str):
    """Word-order-insensitive key for multi-word names ('Marin Kitagawa' == 'Kitagawa Marin')."""
    tokens = [t for t in k.split("_") if t]
    if len(tokens) < 2 or "(" in k:
        return None
    return "~" + "_".join(sorted(tokens))


def _compact_keys(k: str) -> list[str]:
    """Run-together forms of a name: 'yuki_suou' -> 'yukisuou' (+ reversed order for 2 words)."""
    tokens = [t for t in k.split("_") if t]
    if len(tokens) < 2 or "(" in k:
        return []
    forms = ["".join(tokens)]
    if len(tokens) == 2:
        forms.append(tokens[1] + tokens[0])
    return [f for f in forms if len(f) >= 7]


def franchise_total(name: str) -> int:
    """Sum of seen_count over all dictionary rows of a franchise (0 if unknown)."""
    if _MAPS is None:
        return 0
    return _MAPS[0].get("franchise_total", {}).get(norm_key(name), 0)


def load_dictionary_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build_maps(rows: list[dict]):
    maps = {
        "character": {},
        "franchise": {},
        "studio": {},
        "source": {},
    }
    character_maps_by_category: dict[str, dict] = {}
    franchise_total_map: dict[str, int] = {}
    maps["franchise_total"] = franchise_total_map

    def put(store: dict, key: str, original: str, row: dict, seen: int):
        prev = store.get(key)
        if not prev or prev["seen"] < seen:
            store[key] = {"original": original, "row": row, "seen": seen}

    for row in rows:
        seen = int(row.get("seen_count") or 0)
        franchise_values = []
        for col in ("franchise", "franchise_en", "franchise_jp"):
            val = clean_value(row.get(col))
            if val and val not in franchise_values:
                franchise_values.append(val)
        franchise_norms = list({norm_key(v) for v in franchise_values if norm_key(v)})
        for fn in franchise_norms:
            franchise_total_map[fn] = franchise_total_map.get(fn, 0) + seen
            
        for original in franchise_values:
            norm = norm_key(original)
            if not norm:
                continue
            keys = [norm]
            sp = split_paren(norm)
            if sp and sp["head"]:
                keys.append(sp["head"])
            for k in keys:
                if k in DENY:
                    continue
                if "_" not in k and seen < MIN_SEEN_WEAK_NS:
                    continue
                put(maps["franchise"], k, original, row, seen)
                ck = re.sub(r"[_\-]", "", k)  # 'GenshinImpact' == 'genshin_impact'
                if ck != k and len(ck) >= 8 and ck not in DENY:
                    put(maps["franchise"], ck, original, row, seen)

        for ns in ("character", "studio", "source"):
            raw = clean_value(row.get(ns))
            if not raw:
                continue
            values = str(raw).split(";") if ns == "source" else [raw]
            for v in values:
                original = str(v).strip()
                norm = norm_key(original)
                if not norm:
                    continue
                keys = [norm]
                sp = split_paren(norm)
                if sp and sp["head"]:
                    keys.append(sp["head"])
                for k in keys:
                    if k in DENY:
                        continue
                    min_seen = MIN_SEEN_SINGLE if ns == "character" else MIN_SEEN_WEAK_NS
                    if "_" not in k and seen < min_seen:
                        continue
                    put(maps[ns], k, original, row, seen)
                    if ns == "character":
                        extra = [x for x in [_sorted_key(k), *_compact_keys(k)] if x]
                        for x in extra:
                            put(maps[ns], x, original, row, seen)
                        for fn in franchise_norms:
                            cat = character_maps_by_category.setdefault(fn, {})
                            put(cat, k, original, row, seen)
                            for x in extra:
                                put(cat, x, original, row, seen)
    return maps, character_maps_by_category


def _lookup_tag(norm: str, ns_order, allow_fuzzy: bool):
    for ns, store in ns_order:
        hit = store.get(norm)
        if hit:
            return ns, hit
    sp = split_paren(norm)
    if sp and sp["head"]:
        for ns, store in ns_order:
            hit = store.get(sp["head"])
            if hit:
                return ns, hit
    for cand in (norm, sp["head"] if sp else None):
        sk = _sorted_key(cand) if cand else None
        if sk:
            for ns, store in ns_order:
                if ns == "character" and store.get(sk):
                    return ns, store[sk]
    if allow_fuzzy:
        tag_tokens = set(t for t in norm.split("_") if t)
        if tag_tokens:
            best = None
            for ns, store in ns_order:
                if ns != "character":
                    continue
                for key, hit in store.items():
                    key_tokens = [t for t in key.split("_") if t]
                    if not key_tokens:
                        continue
                    key_set = set(key_tokens)
                    smaller = tag_tokens if len(tag_tokens) <= len(key_set) else key_set
                    larger = key_set if len(tag_tokens) <= len(key_set) else tag_tokens
                    if all(t in larger for t in smaller):
                        if not best or hit["seen"] > best[1]["seen"]:
                            best = (ns, hit)
            if best:
                return best
    return None


_MAPS = None


def get_maps(rows: list[dict]):
    global _MAPS
    if _MAPS is None:
        _MAPS = build_maps(rows)
    return _MAPS


def enrich_posts(posts: list[dict], rows: list[dict]) -> list[dict]:
    maps, character_maps_by_category = get_maps(rows)
    ns_order_fallback = [
        ("character", maps["character"]),
        ("franchise", maps["franchise"]),
        ("studio", maps["studio"]),
        ("source", maps["source"]),
    ]
    franchise_keys = set(maps["franchise"].keys())

    def strip_franchise_prefix(inside: str) -> str:
        for k in franchise_keys:
            if inside.startswith(k + "_"):
                return inside[len(k) + 1 :]
        return inside

    def is_franchise_ref(inside: str) -> bool:
        if inside in franchise_keys:
            return True
        return any(inside.startswith(k + "_") for k in franchise_keys)

    out = []
    for p in posts:
        is_zerochan = str(p.get("source") or "").lower() == "zerochan"
        post_category_norm = norm_key(p.get("category") or "")
        filtered = character_maps_by_category.get(post_category_norm) if post_category_norm else None
        ns_order = (
            [("character", filtered), *ns_order_fallback[1:]] if filtered else ns_order_fallback
        )

        raw_tags = []
        for key in ("tagsRaw", "generalTags", "tags"):
            val = p.get(key)
            if isinstance(val, list):
                raw_tags.extend(val)

        seen_norm = set()
        unique_raw = []
        for t in raw_tags:
            s = str(t)
            n = norm_key(s)
            if not n or n in seen_norm:
                continue
            seen_norm.add(n)
            unique_raw.append(s)

        buckets = {"character": [], "franchise": [], "studio": [], "source": [], "general": []}
        in_resolved = {k: set() for k in ("character", "franchise", "studio", "source")}
        in_general = set()
        char_row = None
        extracted = []
        extracted_set = set()

        for raw_tag in unique_raw:
            norm = norm_key(raw_tag)
            hit = _lookup_tag(norm, ns_order, bool(filtered))
            if hit:
                ns, rec = hit
                resolved_key = norm_key(rec["original"])
                if resolved_key not in in_resolved[ns]:
                    in_resolved[ns].add(resolved_key)
                    buckets[ns].append(rec["original"])
                if ns == "character" and char_row is None:
                    char_row = rec["row"]
                continue
            sp = split_paren(norm)
            if (
                sp
                and len(sp["head"]) > 1
                and "_" not in sp["head"]
                and sp["head"] not in DENY
                and is_franchise_ref(sp["inside"])
                and not PAREN_JUNK.search(strip_franchise_prefix(sp["inside"]))
            ):
                name = pretty(sp["head"])
                if name not in extracted_set:
                    extracted_set.add(name)
                    extracted.append(name)
            if norm not in in_general:
                in_general.add(norm)
                buckets["general"].append(raw_tag)

        for name in extracted:
            n = norm_key(name)
            if n not in in_resolved["character"]:
                in_resolved["character"].add(n)
                buckets["character"].append(name)

        character = buckets["character"][0] if buckets["character"] else None
        upstream = None
        if p.get("character") and str(p["character"]).strip() and str(p["character"]).lower() != "unknown":
            upstream = pretty(norm_key(p["character"]))
        if not character and not is_zerochan and upstream:
            character = upstream

        character_list = [character] if character else []
        for c in buckets["character"]:
            if c not in character_list:
                character_list.append(c)

        requested_anime = p.get("requestedAnime")
        zerochan_match = (not is_zerochan) or (not requested_anime) or (norm_key(requested_anime) in seen_norm)

        inferred_franchise = None
        if char_row:
            inferred_franchise = (
                clean_value(char_row.get("franchise_en"))
                or clean_value(char_row.get("franchise"))
                or clean_value(char_row.get("franchise_jp"))
            ) or None
        if not inferred_franchise:
            category = p.get("category")
            category_ok = bool(category) and norm_key(category) not in GENERIC_CATEGORIES
            inferred_franchise = buckets["franchise"][0] if buckets["franchise"] else (
                pretty(norm_key(category)) if category_ok else None
            )

        inferred_studio = clean_value(char_row.get("studio")) if char_row else ""
        inferred_studio = inferred_studio or None

        enriched = {
            **p,
            "zerochan_match": zerochan_match,
            "character": character,
            "character_tags": buckets["character"],
            "franchise_tags": buckets["franchise"],
            "studio_tags": buckets["studio"],
            "source_tags": buckets["source"],
            "general_tags": buckets["general"],
            "generalTags": buckets["general"],
            "inferred_franchise": inferred_franchise,
            "inferred_studio": inferred_studio,
            "franchiseList": buckets["franchise"],
            "studioList": buckets["studio"],
            "characterList": character_list,
            "tagsRaw": p.get("tagsRaw") or [],
            "zerochan_tags": (p.get("zerochan_tags") or p.get("tagsRaw") or []) if is_zerochan else (p.get("zerochan_tags") or []),
            "all_tags": unique_raw,
        }
        out.append(enriched)
    return out
