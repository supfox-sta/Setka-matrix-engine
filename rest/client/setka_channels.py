# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_integer, parse_json_object_from_request, parse_string
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


def _decode_room_id(room_id: str) -> str:
    return unquote(room_id)


class SetkaChannelsPublicServlet(RestServlet):
    PATTERNS = client_patterns("/setka/channels/public$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_channels_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        limit = parse_integer(request, "limit", 40)
        since = parse_string(request, "since")
        query = parse_string(request, "q")
        if limit == 0:
            limit = None
        return 200, await self._handler.list_public_channels(
            limit=limit,
            since_token=since,
            search_query=query,
            user_id=requester.user.to_string(),
        )


class SetkaChannelsPublishServlet(RestServlet):
    PATTERNS = client_patterns("/setka/channels/(?P<room_id>[^/]*)/publish$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_channels_handler()

    async def on_POST(self, request: "SynapseRequest", room_id: str) -> tuple[int, "JsonDict"]:
        room_id = _decode_room_id(room_id)
        requester = await self._auth.get_user_by_req(request)
        await self._auth.check_user_in_room(room_id, requester)
        category_id: str | None = None
        try:
            body = parse_json_object_from_request(request)
            raw = body.get("category_id")
            if isinstance(raw, str) and raw.strip():
                category_id = raw.strip()
        except Exception:
            pass
        published = await self._handler.ensure_channel_published(
            room_id,
            user_id=requester.user.to_string(),
            category_id=category_id,
        )
        if not published:
            raise SynapseError(
                400,
                "Room is not a discoverable Setka channel (check topic and join rules)",
                Codes.INVALID_PARAM,
            )
        return 200, {"room_id": room_id, "published": True}

    async def on_DELETE(self, request: "SynapseRequest", room_id: str) -> tuple[int, "JsonDict"]:
        room_id = _decode_room_id(room_id)
        requester = await self._auth.get_user_by_req(request)
        await self._auth.check_user_in_room(room_id, requester)
        await self._handler.unpublish_channel(room_id)
        return 200, {"room_id": room_id, "published": False}


def register_servlets(hs: "HomeServer", http_server) -> None:
    hs.get_setka_channels_handler()
    SetkaChannelsPublishServlet(hs).register(http_server)
    SetkaChannelsPublicServlet(hs).register(http_server)
