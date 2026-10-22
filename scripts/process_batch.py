#!/usr/bin/env python3
"""Run one GachaVerse Art batch handed over by the bot through the private Telegram group.

The repo is public: stdout only gets counters. All pipeline logs go to a buffer and are
sent to the private group (dry runs and errors only). State changes go back as acks:
  /ack <batch> gp <id>=<value> ...          posted ids
  /ack <batch> blob <kind> <i> <n> <chunk>  runtime ("rt") / tag stats ("st0", "st1", ...)
  /ack <batch> done
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOG_BUF = io.StringIO()
_handler = logging.StreamHandler(LOG_BUF)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().handlers[:] = [_handler]
logging.getLogger().setLevel(logging.INFO)

from telethon import TelegramClient              # noqa: E402
from telethon.errors import FloodWaitError       # noqa: E402
from telethon.sessions import StringSession      # noqa: E402

CHUNK = 3000        # chars per ack message (Telegram limit is 4096)
ACK_GAP = 1.2       # seconds between messages to the group


async def _connect():
    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    return client


async def _resolve(client, chat_id):
    value = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
    try:
        return await client.get_input_entity(value)
    except ValueError:
        await client.get_dialogs()
        return await client.get_input_entity(value)


async def _fetch_batch(chat_id, message_id):
    client = await _connect()
    try:
        entity = await _resolve(client, chat_id)
        msg = await client.get_messages(entity, ids=int(message_id))
        if not msg or not msg.media:
            raise RuntimeError("batch message not found")
        data = await client.download_media(msg, file=bytes)
        return json.loads(data.decode("utf-8"))
    finally:
        await client.disconnect()


async def _say(chat_id, text):
    client = await _connect()
    try:
        entity = await _resolve(client, chat_id)
        await client.send_message(entity, text, parse_mode=None)
    finally:
        await client.disconnect()


def say(chat_id, text) -> bool:
    for _ in range(3):
        try:
            asyncio.run(_say(chat_id, str(text)[:4000]))
            return True
        except FloodWaitError as e:
            time.sleep(min(e.seconds + 1, 60))
        except Exception as e:
            print(f"say failed: {type(e).__name__}")
            time.sleep(3)
    return False


class AckSink:
    def __init__(self, chat, batch_id):
        self.chat = chat
        self.batch_id = batch_id

    def _send(self, text):
        say(self.chat, text)
        time.sleep(ACK_GAP)

    def posted(self, delta: dict) -> None:
        cur, size = [], 0
        for k, v in delta.items():
            item = f"{k}={v}"
            if cur and size + len(item) + 1 > CHUNK:
                self._send(f"/ack {self.batch_id} gp {' '.join(cur)}")
                cur, size = [], 0
            cur.append(item)
            size += len(item) + 1
        if cur:
            self._send(f"/ack {self.batch_id} gp {' '.join(cur)}")

    def blob(self, kind: str, data) -> None:
        raw = json.dumps(data, separators=(",", ":")).replace(" ", "\\u0020")
        parts = [raw[i:i + CHUNK] for i in range(0, len(raw), CHUNK)] or [raw]
        for i, part in enumerate(parts, start=1):
            self._send(f"/ack {self.batch_id} blob {kind} {i} {len(parts)} {part}")


def send_log(chat, header: str, limit: int) -> None:
    text = LOG_BUF.getvalue()[-limit:]
    if not text:
        return
    for i in range(0, len(text), 3800):
        say(chat, f"{header}\n{text[i:i + 3800]}")
        time.sleep(ACK_GAP)


def run(batch: dict, sink: AckSink) -> int:
    from lab import pipeline
    from lab.config import env_config
    from lab.plan import run_plan
    from lab.state import State

    cfg = env_config(batch)
    state = State(batch.get("posted"), batch.get("runtime"), sink, dry=cfg["dry_run"])
    cfg["state"] = state
    try:
        if batch.get("mode") == "now":
            return pipeline.run_round(cfg)
        return run_plan(cfg, cfg["dry_run"])
    finally:
        state.flush()


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--message-id", required=True)
    args = ap.parse_args()

    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING", "BATCH_CHAT_ID"):
        if not os.environ.get(k, "").strip():
            print(f"missing env: {k}")
            return 1
    chat = os.environ["BATCH_CHAT_ID"].strip()

    try:
        batch = asyncio.run(_fetch_batch(chat, args.message_id))
        if batch.get("batch_id") != args.batch_id or batch.get("kind") != "gachaverse":
            raise RuntimeError("bad batch")
    except Exception as e:
        print(f"batch load failed: {type(e).__name__}")
        say(chat, f"❌ {args.batch_id} could not load: {type(e).__name__}: {str(e)[:300]}")
        return 1

    print(f"batch loaded: mode={batch.get('mode')} dry={bool(batch.get('dry'))}")
    sink = AckSink(chat, args.batch_id)
    err = None
    try:
        run(batch, sink)
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        err = e

    if batch.get("dry") or err is not None:
        head = f"🧾 {args.batch_id}" + (f" ❌ {type(err).__name__}: {str(err)[:300]}" if err else " (dry)")
        send_log(chat, head, 12000 if batch.get("dry") else 3000)
    say(chat, f"/ack {args.batch_id} done")
    print(f"finished: error={type(err).__name__ if err else None}")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
