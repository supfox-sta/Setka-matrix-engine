# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka Lite — paginated sync API for slow connections."""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

from synapse.api.constants import Direction, EventTypes
from synapse.api.errors import Codes, SynapseError
from synapse.events.utils import SerializeEventConfig
from synapse.streams.config import PaginationConfig
from synapse.types import JsonDict, StreamToken

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.types import Requester


class SetkaLiteHandler:
    DEFAULT_EVENTS_PAGE = 15
    DEFAULT_ROOMS_PAGE = 30
    MAX_PROBE_BYTES = 65536

    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._pagination_handler = hs.get_pagination_handler()
        self._event_serializer = hs.get_event_client_serializer()
        self._clock = hs.get_clock()

    def get_config(self) -> JsonDict:
        return {
            "enabled": True,
            "max_events_per_page": self.DEFAULT_EVENTS_PAGE,
            "max_rooms_per_page": self.DEFAULT_ROOMS_PAGE,
            "max_event_pages_per_room": 3,
            "timeline_refresh_pages": 5,
            "probe_max_bytes": self.MAX_PROBE_BYTES,
            "max_kbps": 128,
            "page_delay_ms": 150,
        }

    def build_probe_payload(self, size: int) -> JsonDict:
        clamped = max(0, min(size, self.MAX_PROBE_BYTES))
        data = os.urandom(clamped)
        return {
            "size": clamped,
            "data": base64.b64encode(data).decode("ascii"),
        }

    async def list_rooms(
        self,
        user_id: str,
        *,
        limit: int,
        since: str | None,
    ) -> JsonDict:
        room_ids = sorted(await self._store.get_rooms_for_user(user_id))
        offset = 0
        if since:
            try:
                offset = max(0, int(since))
            except ValueError as exc:
                raise SynapseError(400, "Invalid since token", Codes.INVALID_PARAM) from exc

        page_limit = max(1, min(limit, 100))
        page_ids = room_ids[offset : offset + page_limit]
        rooms: list[JsonDict] = []
        for room_id in page_ids:
            rooms.append(
                {
                    "room_id": room_id,
                    "name": await self._resolve_room_name(room_id),
                }
            )

        next_offset = offset + len(page_ids)
        has_more = next_offset < len(room_ids)
        return {
            "rooms": rooms,
            "has_more": has_more,
            "next_since": str(next_offset) if has_more else "",
        }

    async def get_room_events(
        self,
        requester: "Requester",
        room_id: str,
        *,
        limit: int,
        before: str | None,
    ) -> JsonDict:
        from_token: StreamToken | None = None
        if before:
            try:
                from_token = await StreamToken.from_string(self._store, before)
            except Exception as exc:
                raise SynapseError(400, "Invalid before token", Codes.INVALID_PARAM) from exc

        page_limit = max(1, min(limit, 50))
        pagin_config = PaginationConfig(
            from_token=from_token,
            to_token=None,
            direction=Direction.BACKWARDS,
            limit=page_limit,
        )
        result = await self._pagination_handler.get_messages(
            requester=requester,
            room_id=room_id,
            pagin_config=pagin_config,
            as_client_event=True,
        )
        serialize_options = SerializeEventConfig(
            as_client_event=True,
            requester=requester,
        )
        time_now = self._clock.time_msec()
        events = await self._event_serializer.serialize_events(
            result.messages_chunk,
            time_now,
            config=serialize_options,
        )
        prev_token = ""
        if result.end_token is not None:
            prev_token = await result.end_token.to_string(self._store)
        has_more = result.end_token is not None and len(events) >= page_limit
        return {
            "events": events,
            "has_more": has_more,
            "prev_token": prev_token,
        }

    async def _resolve_room_name(self, room_id: str) -> str:
        name_event = await self._store.get_current_state_event(
            room_id, EventTypes.Name, ""
        )
        if name_event is not None:
            name = name_event.content.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return room_id

    async def get_to_device_events(
        self,
        user_id: str,
        device_id: str,
        *,
        since: str | None,
        limit: int,
    ) -> JsonDict:
        from_stream_id = 0
        if since:
            try:
                from_stream_id = max(0, int(since))
            except ValueError as exc:
                raise SynapseError(400, "Invalid since token", Codes.INVALID_PARAM) from exc

        page_limit = max(1, min(limit, 100))
        to_stream_id = self._store.get_to_device_stream_token()
        messages, last_stream_id = await self._store.get_messages_for_device(
            user_id,
            device_id,
            from_stream_id,
            to_stream_id,
            limit=page_limit,
        )
        has_more = last_stream_id < to_stream_id
        return {
            "events": messages,
            "has_more": has_more,
            "next_since": str(last_stream_id) if has_more else "",
        }
