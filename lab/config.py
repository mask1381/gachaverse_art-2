from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

PIXIV_OAUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_BASE = "https://app-api.pixiv.net/v1"
PIXIV_CLIENT_ID = os.getenv("PIXIV_CLIENT_ID") or "MOBrBDS8blbauoSck0ZfDbtuzpyT"  # public app constants
PIXIV_CLIENT_SECRET = os.getenv("PIXIV_CLIENT_SECRET") or "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAPH_API_BASE = "https://api.telegra.ph"

MAX_ALBUM_SIZE = 10
MAX_IMAGE_EDGE = 1920
JPEG_QUALITY = 80
MAX_PIXIV_PAGES = 3
TELEGRAM_MAX_BYTES = 9_500_000
REQUEST_TIMEOUT = 20.0


def _settings() -> dict:
    path = CONFIG_DIR / "settings.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def load_artists() -> list[dict]:
    sources = json.loads((CONFIG_DIR / "sources.json").read_text(encoding="utf-8"))
    for src in sources.get("sources", []):
        if src.get("id") == "pixiv":
            return list(src.get("params", {}).get("artists") or [])
    return []


def _env(*names: str) -> str:
    """First non-empty env var among names (GachaVerse uses TG_*, waifu-lab used TELEGRAM_*)."""
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


def env_config(batch: dict | None = None) -> dict:
    refresh = _env("PIXIV_REFRESH_TOKEN")
    bot = _env("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    telegraph = _env("TELEGRAPH_TOKEN")

    if batch is not None and isinstance(batch, dict):
        chat = str(batch.get("target_chat_id") or "").strip()
        dry_run = bool(batch.get("dry"))
        artists = list(batch.get("artists") or [])
    else:
        settings = _settings()
        chat = _env("TG_CHAT_ID", "TELEGRAM_CHAT_ID") or str(settings.get("defaultChatId") or "").strip()
        dry_run = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}
        artists = load_artists()

    missing = [
        name
        for name, value in [
            ("PIXIV_REFRESH_TOKEN", refresh),
            ("TG_BOT_TOKEN", bot),
            ("TG_CHAT_ID", chat),
        ]
        if not value and not dry_run
    ]
    if missing:
        raise SystemExit(
            "Missing required env: "
            + ", ".join(missing)
        )

    return {
        "pixiv_refresh_token": refresh,
        "telegram_bot_token": bot,
        "telegram_chat_id": chat,
        "telegraph_token": telegraph,
        "dry_run": dry_run,
        "artists": artists,
        "dictionary_path": DATA_DIR / "character_dictionary.csv",
        "posted_path": DATA_DIR / "sent_ids.json",
        "runtime_path": DATA_DIR / "state.json",
        "content_tags_path": DATA_DIR / "content_tags.csv",
        "adult_tags_path": DATA_DIR / "adult_tags.csv",
        "drop_tags_path": DATA_DIR / "drop_tags.csv",
        "tag_stats_path": DATA_DIR / "tag_stats.csv",
    }


# Extra Instant View-friendly image hosts for the Telegraph page (first = personal Worker if set).
PIXIV_PROXY_HOST = (os.getenv("PIXIV_PROXY_HOST") or "").strip()
