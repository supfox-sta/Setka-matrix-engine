# This file is licensed under the Affero General Public License (AGPL) version 3.

# Copyright (C) 2026 Setka Matrix



from __future__ import annotations



from typing import TYPE_CHECKING

from urllib.parse import unquote



from synapse.http.servlet import RestServlet, parse_integer, parse_json_object_from_request

from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin

from synapse.types import JsonDict



if TYPE_CHECKING:

    from synapse.http.site import SynapseRequest

    from synapse.server import HomeServer





def _decode_room_id(room_id: str) -> str:

    return unquote(room_id)





class SetkaChannelsRebuildServlet(RestServlet):

    PATTERNS = admin_patterns("/setka/channels/rebuild$")



    def __init__(self, hs: "HomeServer"):

        self._auth = hs.get_auth()

        self._handler = hs.get_setka_channels_handler()



    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:

        await assert_requester_is_admin(self._auth, request)

        return 200, await self._handler.rebuild_registry()





class SetkaChannelsListServlet(RestServlet):

    PATTERNS = admin_patterns("/setka/channels$")



    def __init__(self, hs: "HomeServer"):

        self._auth = hs.get_auth()

        self._handler = hs.get_setka_channels_handler()



    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:

        await assert_requester_is_admin(self._auth, request)

        limit = parse_integer(request, "limit", default=100)

        return 200, await self._handler.list_published_channels_admin(limit=limit)





class SetkaChannelsRoomServlet(RestServlet):

    PATTERNS = admin_patterns("/setka/channels/(?P<room_id>[^/]*)$")



    def __init__(self, hs: "HomeServer"):

        self._auth = hs.get_auth()

        self._handler = hs.get_setka_channels_handler()



    async def on_PUT(self, request: "SynapseRequest", room_id: str) -> tuple[int, JsonDict]:

        await assert_requester_is_admin(self._auth, request)

        room_id = _decode_room_id(room_id)

        body = parse_json_object_from_request(request)

        hidden = body.get("hidden")

        if not isinstance(hidden, bool):

            from synapse.api.errors import Codes, SynapseError



            raise SynapseError(400, "hidden must be a boolean", Codes.INVALID_PARAM)

        return 200, await self._handler.set_channel_hidden(room_id, hidden=hidden)



    async def on_DELETE(self, request: "SynapseRequest", room_id: str) -> tuple[int, JsonDict]:

        await assert_requester_is_admin(self._auth, request)

        room_id = _decode_room_id(room_id)

        await self._handler.unpublish_channel(room_id)

        return 200, {"room_id": room_id, "published": False}





def register_servlets(hs: "HomeServer", http_server: object) -> None:

    SetkaChannelsRebuildServlet(hs).register(http_server)

    SetkaChannelsListServlet(hs).register(http_server)

    SetkaChannelsRoomServlet(hs).register(http_server)


