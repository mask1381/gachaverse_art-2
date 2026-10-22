from __future__ import annotations

"""Game gate: only Genshin Impact and Wuthering Waves art is posted."""

import re

GAMES: dict[str, dict] = {
    "Genshin Impact": {"hashtag": "#GenshinImpact", "aliases": ["原神", "genshinimpact", "genshin"]},
    "Wuthering Waves": {"hashtag": "#WutheringWaves", "aliases": ["鳴潮", "鸣潮", "wutheringwaves", "wuwa"]},
}

# Short hashtag + display name for franchises whose full dictionary name is too long.
# Key = the franchise name with everything but letters/digits removed, lowercase.
# Names that are not listed keep their old behaviour (full name -> hashtag, name as the display).
FRANCHISE_SHORT: dict[str, tuple[str, str]] = {
    "nikkethegoddessofvictory": ("#NIKKE", "NIKKE"),
    "goddessofvictorynikke": ("#NIKKE", "NIKKE"),
}


def _n(s) -> str:
    return re.sub(r"[\W_]+", "", str(s or "").lower())


def short_franchise(name) -> tuple[str, str] | None:
    """(hashtag, display name) from FRANCHISE_SHORT, or None."""
    return FRANCHISE_SHORT.get(_n(name))


def franchise_display(name) -> str:
    """Name used in the caption title: the short name if listed, else the name as it is."""
    hit = short_franchise(name)
    return hit[1] if hit else str(name or "").strip()


def detect_game(pairs) -> str | None:
    """pairs: [(pixiv tag, English translation)] -> allowed game name or None.

    Short ASCII aliases must match the whole tag (so 'wuwa' does not hit random words);
    long ones and CJK aliases may appear inside a longer tag ('原神10000users入り').
    """
    norms = [_n(x) for p in pairs for x in p if x]
    for game, g in GAMES.items():
        for a in g["aliases"]:
            na = _n(a)
            if not na:
                continue
            ascii_alias = na.isascii()
            for n in norms:
                if n == na or (n and na in n and (not ascii_alias or len(na) >= 6)):
                    return game
    return None


def game_hashtag(game: str | None) -> str | None:
    return (GAMES.get(game or "") or {}).get("hashtag")
