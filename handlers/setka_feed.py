# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka channel feed: categories, subscriptions, post index, recommendations."""

from __future__ import annotations

import logging
import random
import re
from typing import TYPE_CHECKING, Any

from synapse.api.errors import Codes, SynapseError
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

HEART_EMOJI = "❤️"

DEFAULT_CATEGORIES: tuple[tuple[str, str, int], ...] = (
    ("humor", "#Юмор", 1),
    ("creativity", "#Творчество", 2),
    ("news", "#Новости", 3),
    ("tech", "#IT", 4),
    ("music", "#Музыка", 5),
    ("education", "#Образование", 6),
    ("lifestyle", "#Lifestyle", 7),
    ("games", "#Игры", 8),
)


def _content_mapping(content: object) -> dict | None:
    if isinstance(content, dict):
        return content
    return None


def _message_fields_from_content(content: dict) -> tuple[str, str, str | None]:
    msgtype = str(content.get("msgtype") or "m.text")
    body = str(content.get("body") or "")
    media_mxc: str | None = None
    if msgtype in ("m.image", "m.video"):
        media_mxc = content.get("url")
    file_content = content.get("file")
    if not media_mxc and isinstance(file_content, dict):
        media_mxc = file_content.get("url")
    if media_mxc is not None:
        media_mxc = str(media_mxc)
    return msgtype, body, media_mxc


def _post_is_visible(post: JsonDict) -> bool:
    body = str(post.get("body") or "").strip()
    media = post.get("media_mxc")
    msgtype = str(post.get("msgtype") or "")
    if body:
        return True
    if media and msgtype in ("m.image", "m.video", "m.audio", "m.file"):
        return True
    return False


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


class SetkaFeedHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._tables_ready = False

    async def _ensure_tables(self) -> None:
        if self._tables_ready:
            return

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_channel_categories (
                    id TEXT NOT NULL PRIMARY KEY,
                    label TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            for cat_id, label, sort_order in DEFAULT_CATEGORIES:
                txn.execute(
                    "SELECT 1 FROM setka_channel_categories WHERE id = ?",
                    (cat_id,),
                )
                if txn.fetchone() is None:
                    txn.execute(
                        """
                        INSERT INTO setka_channel_categories
                            (id, label, sort_order, is_active)
                        VALUES (?, ?, ?, 1)
                        """,
                        (cat_id, label, sort_order),
                    )
                else:
                    txn.execute(
                        """
                        UPDATE setka_channel_categories
                        SET label = ?, sort_order = ?
                        WHERE id = ?
                        """,
                        (label, sort_order, cat_id),
                    )

            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_published_channels (
                    room_id TEXT NOT NULL PRIMARY KEY,
                    published_at BIGINT NOT NULL,
                    tag TEXT
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

            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_channel_subscriptions (
                    user_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    subscribed_at BIGINT NOT NULL,
                    PRIMARY KEY (user_id, room_id)
                )
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_feed_posts (
                    event_id TEXT NOT NULL PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    category_id TEXT,
                    sender TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    msgtype TEXT NOT NULL,
                    body TEXT,
                    media_mxc TEXT,
                    comment_count INTEGER NOT NULL DEFAULT 0,
                    view_count INTEGER NOT NULL DEFAULT 0,
                    like_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            txn.execute(
                """
                CREATE INDEX IF NOT EXISTS setka_feed_posts_room_created
                ON setka_feed_posts (room_id, created_at DESC)
                """
            )
            txn.execute(
                """
                CREATE INDEX IF NOT EXISTS setka_feed_posts_category_created
                ON setka_feed_posts (category_id, created_at DESC)
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_feed_likes (
                    user_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    liked_at BIGINT NOT NULL,
                    PRIMARY KEY (user_id, event_id)
                )
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_feed_views (
                    user_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    viewed_at BIGINT NOT NULL,
                    PRIMARY KEY (user_id, event_id)
                )
                """
            )

        try:
            await self._store.db_pool.runInteraction("setka_feed_create_tables", _txn)
        except Exception:
            logger.exception("Setka feed: failed to create/migrate tables")
            raise
        self._tables_ready = True

    async def list_categories(self) -> JsonDict:
        try:
            await self._ensure_tables()
        except Exception as e:
            logger.exception("Setka feed: list_categories schema setup failed")
            raise SynapseError(
                500,
                f"Setka feed schema setup failed: {type(e).__name__}: {e}",
                Codes.UNKNOWN,
            ) from e

        def _txn(txn: Any) -> list[JsonDict]:
            txn.execute(
                """
                SELECT id, label, sort_order
                FROM setka_channel_categories
                WHERE is_active = 1
                ORDER BY sort_order ASC, label ASC
                """
            )
            return [
                {"id": row[0], "label": row[1], "sort_order": row[2]}
                for row in txn.fetchall()
            ]

        categories = await self._store.db_pool.runInteraction(
            "setka_feed_list_categories",
            _txn,
        )
        if not categories:
            categories = [
                {"id": cat_id, "label": label, "sort_order": sort_order}
                for cat_id, label, sort_order in DEFAULT_CATEGORIES
            ]
        return {"categories": categories}

    async def get_category_label(self, category_id: str | None) -> str | None:
        if not category_id:
            return None
        await self._ensure_tables()

        def _txn(txn: Any) -> str | None:
            txn.execute(
                "SELECT label FROM setka_channel_categories WHERE id = ?",
                (category_id,),
            )
            row = txn.fetchone()
            return str(row[0]) if row else None

        return await self._store.db_pool.runInteraction(
            "setka_feed_category_label",
            _txn,
        )

    async def subscription_map(self, user_id: str, room_ids: list[str]) -> dict[str, bool]:
        if not room_ids:
            return {}
        await self._ensure_tables()

        def _txn(txn: Any) -> dict[str, bool]:
            placeholders = ",".join("?" for _ in room_ids)
            txn.execute(
                f"""
                SELECT room_id FROM setka_channel_subscriptions
                WHERE user_id = ? AND room_id IN ({placeholders})
                """,
                [user_id, *room_ids],
            )
            subscribed = {str(row[0]) for row in txn.fetchall()}
            return {room_id: room_id in subscribed for room_id in room_ids}

        return await self._store.db_pool.runInteraction(
            "setka_feed_subscription_map",
            _txn,
        )

    async def validate_category_id(self, category_id: str | None) -> str:
        if not category_id or not str(category_id).strip():
            raise SynapseError(400, "category_id is required", Codes.MISSING_PARAM)
        category_id = str(category_id).strip()
        await self._ensure_tables()

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                SELECT 1 FROM setka_channel_categories
                WHERE id = ? AND is_active = 1
                """,
                (category_id,),
            )
            return txn.fetchone() is not None

        exists = await self._store.db_pool.runInteraction(
            "setka_feed_validate_category",
            _txn,
        )
        if not exists:
            raise SynapseError(400, "Unknown category_id", Codes.INVALID_PARAM)
        return category_id

    async def set_channel_category(
        self, room_id: str, *, category_id: str, feed_enabled: bool = True
    ) -> None:
        category_id = await self.validate_category_id(category_id)
        await self._ensure_tables()

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                UPDATE setka_published_channels
                SET category_id = ?, feed_enabled = ?
                WHERE room_id = ?
                """,
                (category_id, 1 if feed_enabled else 0, room_id),
            )

        await self._store.db_pool.runInteraction(
            "setka_feed_set_channel_category",
            _txn,
        )

    async def subscribe(self, user_id: str, room_id: str) -> JsonDict:
        await self._ensure_tables()
        now_ms = self._clock.time_msec()
        await self._store.db_pool.simple_upsert(
            table="setka_channel_subscriptions",
            keyvalues={"user_id": user_id, "room_id": room_id},
            values={},
            insertion_values={"subscribed_at": now_ms},
            desc="setka_channel_subscribe",
        )
        return {"room_id": room_id, "subscribed": True}

    async def unsubscribe(self, user_id: str, room_id: str) -> JsonDict:
        await self._ensure_tables()

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                DELETE FROM setka_channel_subscriptions
                WHERE user_id = ? AND room_id = ?
                """,
                (user_id, room_id),
            )

        await self._store.db_pool.runInteraction("setka_channel_unsubscribe", _txn)
        return {"room_id": room_id, "subscribed": False}

    async def get_subscription(self, user_id: str, room_id: str) -> JsonDict:
        await self._ensure_tables()

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                SELECT 1 FROM setka_channel_subscriptions
                WHERE user_id = ? AND room_id = ?
                """,
                (user_id, room_id),
            )
            return txn.fetchone() is not None

        subscribed = await self._store.db_pool.runInteraction(
            "setka_channel_get_subscription",
            _txn,
        )
        return {"room_id": room_id, "subscribed": subscribed}

    async def _user_subscribed_rooms(self, user_id: str) -> set[str]:
        await self._ensure_tables()

        def _txn(txn: Any) -> set[str]:
            txn.execute(
                "SELECT room_id FROM setka_channel_subscriptions WHERE user_id = ?",
                (user_id,),
            )
            return {str(row[0]) for row in txn.fetchall()}

        return await self._store.db_pool.runInteraction(
            "setka_feed_user_subscriptions",
            _txn,
        )

    async def _user_top_categories(self, user_id: str) -> list[str]:
        subscribed = await self._user_subscribed_rooms(user_id)
        if not subscribed:
            return []

        def _txn(txn: Any) -> list[str]:
            placeholders = ",".join("?" for _ in subscribed)
            txn.execute(
                f"""
                SELECT category_id, COUNT(*) AS cnt
                FROM setka_published_channels
                WHERE room_id IN ({placeholders}) AND category_id IS NOT NULL
                GROUP BY category_id
                ORDER BY cnt DESC
                LIMIT 5
                """,
                list(subscribed),
            )
            return [str(row[0]) for row in txn.fetchall() if row[0]]

        return await self._store.db_pool.runInteraction(
            "setka_feed_user_top_categories",
            _txn,
        )

    async def maybe_ingest_event(self, event: "EventBase") -> None:
        if event.type != "m.room.message":
            return
        content = _content_mapping(event.content) or {}
        if content.get("m.relates_to"):
            return
        room_id = event.room_id
        await self._ensure_tables()

        def _room_feed_enabled(txn: Any) -> tuple[bool, str | None]:
            txn.execute(
                """
                SELECT feed_enabled, category_id
                FROM setka_published_channels
                WHERE room_id = ? AND (hidden = 0 OR hidden IS NULL)
                """,
                (room_id,),
            )
            row = txn.fetchone()
            if row is None:
                return False, None
            return bool(row[0]), row[1]

        enabled, category_id = await self._store.db_pool.runInteraction(
            "setka_feed_room_enabled",
            _room_feed_enabled,
        )
        if not enabled:
            return

        msgtype, body, media_mxc = _message_fields_from_content(content)

        def _upsert(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO setka_feed_posts (
                    event_id, room_id, category_id, sender, created_at,
                    msgtype, body, media_mxc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_id) DO UPDATE SET
                    body = excluded.body,
                    media_mxc = excluded.media_mxc,
                    msgtype = excluded.msgtype
                """,
                (
                    event.event_id,
                    room_id,
                    category_id,
                    event.sender,
                    int(event.origin_server_ts),
                    msgtype,
                    body,
                    media_mxc,
                ),
            )

        await self._store.db_pool.runInteraction("setka_feed_ingest_post", _upsert)

    async def is_feed_enabled(self, room_id: str) -> tuple[bool, str | None]:
        await self._ensure_tables()

        def _txn(txn: Any) -> tuple[bool, str | None]:
            txn.execute(
                """
                SELECT feed_enabled, category_id
                FROM setka_published_channels
                WHERE room_id = ?
                """,
                (room_id,),
            )
            row = txn.fetchone()
            if row is None:
                return False, None
            return bool(row[0]), (str(row[1]) if row[1] else None)

        return await self._store.db_pool.runInteraction(
            "setka_feed_is_enabled",
            _txn,
        )

    async def ingest_post(
        self,
        event: "EventBase",
        *,
        room_id: str,
        category_id: str,
    ) -> None:
        if event.type != "m.room.message":
            return
        content = _content_mapping(event.content) or {}
        if content.get("m.relates_to"):
            return
        await self._ensure_tables()
        msgtype, body, media_mxc = _message_fields_from_content(content)

        def _upsert(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO setka_feed_posts (
                    event_id, room_id, category_id, sender, created_at,
                    msgtype, body, media_mxc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_id) DO UPDATE SET
                    body = excluded.body,
                    media_mxc = excluded.media_mxc,
                    msgtype = excluded.msgtype,
                    category_id = excluded.category_id
                """,
                (
                    event.event_id,
                    room_id,
                    category_id,
                    event.sender,
                    int(event.origin_server_ts),
                    msgtype,
                    body,
                    media_mxc,
                ),
            )

        await self._store.db_pool.runInteraction("setka_feed_ingest_post", _upsert)

    async def _user_joined_published_channels(self, user_id: str) -> set[str]:
        await self._ensure_tables()

        def _txn(txn: Any) -> set[str]:
            txn.execute(
                """
                SELECT setka_published_channels.room_id
                FROM setka_published_channels
                INNER JOIN local_current_membership
                    ON setka_published_channels.room_id = local_current_membership.room_id
                WHERE local_current_membership.user_id = ?
                  AND local_current_membership.membership = 'join'
                """,
                (user_id,),
            )
            return {str(row[0]) for row in txn.fetchall()}

        try:
            return await self._store.db_pool.runInteraction(
                "setka_feed_joined_published_channels",
                _txn,
            )
        except Exception:
            logger.warning(
                "Setka feed: joined published channels lookup failed for %s",
                user_id,
                exc_info=True,
            )
            return set()

    async def _subscription_feed_room_ids(self, user_id: str) -> set[str]:
        subscribed = await self._user_subscribed_rooms(user_id)
        joined = await self._user_joined_published_channels(user_id)
        return subscribed | joined

    async def _redacted_event_ids(self, event_ids: list[str]) -> set[str]:
        ids = [event_id for event_id in event_ids if event_id]
        if not ids:
            return set()

        def _txn(txn: Any) -> set[str]:
            placeholders = ",".join("?" for _ in ids)
            txn.execute(
                f"SELECT redacts FROM redactions WHERE redacts IN ({placeholders})",
                ids,
            )
            return {str(row[0]) for row in txn.fetchall()}

        return await self._store.db_pool.runInteraction(
            "setka_feed_redacted_event_ids",
            _txn,
        )

    async def _purge_feed_posts(self, event_ids: set[str]) -> None:
        if not event_ids:
            return

        def _txn(txn: Any) -> None:
            placeholders = ",".join("?" for _ in event_ids)
            txn.execute(
                f"DELETE FROM setka_feed_posts WHERE event_id IN ({placeholders})",
                list(event_ids),
            )

        await self._store.db_pool.runInteraction("setka_feed_purge_posts", _txn)

    async def _filter_posts_with_events(self, posts: list[JsonDict]) -> list[JsonDict]:
        if not posts:
            return []

        event_ids = [str(post["event_id"]) for post in posts]
        try:
            events = await self._store.get_events_as_list(
                event_ids,
                redact_behaviour=EventRedactBehaviour.block,
            )
        except Exception:
            logger.warning(
                "Setka feed: batch event fetch failed, using indexed content",
                exc_info=True,
            )
            return [post for post in posts if _post_is_visible(post)]

        events_by_id = {str(event.event_id): event for event in events}
        missing_ids = [
            event_id for event_id in event_ids if event_id not in events_by_id
        ]
        redacted_ids = await self._redacted_event_ids(missing_ids)
        if redacted_ids:
            await self._purge_feed_posts(redacted_ids)

        filled: list[JsonDict] = []
        for post in posts:
            event_id = str(post["event_id"])
            event = events_by_id.get(event_id)
            if event is None:
                # Missing from the batch: redacted/deleted, or not yet visible to the
                # events worker. Keep indexed snapshots for live posts; hide redactions.
                if event_id in redacted_ids:
                    continue
                if _post_is_visible(post):
                    filled.append(post)
                continue
            if event.type != "m.room.message":
                continue
            content = _content_mapping(event.content) or {}
            if content.get("m.relates_to"):
                continue
            msgtype, body, media_mxc = _message_fields_from_content(content)
            refreshed = dict(post)
            refreshed["msgtype"] = msgtype
            refreshed["body"] = body
            if media_mxc:
                refreshed["media_mxc"] = media_mxc
            if _post_is_visible(refreshed):
                filled.append(refreshed)
        return filled

    async def _hydrate_posts(
        self, user_id: str, rows: list[tuple[Any, ...]]
    ) -> list[JsonDict]:
        if not rows:
            return []
        event_ids = [str(row[0]) for row in rows]
        room_ids = {str(row[1]) for row in rows}

        def _meta(txn: Any) -> tuple[dict[str, bool], dict[str, bool], dict[str, JsonDict]]:
            placeholders = ",".join("?" for _ in event_ids)
            txn.execute(
                f"SELECT event_id FROM setka_feed_likes WHERE user_id = ? AND event_id IN ({placeholders})",
                [user_id, *event_ids],
            )
            liked = {str(row[0]): True for row in txn.fetchall()}

            room_placeholders = ",".join("?" for _ in room_ids)
            txn.execute(
                f"""
                SELECT room_id FROM setka_channel_subscriptions
                WHERE user_id = ? AND room_id IN ({room_placeholders})
                """,
                [user_id, *room_ids],
            )
            subscribed_rooms = {str(row[0]): True for row in txn.fetchall()}

            txn.execute(
                f"""
                SELECT
                    setka_published_channels.room_id,
                    room_stats_state.name,
                    room_stats_state.topic,
                    room_stats_state.avatar,
                    setka_published_channels.category_id,
                    setka_channel_categories.label
                FROM setka_published_channels
                LEFT JOIN room_stats_state
                    ON setka_published_channels.room_id = room_stats_state.room_id
                LEFT JOIN setka_channel_categories
                    ON setka_published_channels.category_id = setka_channel_categories.id
                WHERE setka_published_channels.room_id IN ({room_placeholders})
                """,
                list(room_ids),
            )
            room_meta = {
                str(row[0]): {
                    "room_name": row[1],
                    "room_topic": row[2],
                    "room_avatar_url": row[3],
                    "category_id": row[4],
                    "category_label": row[5],
                }
                for row in txn.fetchall()
            }
            return liked, subscribed_rooms, room_meta

        liked, subscribed_rooms, room_meta = await self._store.db_pool.runInteraction(
            "setka_feed_hydrate_posts",
            _meta,
        )

        posts: list[JsonDict] = []
        for row in rows:
            event_id = str(row[0])
            room_id = str(row[1])
            meta = room_meta.get(room_id, {})
            posts.append(
                {
                    "event_id": event_id,
                    "room_id": room_id,
                    "sender": row[2],
                    "created_at": row[3],
                    "msgtype": row[4],
                    "body": row[5],
                    "media_mxc": row[6],
                    "comment_count": row[7],
                    "view_count": row[8],
                    "like_count": row[9],
                    "liked": liked.get(event_id, False),
                    "subscribed": subscribed_rooms.get(room_id, False),
                    "room_name": meta.get("room_name"),
                    "room_avatar_url": meta.get("room_avatar_url"),
                    "category_id": meta.get("category_id"),
                    "category_label": meta.get("category_label"),
                }
            )
        return await self._filter_posts_with_events(posts)

    def _parse_since(self, since: str | None) -> int | None:
        if not since:
            return None
        try:
            return int(since)
        except ValueError:
            return None

    async def _fetch_posts(
        self,
        *,
        user_id: str,
        limit: int,
        since_ms: int | None,
        room_ids: set[str] | None,
        category_ids: list[str] | None,
        search_query: str | None,
        random_explore: bool,
    ) -> list[JsonDict]:
        await self._ensure_tables()
        limit = max(1, min(limit, 50))

        def _txn(txn: Any) -> list[tuple[Any, ...]]:
            clauses = [
                "setka_published_channels.feed_enabled = 1",
                "(setka_published_channels.hidden = 0 OR setka_published_channels.hidden IS NULL)",
            ]
            args: list[object] = []
            if since_ms is not None:
                clauses.append("setka_feed_posts.created_at < ?")
                args.append(since_ms)
            if room_ids is not None:
                if not room_ids:
                    return []
                placeholders = ",".join("?" for _ in room_ids)
                clauses.append(f"setka_feed_posts.room_id IN ({placeholders})")
                args.extend(sorted(room_ids))
            if category_ids:
                placeholders = ",".join("?" for _ in category_ids)
                clauses.append(f"setka_feed_posts.category_id IN ({placeholders})")
                args.extend(category_ids)
            if search_query:
                clauses.append("LOWER(setka_feed_posts.body) LIKE ?")
                args.append(f"%{search_query.lower()}%")

            order = "RANDOM()" if random_explore else "setka_feed_posts.created_at DESC"
            sql = f"""
                SELECT
                    setka_feed_posts.event_id,
                    setka_feed_posts.room_id,
                    setka_feed_posts.sender,
                    setka_feed_posts.created_at,
                    setka_feed_posts.msgtype,
                    setka_feed_posts.body,
                    setka_feed_posts.media_mxc,
                    setka_feed_posts.comment_count,
                    setka_feed_posts.view_count,
                    setka_feed_posts.like_count
                FROM setka_feed_posts
                INNER JOIN setka_published_channels
                    ON setka_feed_posts.room_id = setka_published_channels.room_id
                WHERE {" AND ".join(clauses)}
                ORDER BY {order}
                LIMIT ?
            """
            args.append(limit)
            txn.execute(sql, args)
            return list(txn.fetchall())

        rows = await self._store.db_pool.runInteraction("setka_feed_fetch_posts", _txn)
        return await self._hydrate_posts(user_id, rows)

    async def get_feed(
        self,
        user_id: str,
        *,
        mode: str,
        limit: int,
        since: str | None,
        search_query: str | None,
    ) -> JsonDict:
        since_ms = self._parse_since(since)
        if search_query:
            posts = await self._fetch_posts(
                user_id=user_id,
                limit=limit,
                since_ms=since_ms,
                room_ids=None,
                category_ids=None,
                search_query=search_query.strip(),
                random_explore=False,
            )
            return self._page_response(posts)

        if mode == "subscriptions":
            subscribed = await self._subscription_feed_room_ids(user_id)
            posts = await self._fetch_posts(
                user_id=user_id,
                limit=limit,
                since_ms=since_ms,
                room_ids=subscribed,
                category_ids=None,
                search_query=None,
                random_explore=False,
            )
            return self._page_response(posts)

        # recommended mix
        page_size = max(1, min(limit, 50))
        sub_count = max(1, int(page_size * 0.6))
        cat_count = max(0, int(page_size * 0.25))
        explore_count = max(0, page_size - sub_count - cat_count)

        subscribed = await self._user_subscribed_rooms(user_id)
        top_categories = await self._user_top_categories(user_id)

        posts: list[JsonDict] = []
        seen: set[str] = set()

        def _merge(batch: list[JsonDict]) -> None:
            for post in batch:
                event_id = str(post["event_id"])
                if event_id in seen:
                    continue
                seen.add(event_id)
                posts.append(post)

        if subscribed:
            _merge(
                await self._fetch_posts(
                    user_id=user_id,
                    limit=sub_count,
                    since_ms=since_ms,
                    room_ids=subscribed,
                    category_ids=None,
                    search_query=None,
                    random_explore=False,
                )
            )
        if top_categories and cat_count:
            _merge(
                await self._fetch_posts(
                    user_id=user_id,
                    limit=cat_count * 2,
                    since_ms=since_ms,
                    room_ids=None,
                    category_ids=top_categories,
                    search_query=None,
                    random_explore=False,
                )
            )
        if explore_count:
            _merge(
                await self._fetch_posts(
                    user_id=user_id,
                    limit=explore_count * 3,
                    since_ms=since_ms,
                    room_ids=None,
                    category_ids=None,
                    search_query=None,
                    random_explore=True,
                )
            )

        posts.sort(key=lambda p: int(p["created_at"]), reverse=True)
        posts = posts[:page_size]
        if len(posts) < page_size:
            random.shuffle(posts)
        return self._page_response(posts)

    def _page_response(self, posts: list[JsonDict]) -> JsonDict:
        next_batch = None
        if posts:
            last = posts[-1]
            next_batch = str(int(last["created_at"]))
        return {"posts": posts, "next_batch": next_batch}

    async def search_posts(
        self, user_id: str, *, query: str, limit: int, since: str | None
    ) -> JsonDict:
        return await self.get_feed(
            user_id,
            mode="recommended",
            limit=limit,
            since=since,
            search_query=query,
        )

    async def toggle_like(self, user_id: str, event_id: str) -> JsonDict:
        await self._ensure_tables()
        now_ms = self._clock.time_msec()

        def _txn(txn: Any) -> bool:
            txn.execute(
                "SELECT 1 FROM setka_feed_likes WHERE user_id = ? AND event_id = ?",
                (user_id, event_id),
            )
            if txn.fetchone():
                txn.execute(
                    "DELETE FROM setka_feed_likes WHERE user_id = ? AND event_id = ?",
                    (user_id, event_id),
                )
                txn.execute(
                    """
                    UPDATE setka_feed_posts
                    SET like_count = CASE WHEN like_count > 0 THEN like_count - 1 ELSE 0 END
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
                return False
            txn.execute(
                """
                INSERT INTO setka_feed_likes (user_id, event_id, liked_at)
                VALUES (?, ?, ?)
                """,
                (user_id, event_id, now_ms),
            )
            txn.execute(
                """
                UPDATE setka_feed_posts SET like_count = like_count + 1
                WHERE event_id = ?
                """,
                (event_id,),
            )
            return True

        liked = await self._store.db_pool.runInteraction("setka_feed_toggle_like", _txn)

        def _count(txn: Any) -> int:
            txn.execute(
                "SELECT like_count FROM setka_feed_posts WHERE event_id = ?",
                (event_id,),
            )
            row = txn.fetchone()
            return int(row[0]) if row else 0

        like_count = await self._store.db_pool.runInteraction(
            "setka_feed_like_count",
            _count,
        )
        return {"event_id": event_id, "liked": liked, "like_count": like_count, "emoji": HEART_EMOJI}

    async def record_view(self, user_id: str, event_id: str) -> JsonDict:
        await self._ensure_tables()
        now_ms = self._clock.time_msec()

        def _txn(txn: Any) -> int:
            txn.execute(
                "SELECT 1 FROM setka_feed_views WHERE user_id = ? AND event_id = ?",
                (user_id, event_id),
            )
            if txn.fetchone() is None:
                txn.execute(
                    """
                    INSERT INTO setka_feed_views (user_id, event_id, viewed_at)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, event_id, now_ms),
                )
                txn.execute(
                    """
                    UPDATE setka_feed_posts SET view_count = view_count + 1
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )
            txn.execute(
                "SELECT view_count FROM setka_feed_posts WHERE event_id = ?",
                (event_id,),
            )
            row = txn.fetchone()
            return int(row[0]) if row else 0

        view_count = await self._store.db_pool.runInteraction(
            "setka_feed_record_view",
            _txn,
        )
        return {"event_id": event_id, "view_count": view_count}
