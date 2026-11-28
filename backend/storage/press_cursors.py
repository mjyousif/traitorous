"""Per-seat, per-channel press read cursor persistence in `public.press_read_cursors`."""

import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from domain.board import Power
from storage import supabase
from storage.press import _is_saved_game
from storage.supabase import SupabaseUnavailable
from telemetry import record_supabase_query

logger = logging.getLogger("diplomacy.server")

# In-memory read cursors: game_id -> power -> channel_key -> last_read_seq
GAME_PRESS_CURSORS: dict[str, dict[Power, dict[str, int]]] = {}
PRESS_CURSORS_UNSAVED: set[str] = set()

_CURSOR_SAVES_RUNNING: dict[str, bool] = {}
_PERSISTED_CURSORS: dict[str, dict[tuple[Power, str], int]] = {}


def clear_press_cursors(game_id: str) -> None:
    """Clear in-memory read cursors and persistence watermarks for `game_id`."""
    GAME_PRESS_CURSORS.pop(game_id, None)
    PRESS_CURSORS_UNSAVED.discard(game_id)
    _PERSISTED_CURSORS.pop(game_id, None)


def has_unsaved_press_cursors(game_id: str) -> bool:
    """True while memory holds cursor advances for `game_id` not yet stored in Supabase."""
    return game_id in PRESS_CURSORS_UNSAVED


async def load_press_cursors_from_db(
    game_id: str, client: httpx.AsyncClient | None = None
) -> dict[Power, dict[str, int]]:
    """Load saved press read cursors for `game_id`. Raise SupabaseUnavailable on read failure."""
    db_id = await supabase.resolve_game_db_id(game_id, client=client)
    if not db_id:
        return {}

    url = (
        f"{supabase.get_supabase_url()}/rest/v1/press_read_cursors"
        f"?game_id=eq.{quote(db_id, safe='')}"
        f"&select=power,channel_key,last_read_seq"
    )
    headers = supabase.get_headers()
    start_time = time.perf_counter()
    try:
        c = client or supabase.get_http_client()
        resp = await c.get(url, headers=headers, timeout=10.0)
    except Exception as ex:
        record_supabase_query(
            "load_press_cursors", "error", 0, (time.perf_counter() - start_time) * 1000.0
        )
        raise SupabaseUnavailable(str(ex)) from ex

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    if resp.status_code != 200:
        record_supabase_query("load_press_cursors", "error", resp.status_code, elapsed_ms)
        raise SupabaseUnavailable(f"HTTP {resp.status_code} {resp.text}")
    rows = resp.json()
    if not isinstance(rows, list):
        record_supabase_query("load_press_cursors", "error", resp.status_code, elapsed_ms)
        raise SupabaseUnavailable(f"Unexpected response: {rows!r}")

    cursors: dict[Power, dict[str, int]] = {}
    persisted: dict[tuple[Power, str], int] = {}
    for row in rows:
        try:
            power = Power(row["power"])
            channel = str(row["channel_key"])
            seq = int(row["last_read_seq"])
            if not channel or seq < 0:
                continue
        except Exception as ex:
            logger.debug("Skipping unparseable press cursor row for %s: %s", game_id, ex)
            continue
        power_map = cursors.setdefault(power, {})
        power_map[channel] = max(power_map.get(channel, 0), seq)
        persisted[(power, channel)] = max(persisted.get((power, channel), 0), seq)

    if persisted:
        _PERSISTED_CURSORS[game_id] = persisted
    record_supabase_query("load_press_cursors", "ok", resp.status_code, elapsed_ms)
    return cursors


