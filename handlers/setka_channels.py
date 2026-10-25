# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka broadcast channels: threaded comments + public channel directory."""

from __future__ import annotations

import collections.abc
import logging
import re
from typing import TYPE_CHECKING, Any

from synapse.api.constants import EventTypes, JoinRules
from synapse.api.errors import Codes, StoreError, SynapseError
from synapse.events import EventBase
from synapse.event_auth import get_user_power_level
from synapse.handlers.room_list import RoomListNextBatch
from synapse.types import JsonDict, StateMap

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

CHANNEL_TOPIC_PREFIX = "setka:channel"
MODERATOR_POWER_LEVEL = 50
_FEED_EVENT_TYPES = frozenset({"m.room.message", "m.sticker"})
_THREAD_REL_TYPES = frozenset({"m.thread", "io.element.thread"})
_TAG_PATTERN = re.compile(r"^#([a-z0-9_]{3,32})$", re.IGNORECASE)

_PUBLIC_JOIN_RULES = (
    JoinRules.PUBLIC,
    JoinRules.KNOCK,
    JoinRules.KNOCK_RESTRICTED,
)


def _content_mapping(content: object) -> collections.abc.Mapping | None:
    if isinstance(content, collections.abc.Mapping):
        return content
    return None


def _topic_from_state(state: StateMap[EventBase]) -> str:
    topic_event = state.get((EventTypes.Topic, ""))
    if topic_event is None:
        return ""
    content = _content_mapping(topic_event.content)
    if content is None:
        return ""
    return str(content.get("topic") or "")


def _is_setka_channel_state(state: StateMap[EventBase]) -> bool:
    topic = _topic_from_state(state).strip()
    return topic.lower().startswith(CHANNEL_TOPIC_PREFIX.lower())


def _parse_channel_tag(topic: str) -> str | None:
    if not topic.lower().startswith(CHANNEL_TOPIC_PREFIX.lower()):
        return None
    remainder = topic.strip()[len(CHANNEL_TOPIC_PREFIX) :].strip()
    if not remainder:
        return None
    first = remainder.split(" ", 1)[0].strip()
    match = _TAG_PATTERN.match(first)
    if match is None:
        return None
    return match.group(1).lower()


def _is_threaded_content(content: object) -> bool:
    content_map = _content_mapping(content)
    if content_map is None:
        return False
    relates = _content_mapping(content_map.get("m.relates_to"))
    if relates is not None:
        rel_type = relates.get("rel_type")
        if rel_type in _THREAD_REL_TYPES:
            return True
        if rel_type and relates.get("event_id"):
            return True
        in_reply = _content_mapping(relates.get("m.in_reply_to"))
        if in_reply is not None and in_reply.get("event_id"):
            return True
    top_in_reply = _content_mapping(content_map.get("m.in_reply_to"))
    if top_in_reply is not None and top_in_reply.get("event_id"):
        return True
    return False


def setka_channel_thread_comment_send_level(
    event: EventBase, auth_events: StateMap[EventBase]
) -> int | None:
    """Lower send_level for threaded comments in Setka channels (events_default=50)."""
    if event.type not in _FEED_EVENT_TYPES:
        return None
    if not _is_setka_channel_state(auth_events):
        return None
    if _is_threaded_content(event.content):
        return None
    pl_event = auth_events.get((EventTypes.PowerLevels, ""))
    pl_content = _content_mapping(pl_event.content) if pl_event is not None else None
    if pl_content is None:
        return 0
    try:
        return int(pl_content.get("users_default") or 0)
    except (TypeError, ValueError):
        return 0


def _room_entry_to_channel(room: JsonDict) -> JsonDict | None:
    topic = str(room.get("topic") or "")
    if not topic.lower().startswith(CHANNEL_TOPIC_PREFIX.lower()):
        return None
    return {
        "room_id": room.get("room_id"),
        "name": room.get("name"),
        "topic": topic,
        "tag": room.get("tag") or _parse_channel_tag(topic),
        "avatar_url": room.get("avatar_url"),
        "num_joined_members": room.get("num_joined_members", 0),
        "canonical_alias": room.get("canonical_alias"),
        "aliases": room.get("aliases") or [],
        "published_at": room.get("published_at"),
        "hidden": bool(room.get("hidden")),
    }


def _column_exists(txn: Any, engine: object, table: str, column: str) -> bool:
    from synapse.storage.engines import PostgresEngine

    if isinstance(engine, PostgresEngine):
        txn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            (table, column),
        )
        return txn.fetchone() is not None

    txn.execute(f"PRAGMA table_info({table})")
    return any(str(row[1]) == column for row in txn.fetchall())


