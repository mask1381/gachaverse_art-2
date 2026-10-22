from __future__ import annotations

"""Posts that must never be published, whatever the artist rules say.

data/blocked_content.csv (kind,value):
  stem  - substring match on a lowercased text with separators removed
  regex - regular expression on the lowercased text

Each text (title, tag, tag translation) is checked on its own, so words never
glue together across texts.
"""

import csv
import logging
import re
from pathlib import Path

from lab.config import DATA_DIR

log = logging.getLogger(__name__)

_SEP = re.compile(r"[\s_\-./:'’・]+")
_RULES: dict | None = None


def load_blocklist(path: Path) -> dict:
    out: dict = {"stems": [], "regexes": []}
    if not path.exists():
        log.warning("Blocklist file missing: %s", path)
        return out
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            kind = (row.get("kind") or "").strip().lower()
            value = (row.get("value") or "").strip()
            if not value:
                continue
            if kind == "stem":
                stem = _SEP.sub("", value.lower())
                if stem:
                    out["stems"].append(stem)
            elif kind == "regex":
                try:
                    out["regexes"].append(re.compile(value, re.I))
                except re.error as exc:
                    log.warning("Bad regex in blocked_content.csv %r: %s", value, exc)
    return out


def get_blocklist() -> dict:
    global _RULES
    if _RULES is None:
        _RULES = load_blocklist(DATA_DIR / "blocked_content.csv")
    return _RULES


def match_blocked(texts) -> str | None:
    """First blocklist rule that matches any of the texts, else None."""
    rules = get_blocklist()
    for text in texts:
        raw = str(text or "").lower().replace("_", " ")
        if not raw.strip():
            continue
        compact = _SEP.sub("", raw)
        for stem in rules["stems"]:
            if stem in compact:
                return stem
        for rx in rules["regexes"]:
            if rx.search(raw):
                return rx.pattern
    return None


def item_texts(item: dict) -> list[str]:
    """Every text of a post/page that can reveal blocked content."""
    out: list = [item.get("title")]
    for key in ("generalTags", "tagsRaw", "all_tags"):
        val = item.get(key)
        if isinstance(val, list):
            out.extend(val)
    translations = item.get("tagTranslations")
    if isinstance(translations, dict):
        out.extend(translations.values())
    return [str(x) for x in out if x]


def blocked_reason(item: dict) -> str | None:
    return match_blocked(item_texts(item))