async def save_press_cursors_to_db(
    game_id: str,
    cursors: dict[Power, dict[str, int]],
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Incrementally upsert advanced read cursors to `public.press_read_cursors`."""
    try:
        db_id = await supabase.resolve_game_db_id(game_id, client=client)
    except SupabaseUnavailable:
        return False
    if not db_id:
        return False

    persisted = _PERSISTED_CURSORS.get(game_id, {})
    now_iso = datetime.now(timezone.utc).isoformat()
    dirty_rows: list[dict[str, Any]] = []
    dirty_pairs: list[tuple[Power, str, int]] = []
    for power, channels in cursors.items():
        for channel, seq in channels.items():
            seq_int = int(seq)
            if seq_int > persisted.get((power, channel), 0):
                dirty_rows.append(
                    {
                        "game_id": db_id,
                        "power": power.value,
                        "channel_key": channel,
                        "last_read_seq": seq_int,
                        "updated_at": now_iso,
                    }
                )
                dirty_pairs.append((power, channel, seq_int))

    if not dirty_rows:
        return True

    c = client or supabase.get_http_client()
    start_time = time.perf_counter()
    try:
        headers = {**supabase.get_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
        url = f"{supabase.get_supabase_url()}/rest/v1/press_read_cursors?on_conflict=game_id,power,channel_key"
        resp = await c.post(url, headers=headers, json=dirty_rows, timeout=10.0)
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        if resp.status_code not in (200, 201, 204):
            record_supabase_query("save_press_cursors", "error", resp.status_code, elapsed_ms)
            logger.warning(
                "Supabase press cursors save failed for '%s': HTTP %d %s",
                game_id,
                resp.status_code,
                resp.text,
            )
            return False
        cache = _PERSISTED_CURSORS.setdefault(game_id, {})
        for power, channel, seq_int in dirty_pairs:
            cache[(power, channel)] = max(cache.get((power, channel), 0), seq_int)
        record_supabase_query("save_press_cursors", "ok", resp.status_code, elapsed_ms)
        return True
    except Exception as ex:
        record_supabase_query(
            "save_press_cursors", "error", 0, (time.perf_counter() - start_time) * 1000.0
        )
        logger.warning("Supabase exception saving press cursors for '%s': %s", game_id, ex)
        return False


async def press_cursors_of(game_id: str) -> dict[Power, dict[str, int]]:
    """Return the live read-cursor map for `game_id`, lazy-loading from Supabase on first read."""
    if game_id not in GAME_PRESS_CURSORS:
        loaded = await load_press_cursors_from_db(game_id) if _is_saved_game(game_id) else {}
        existing = GAME_PRESS_CURSORS.setdefault(game_id, {})
        for power, channels in loaded.items():
            target = existing.setdefault(power, {})
            for channel, seq in channels.items():
                target[channel] = max(target.get(channel, 0), seq)
    return GAME_PRESS_CURSORS[game_id]


def cursors_for_power(game_id: str, power: Power) -> dict[str, int]:
    """Return a copy of `power`'s current per-channel read cursors for `game_id`."""
    return dict(GAME_PRESS_CURSORS.get(game_id, {}).get(power, {}))


def advance_press_cursor(game_id: str, power: Power, channel: str, seq: int) -> bool:
    """Monotonically advance `power`'s read cursor for `channel` to `seq`. Returns True if advanced."""
    if seq <= 0:
        return False
    by_power = GAME_PRESS_CURSORS.setdefault(game_id, {})
    by_channel = by_power.setdefault(power, {})
    if seq <= by_channel.get(channel, 0):
        return False
    by_channel[channel] = seq
    return True


async def persist_press_cursors(game_id: str) -> None:
    """Save read cursors incrementally with per-game coalescing."""
    from storage.store import GAMES, PRIVATE_SANDBOXES, UNSAVED_GAMES, persist_game

    if not _is_saved_game(game_id) or game_id in PRIVATE_SANDBOXES or game_id not in GAMES:
        return
    PRESS_CURSORS_UNSAVED.add(game_id)
    if game_id in _CURSOR_SAVES_RUNNING:
        _CURSOR_SAVES_RUNNING[game_id] = True
        return

    saved = False
    try:
        repeat = True
        while repeat:
            _CURSOR_SAVES_RUNNING[game_id] = False
            if game_id in UNSAVED_GAMES:
                await persist_game(game_id)
            cursors = GAME_PRESS_CURSORS.get(game_id)
            if cursors is None:
                return
            snapshot = {power: dict(ch_map) for power, ch_map in cursors.items()}
            saved = await save_press_cursors_to_db(game_id, snapshot)
            repeat = _CURSOR_SAVES_RUNNING[game_id]
    finally:
        _CURSOR_SAVES_RUNNING.pop(game_id, None)
    if saved:
        PRESS_CURSORS_UNSAVED.discard(game_id)