def _add_column_if_missing(
    txn: Any,
    engine: object,
    table: str,
    column: str,
    column_def: str,
) -> None:
    """Add a column without leaving the transaction in a failed state."""
    from synapse.storage.engines import PostgresEngine

    if isinstance(engine, PostgresEngine):
        txn.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_def}"
        )
        return

    if _column_exists(txn, engine, table, column):
        return
    txn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def _matches_search(entry: JsonDict, query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    if needle.startswith("#"):
        tag = str(entry.get("tag") or "").lower()
        return needle[1:] in tag or tag == needle[1:]
    haystacks = [
        str(entry.get("name") or "").lower(),
        str(entry.get("topic") or "").lower(),
        str(entry.get("canonical_alias") or "").lower(),
        str(entry.get("tag") or "").lower(),
    ]
    return any(needle in hay for hay in haystacks)


class SetkaChannelsHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._registry_table_ready = False
        hs.get_module_api().register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
        )
        logger.info(
            "Setka channels: event rules registered (topic prefix=%s)",
            CHANNEL_TOPIC_PREFIX,
        )

    def _ensure_registry_table_sync(self, txn: Any) -> None:
        txn.execute(
            """
            CREATE TABLE IF NOT EXISTS setka_published_channels (
                room_id TEXT NOT NULL,
                published_at BIGINT NOT NULL,
                tag TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                category_id TEXT,
                feed_enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (room_id)
            )
            """
        )
        engine = self._store.database_engine
        for column, column_def in (
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            ("category_id", "TEXT"),
            ("feed_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ):
            _add_column_if_missing(
                txn,
                engine,
                "setka_published_channels",
                column,
                column_def,
            )

    async def _ensure_registry_table(self) -> None:
        if self._registry_table_ready:
            return

        def _txn(txn: Any) -> None:
            self._ensure_registry_table_sync(txn)

        await self._store.db_pool.runInteraction(
            "setka_published_channels_create",
            _txn,
        )
        self._registry_table_ready = True

    async def check_event_allowed(
        self, event: EventBase, state: StateMap[EventBase]
    ) -> tuple[bool, dict | None]:
        if event.type not in _FEED_EVENT_TYPES:
            return True, None
        if not _is_setka_channel_state(state):
            return True, None
        if _is_threaded_content(event.content):
            return True, None
        if get_user_power_level(event.sender, state) >= MODERATOR_POWER_LEVEL:
            try:
                # Feed listing joins setka_published_channels. Ensure the room is
                # registered for feed even if client publish raced / failed.
                await self._ensure_feed_registry_for_channel(event.room_id, state)
                await self._hs.get_setka_feed_handler().maybe_ingest_event(event)
            except Exception:
                logger.warning(
                    "Setka feed: failed to ingest post %s in %s",
                    event.event_id,
                    event.room_id,
                    exc_info=True,
                )
            return True, None
        return False, None

    async def _ensure_feed_registry_for_channel(
        self, room_id: str, state: StateMap[EventBase]
    ) -> None:
        """Ensure a Setka channel room can appear in the feed index."""
        topic = _topic_from_state(state)
        tag = _parse_channel_tag(topic)
        await self._upsert_published_channel(
            room_id,
            tag=tag,
            update_category=False,
        )

    async def _upsert_published_channel(
        self,
        room_id: str,
        *,
        tag: str | None,
        category_id: str | None = None,
        published_at: int | None = None,
        update_category: bool = False,
    ) -> None:
        await self._ensure_registry_table()
        ts = published_at if published_at is not None else self._clock.time_msec()

        def _txn(txn: Any) -> None:
            self._ensure_registry_table_sync(txn)
            txn.execute(
                "SELECT room_id FROM setka_published_channels WHERE room_id = ?",
                (room_id,),
            )
            exists = txn.fetchone() is not None
            if exists:
                if update_category:
                    txn.execute(
                        """
                        UPDATE setka_published_channels
                        SET published_at = ?, tag = ?, category_id = ?, feed_enabled = 1
                        WHERE room_id = ?
                        """,
                        (ts, tag, category_id, room_id),
                    )
                else:
                    txn.execute(
                        """
                        UPDATE setka_published_channels
                        SET published_at = ?, tag = ?, feed_enabled = 1
                        WHERE room_id = ?
                        """,
                        (ts, tag, room_id),
                    )
            elif update_category:
                txn.execute(
                    """
                    INSERT INTO setka_published_channels
                        (room_id, published_at, tag, category_id, feed_enabled)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (room_id, ts, tag, category_id),
                )
            else:
                txn.execute(
                    """
                    INSERT INTO setka_published_channels
                        (room_id, published_at, tag, feed_enabled)
                    VALUES (?, ?, ?, 1)
                    """,
                    (room_id, ts, tag),
                )

        await self._store.db_pool.runInteraction("setka_channel_publish", _txn)

    async def _fetch_published_channels_from_registry(
        self,
        *,
        limit: int,
        bounds: tuple[int, str] | None,
        forwards: bool,
        include_hidden: bool = False,
    ) -> list[JsonDict]:
        await self._ensure_registry_table()
        topic_pattern = f"%{CHANNEL_TOPIC_PREFIX.lower()}%"
        join_rules_sql = ", ".join(f"'{rule}'" for rule in _PUBLIC_JOIN_RULES)

        def _txn(txn: Any) -> list[JsonDict]:
            where_clauses = [
                "LOWER(room_stats_state.topic) LIKE ?",
                f"room_stats_state.join_rules IN ({join_rules_sql})",
            ]
            if not include_hidden:
                where_clauses.append(
                    "(setka_published_channels.hidden = 0 OR setka_published_channels.hidden IS NULL)"
                )
            args: list[object] = [topic_pattern]

            if bounds:
                last_joined_members, last_room_id = bounds
                if forwards:
                    where_clauses.append(
                        "(room_stats_current.joined_members <= ? AND ("
                        "room_stats_current.joined_members < ? OR "
                        "setka_published_channels.room_id < ?))"
                    )
                else:
                    where_clauses.append(
                        "(room_stats_current.joined_members >= ? AND ("
                        "room_stats_current.joined_members > ? OR "
                        "setka_published_channels.room_id > ?))"
                    )
                args.extend([last_joined_members, last_joined_members, last_room_id])

            direction = "DESC" if forwards else "ASC"
            sql = f"""
                SELECT
                    setka_published_channels.room_id,
                    room_stats_state.name,
                    room_stats_state.topic,
                    room_stats_state.canonical_alias,
                    room_stats_current.joined_members,
                    room_stats_state.avatar,
                    setka_published_channels.published_at,
                    setka_published_channels.tag,
                    setka_published_channels.hidden,
                    setka_published_channels.category_id,
                    setka_published_channels.feed_enabled
                FROM setka_published_channels
                INNER JOIN room_stats_state
                    ON setka_published_channels.room_id = room_stats_state.room_id
                INNER JOIN room_stats_current
                    ON setka_published_channels.room_id = room_stats_current.room_id
                WHERE {" AND ".join(where_clauses)}
                ORDER BY room_stats_current.joined_members {direction},
                         setka_published_channels.room_id {direction}
                LIMIT ?
            """
            args.append(limit)
            txn.execute(sql, args)
            return [
                {
                    "room_id": row[0],
                    "name": row[1],
                    "topic": row[2],
                    "canonical_alias": row[3],
                    "num_joined_members": row[4],
                    "avatar_url": row[5],
                    "published_at": row[6],
                    "tag": row[7],
                    "hidden": int(row[8] or 0),
                    "category_id": row[9],
                    "feed_enabled": int(row[10] if row[10] is not None else 1),
                }
                for row in txn
            ]

        return await self._store.db_pool.runInteraction(
            "list_setka_published_channels",
            _txn,
        )

    async def list_public_channels(
        self,
        *,
        limit: int | None,
        since_token: str | None,
        search_query: str | None = None,
        user_id: str | None = None,
    ) -> JsonDict:
        """List published Setka channels from the registry with live room stats."""
        page_size = max(1, min(limit or 40, 100))
        bounds: tuple[int, str] | None = None
        forwards = True
        if since_token:
            batch_token = RoomListNextBatch.from_token(since_token)
            bounds = (batch_token.last_joined_members, batch_token.last_room_id)
            forwards = batch_token.direction_is_forward

        probing_limit = page_size + 10
        raw_rooms = await self._fetch_published_channels_from_registry(
            limit=probing_limit,
            bounds=bounds,
            forwards=forwards,
        )

        feed_handler = self._hs.get_setka_feed_handler()
        subscription_map: dict[str, bool] = {}
        if user_id:
            room_ids = [str(room.get("room_id") or "") for room in raw_rooms]
            subscription_map = await feed_handler.subscription_map(user_id, room_ids)

        channels: list[JsonDict] = []
        last_considered: JsonDict | None = None
        for room in raw_rooms:
            last_considered = room
            entry = _room_entry_to_channel(room)
            if entry is None:
                continue
            category_id = room.get("category_id")
            if category_id:
                entry["category_id"] = category_id
                entry["category_label"] = await feed_handler.get_category_label(
                    str(category_id)
                )
            if user_id:
                room_id = str(entry.get("room_id") or "")
                entry["subscribed"] = subscription_map.get(room_id, False)
            if not _matches_search(entry, search_query or ""):
                continue
            channels.append(entry)
            if len(channels) >= page_size:
                break

        next_batch: str | None = None
        if last_considered is not None and len(raw_rooms) >= probing_limit:
            next_batch = RoomListNextBatch(
                last_joined_members=int(last_considered.get("num_joined_members") or 0),
                last_room_id=str(last_considered.get("room_id") or ""),
                direction_is_forward=True,
            ).to_token()

        logger.info(
            "Setka channels: list_public_channels returned %d channel(s) (query=%r)",
            len(channels),
            search_query,
        )

        return {
            "channels": channels,
            "next_batch": next_batch,
            "total_room_count_estimate": len(channels),
        }

    async def list_published_channels_admin(self, *, limit: int = 100) -> JsonDict:
        """Admin listing of all registry entries with live stats."""
        page_size = max(1, min(limit, 500))
        raw_rooms = await self._fetch_published_channels_from_registry(
            limit=page_size,
            bounds=None,
            forwards=True,
            include_hidden=True,
        )
        channels = [
            entry
            for room in raw_rooms
            if (entry := _room_entry_to_channel(room)) is not None
        ]
        return {"channels": channels, "total": len(channels)}

    async def ensure_channel_published(
        self,
        room_id: str,
        *,
        user_id: str | None = None,
        category_id: str | None = None,
    ) -> bool:
        """Ensure a Setka channel appears in the public channel registry."""
        try:
            state = await self._hs.get_storage_controllers().state.get_current_state(
                room_id
            )
        except Exception:
            logger.warning(
                "Setka channels: could not load state for %s",
                room_id,
                exc_info=True,
            )
            return False

        topic = _topic_from_state(state)
        if not _is_setka_channel_state(state):
            logger.warning(
                "Setka channels: skip publish for %s (missing channel topic, got %r)",
                room_id,
                topic,
            )
            return False

        join_rules = state.get((EventTypes.JoinRules, ""))
        join_content = _content_mapping(join_rules.content) if join_rules else None
        join_rule = str((join_content or {}).get("join_rule") or "")

        if join_rule not in {"public", "knock", "knock_restricted"}:
            logger.info(
                "Setka channels: skip publish for %s (join_rule=%s)",
                room_id,
                join_rule,
            )
            await self.unpublish_channel(room_id)
            return False

        try:
            feed_handler = self._hs.get_setka_feed_handler()
        except Exception as e:
            logger.exception("Setka channels: feed handler unavailable")
            raise SynapseError(
                500,
                f"Setka feed module is not available: {e}",
                Codes.UNKNOWN,
            ) from e

        resolved_category_id: str | None = None
        try:
            if join_rule == "public":
                await feed_handler._ensure_tables()
                await self._ensure_registry_table()

                def _existing_category(txn: Any) -> str | None:
                    self._ensure_registry_table_sync(txn)
                    txn.execute(
                        "SELECT category_id FROM setka_published_channels WHERE room_id = ?",
                        (room_id,),
                    )
                    row = txn.fetchone()
                    return str(row[0]) if row and row[0] else None

                existing = await self._store.db_pool.runInteraction(
                    "setka_channel_existing_category",
                    _existing_category,
                )
                if category_id:
                    resolved_category_id = await feed_handler.validate_category_id(
                        category_id
                    )
                elif existing:
                    resolved_category_id = existing
                else:
                    raise SynapseError(
                        400, "category_id is required", Codes.MISSING_PARAM
                    )

            tag = _parse_channel_tag(topic)
            await self._upsert_published_channel(
                room_id,
                tag=tag,
                category_id=resolved_category_id,
                update_category=resolved_category_id is not None,
            )
            try:
                await self._store.set_room_is_public(room_id, True)
            except StoreError:
                logger.warning(
                    "Setka channels: room %s missing from rooms table; "
                    "published in Setka registry only",
                    room_id,
                )
        except SynapseError:
            raise
        except Exception as e:
            logger.exception("Setka channels: publish failed for %s", room_id)
            raise SynapseError(
                500,
                f"Channel publish failed: {type(e).__name__}: {e}",
                Codes.UNKNOWN,
            ) from e

        logger.info("Setka channels: published %s in channel registry", room_id)
        try:
            await self._hs.get_setka_analytics_handler().record_event(
                event_name="channel.published",
                user_id=user_id,
                source="server",
                properties={"room_id": room_id},
            )
        except Exception:
            logger.warning(
                "Setka channels: analytics record failed for %s",
                room_id,
                exc_info=True,
            )
        return True

    async def unpublish_channel(self, room_id: str) -> None:
        """Remove a room from the published channel registry."""
        await self._ensure_registry_table()

        def _txn(txn: Any) -> None:
            txn.execute(
                "DELETE FROM setka_published_channels WHERE room_id = ?",
                (room_id,),
            )

        await self._store.db_pool.runInteraction(
            "setka_channel_unpublish",
            _txn,
        )
        await self._store.set_room_is_public(room_id, False)
        logger.info("Setka channels: unpublished %s from channel registry", room_id)

    async def set_channel_hidden(self, room_id: str, *, hidden: bool) -> JsonDict:
        """Hide or show a channel in public discovery (admin)."""
        await self._ensure_registry_table()

        def _txn(txn: Any) -> bool:
            txn.execute(
                "UPDATE setka_published_channels SET hidden = ? WHERE room_id = ?",
                (1 if hidden else 0, room_id),
            )
            return txn.rowcount > 0

        updated = await self._store.db_pool.runInteraction(
            "setka_channel_set_hidden",
            _txn,
        )
        if not updated:
            raise SynapseError(404, "Channel not in registry", Codes.NOT_FOUND)
        logger.info("Setka channels: %s hidden=%s", room_id, hidden)
        return {"room_id": room_id, "hidden": hidden}

    async def rebuild_registry(self) -> JsonDict:
        """Scan all Setka channels with public join rules and repopulate the registry."""
        await self._ensure_registry_table()
        topic_pattern = f"%{CHANNEL_TOPIC_PREFIX.lower()}%"
        join_rules_sql = ", ".join(f"'{rule}'" for rule in _PUBLIC_JOIN_RULES)
        now_ms = self._clock.time_msec()

        def _scan(txn: Any) -> list[tuple[str, str]]:
            txn.execute(
                f"""
                SELECT room_stats_state.room_id, room_stats_state.topic
                FROM room_stats_state
                INNER JOIN room_stats_current USING (room_id)
                WHERE LOWER(room_stats_state.topic) LIKE ?
                  AND room_stats_state.join_rules IN ({join_rules_sql})
                """,
                (topic_pattern,),
            )
            return [(str(row[0]), str(row[1] or "")) for row in txn.fetchall()]

        valid_rooms = await self._store.db_pool.runInteraction(
            "setka_channels_rebuild_scan",
            _scan,
        )
        valid_ids = {room_id for room_id, _ in valid_rooms}

        def _rebuild(txn: Any) -> tuple[int, int]:
            self._ensure_registry_table_sync(txn)
            upserted = 0
            for room_id, topic in valid_rooms:
                tag = _parse_channel_tag(topic)
                txn.execute(
                    "SELECT room_id FROM setka_published_channels WHERE room_id = ?",
                    (room_id,),
                )
                if txn.fetchone() is None:
                    txn.execute(
                        """
                        INSERT INTO setka_published_channels (room_id, published_at, tag)
                        VALUES (?, ?, ?)
                        """,
                        (room_id, now_ms, tag),
                    )
                else:
                    txn.execute(
                        """
                        UPDATE setka_published_channels SET tag = ? WHERE room_id = ?
                        """,
                        (tag, room_id),
                    )
                upserted += 1

            txn.execute("SELECT room_id FROM setka_published_channels")
            stale = [row[0] for row in txn.fetchall() if row[0] not in valid_ids]
            removed = 0
            for room_id in stale:
                txn.execute(
                    "DELETE FROM setka_published_channels WHERE room_id = ?",
                    (room_id,),
                )
                removed += 1
            return upserted, removed

        upserted, removed = await self._store.db_pool.runInteraction(
            "setka_channels_rebuild",
            _rebuild,
        )

        logger.info(
            "Setka channels: registry rebuilt (%d upserted, %d stale removed)",
            upserted,
            removed,
        )
        return {
            "upserted": upserted,
            "removed_stale": removed,
            "total_published": len(valid_ids),
        }
