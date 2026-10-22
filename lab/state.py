from __future__ import annotations

"""State lives in the repo's existing files so the old flows and this one share it.

data/sent_ids.json  {"ids": ["pixiv:123", "zerochan:9", ...]}   every id ever sent / scheduled / skipped
data/state.json     {"recentlyUsed": [...], "failures": {...}, "planRecent": [...], "recentCaptions": {...}}

Ids of other sources are kept untouched. Legacy pixiv ids ("123_p0") count as "pixiv:123".
Writes are atomic (temp file + replace).
"""

from datetime import date
import json
import os
import re
import tempfile
from pathlib import Path

_LEGACY = re.compile(r"^(\d+)_p\d+$")


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":") if path.name == "sent_ids.json" else None,
                      indent=None if path.name == "sent_ids.json" else 2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_runtime(path: Path) -> dict:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            data = {}
    data.setdefault("recentlyUsed", [])
    data.setdefault("recentCaptions", {})
    data.setdefault("failures", {})
    return data


def save_runtime(path: Path, data: dict) -> None:
    _atomic_write(path, data)


def load_posted(path: Path) -> dict[str, str]:
    """{id: note}. The note is not stored on disk (ids only)."""
    posted: dict[str, str] = {}
    if not path.exists():
        return posted
    try:
        ids = (json.loads(path.read_text(encoding="utf-8")) or {}).get("ids", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is corrupt: {exc}") from exc
    for i in ids:
        i = str(i)
        posted[i] = ""
        m = _LEGACY.match(i)
        if m:
            posted.setdefault(f"pixiv:{m.group(1)}", "")
    return posted


def save_posted(path: Path, posted: dict[str, str]) -> None:
    _atomic_write(path, {"ids": sorted(posted)})


def select_artist(artists: list[dict], runtime: dict) -> dict:
    import random

    if not artists:
        raise RuntimeError("Artist list is empty")
    recently = list(runtime.get("recentlyUsed") or [])
    pool = [a for a in artists if a["user_id"] not in recently] or artists
    selected = random.choice(pool)
    recently = [*[r for r in recently if r != selected["user_id"]], selected["user_id"]]
    runtime["recentlyUsed"] = recently[-max(1, len(artists) - 3):]
    return selected


class State:
    """In-memory state seeded from the batch. Nothing is written to disk:
    changes go to the bot through `sink` (acks in the private group)."""

    STATS_GROUP = 150

    def __init__(self, posted: dict | list, runtime: dict, sink=None, dry: bool = False):
        if isinstance(posted, list):
            self._posted = {str(k): "" for k in posted}
        elif isinstance(posted, dict):
            self._posted = dict(posted)
        else:
            self._posted = {}
        self._runtime = dict(runtime or {})
        self._runtime.setdefault("recentlyUsed", [])
        self._runtime.setdefault("recentCaptions", {})
        self._runtime.setdefault("failures", {})
        self._synced = dict(self._posted)
        self.sink = sink
        self.dry = dry
        self.stats: dict[str, list] = {}

    def posted(self) -> dict:
        return self._posted

    def runtime(self) -> dict:
        return self._runtime

    def save(self, posted, runtime) -> None:
        if runtime is not None:
            self._runtime = runtime
        if posted is None or self.dry or not self.sink:
            return
        delta = {k: v for k, v in posted.items() if self._synced.get(k) != v}
        if delta:
            self.sink.posted(delta)
            self._synced.update(delta)

    def add_stats(self, decisions: list[dict], nsfw: bool) -> None:
        today = date.today().isoformat()
        for d in decisions:
            row = self.stats.setdefault(d["tag"], ["", 0, 0, d.get("status", ""), 0, today])
            if d.get("en"):
                row[0] = d["en"]
            row[1] += 1
            row[2] += 1 if nsfw else 0
            row[3] = d.get("status", "")
            row[4] += 1 if d.get("used") else 0

    def flush(self) -> None:
        if self.dry or not self.sink:
            return
        self.save(self._posted, self._runtime)
        self.sink.blob("rt", self._runtime)
        rows = [[tag, *r] for tag, r in self.stats.items()]
        for i in range(0, len(rows), self.STATS_GROUP):
            self.sink.blob(f"st{i // self.STATS_GROUP}", rows[i:i + self.STATS_GROUP])

