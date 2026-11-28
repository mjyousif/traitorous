"""Unit tests for press read cursor persistence, channel normalization, and monotonic updates."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from domain.board import Power
from domain.press import normalize_channel_key
from storage.press_cursors import (
    GAME_PRESS_CURSORS,
    advance_press_cursor,
    clear_press_cursors,
    cursors_for_power,
    load_press_cursors_from_db,
    save_press_cursors_to_db,
)
from storage.supabase import GAME_DB_IDS, SupabaseUnavailable

ENV = {"SUPABASE_URL": "https://proj.supabase.co", "SUPABASE_KEY": "test_key"}
DB_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def client_answering(method: str, *responses: httpx.Response) -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    if len(responses) == 1:
        getattr(client, method).return_value = responses[0]
    else:
        getattr(client, method).side_effect = list(responses)
    return client


def run(coro):
    with patch.dict(os.environ, ENV):
        return asyncio.run(coro)


def test_normalize_channel_key_accepts_public_backchannel_and_canonicalizes_threads():
    assert normalize_channel_key("public", Power.FRANCE) == "public"
    assert normalize_channel_key("backchannel", Power.FRANCE) == "backchannel"
    # Frontend ALL_POWERS order puts FRANCE before ENGLAND before GERMANY.
    assert normalize_channel_key("GERMANY+FRANCE", Power.FRANCE) == "FRANCE+GERMANY"
    assert (
        normalize_channel_key("AUSTRIA+GERMANY+ENGLAND", Power.ENGLAND) == "ENGLAND+GERMANY+AUSTRIA"
    )


def test_normalize_channel_key_rejects_foreign_or_malformed_channels():
    assert normalize_channel_key("", Power.FRANCE) is None
    assert normalize_channel_key("FRANCE", Power.FRANCE) is None
    assert normalize_channel_key("ENGLAND+GERMANY", Power.FRANCE) is None
    assert normalize_channel_key("FRANCE+FRANCE", Power.FRANCE) is None
    assert normalize_channel_key("FRANCE+ATLANTIS", Power.FRANCE) is None
    assert normalize_channel_key("random", Power.FRANCE) is None


def test_advance_press_cursor_is_monotonic_and_clearable():
    clear_press_cursors("room_mono")
    try:
        assert advance_press_cursor("room_mono", Power.FRANCE, "public", 0) is False
        assert advance_press_cursor("room_mono", Power.FRANCE, "public", 4) is True
        assert advance_press_cursor("room_mono", Power.FRANCE, "public", 4) is False
        assert advance_press_cursor("room_mono", Power.FRANCE, "public", 2) is False
        assert cursors_for_power("room_mono", Power.FRANCE) == {"public": 4}
        assert advance_press_cursor("room_mono", Power.FRANCE, "public", 7) is True
        assert cursors_for_power("room_mono", Power.FRANCE) == {"public": 7}
        assert "room_mono" in GAME_PRESS_CURSORS
    finally:
        clear_press_cursors("room_mono")
        assert "room_mono" not in GAME_PRESS_CURSORS


def test_load_reads_cursors_and_skips_malformed_rows():
    GAME_DB_IDS["room_1"] = DB_UUID
    clear_press_cursors("room_1")
    try:
        rows = [
            {"power": "FRANCE", "channel_key": "public", "last_read_seq": 3},
            {"power": "FRANCE", "channel_key": "FRANCE+GERMANY", "last_read_seq": 5},
            {"power": "GERMANY", "channel_key": "backchannel", "last_read_seq": 2},
            {"power": "BOGUS", "channel_key": "public", "last_read_seq": 9},
            {"power": "FRANCE", "channel_key": "", "last_read_seq": 9},
            {"power": "FRANCE", "channel_key": "public", "last_read_seq": -1},
        ]
        client = client_answering("get", httpx.Response(200, json=rows))
        loaded = run(load_press_cursors_from_db("room_1", client=client))
        assert loaded == {
            Power.FRANCE: {"public": 3, "FRANCE+GERMANY": 5},
            Power.GERMANY: {"backchannel": 2},
        }
        url = client.get.call_args.args[0]
        assert "/rest/v1/press_read_cursors" in url
        assert f"game_id=eq.{DB_UUID}" in url
    finally:
        GAME_DB_IDS.pop("room_1", None)
        clear_press_cursors("room_1")


def test_load_returns_empty_for_missing_game_and_raises_on_error():
    GAME_DB_IDS.pop("x", None)
    clear_press_cursors("x")
    try:
        no_game = client_answering("get", httpx.Response(200, json=[]))
        assert run(load_press_cursors_from_db("x", client=no_game)) == {}

        GAME_DB_IDS["x"] = DB_UUID
        with pytest.raises(SupabaseUnavailable):
            run(
                load_press_cursors_from_db(
                    "x", client=client_answering("get", httpx.Response(503, text="down"))
                )
            )
        unreachable = AsyncMock(spec=httpx.AsyncClient)
        unreachable.get.side_effect = httpx.ConnectError("no route")
        with pytest.raises(SupabaseUnavailable):
            run(load_press_cursors_from_db("x", client=unreachable))
    finally:
        GAME_DB_IDS.pop("x", None)
        clear_press_cursors("x")


def test_save_incrementally_upserts_only_advanced_cursors():
    GAME_DB_IDS["room_1"] = DB_UUID
    clear_press_cursors("room_1")
    try:
        client = client_answering(
            "post",
            httpx.Response(201),
            httpx.Response(201),
        )
        assert (
            run(
                save_press_cursors_to_db(
                    "room_1",
                    {Power.FRANCE: {"public": 3, "FRANCE+GERMANY": 1}},
                    client=client,
                )
            )
            is True
        )
        assert client.post.call_count == 1
        assert (
            "/rest/v1/press_read_cursors?on_conflict=game_id,power,channel_key"
            in client.post.call_args_list[0].args[0]
        )
        batch1 = client.post.call_args_list[0].kwargs["json"]
        assert len(batch1) == 2

        # Re-saving identical or lower cursors is a no-op.
        assert (
            run(
                save_press_cursors_to_db(
                    "room_1",
                    {Power.FRANCE: {"public": 3, "FRANCE+GERMANY": 1}},
                    client=client,
                )
            )
            is True
        )
        assert client.post.call_count == 1

        # Advancing one channel sends only that channel.
        assert (
            run(
                save_press_cursors_to_db(
                    "room_1",
                    {Power.FRANCE: {"public": 3, "FRANCE+GERMANY": 4}},
                    client=client,
                )
            )
            is True
        )
        assert client.post.call_count == 2
        batch2 = client.post.call_args_list[1].kwargs["json"]
        assert [(r["channel_key"], r["last_read_seq"]) for r in batch2] == [("FRANCE+GERMANY", 4)]
    finally:
        GAME_DB_IDS.pop("room_1", None)
        clear_press_cursors("room_1")


def test_save_reports_failure_when_game_missing_or_supabase_refuses():
    GAME_DB_IDS.pop("room_1", None)
    clear_press_cursors("room_1")
    try:
        no_game = AsyncMock(spec=httpx.AsyncClient)
        no_game.get.return_value = httpx.Response(200, json=[])
        assert (
            run(save_press_cursors_to_db("room_1", {Power.FRANCE: {"public": 2}}, client=no_game))
            is False
        )

        GAME_DB_IDS["room_1"] = DB_UUID
        refused = client_answering("post", httpx.Response(500, text="boom"))
        assert (
            run(save_press_cursors_to_db("room_1", {Power.FRANCE: {"public": 2}}, client=refused))
            is False
        )
    finally:
        GAME_DB_IDS.pop("room_1", None)
        clear_press_cursors("room_1")
