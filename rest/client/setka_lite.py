# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_integer, parse_string
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


class SetkaLiteConfigRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/config$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_lite_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        await self._auth.get_user_by_req(request)
        return 200, self._handler.get_config()


class SetkaLitePingRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/ping$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        await self._auth.get_user_by_req(request)
        return 200, {"ok": True}


class SetkaLiteProbeRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/probe$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_lite_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        await self._auth.get_user_by_req(request)
        size = parse_integer(request, "size", default=16384)
        return 200, self._handler.build_probe_payload(size)


class SetkaLiteRoomsRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/rooms$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_lite_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        limit = parse_integer(request, "limit", default=self._handler.DEFAULT_ROOMS_PAGE)
        since = parse_string(request, "since")
        data = await self._handler.list_rooms(
            requester.user.to_string(),
            limit=limit,
            since=since,
        )
        return 200, data


class SetkaLiteRoomEventsRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/rooms/(?P<room_id>[^/]+)/events$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_lite_handler()

    async def on_GET(
        self, request: "SynapseRequest", room_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if not room_id.startswith("!"):
            raise SynapseError(400, "Invalid room ID", Codes.INVALID_PARAM)
        limit = parse_integer(request, "limit", default=self._handler.DEFAULT_EVENTS_PAGE)
        before = parse_string(request, "before")
        data = await self._handler.get_room_events(
            requester,
            room_id,
            limit=limit,
            before=before,
        )
        return 200, data


class SetkaLiteToDeviceRestServlet(RestServlet):
    PATTERNS = client_patterns("/setka_lite/crypto/to_device$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_lite_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if not requester.device_id:
            raise SynapseError(400, "Device ID required", Codes.MISSING_PARAM)
        since = parse_string(request, "since")
        limit = parse_integer(request, "limit", default=50)
        data = await self._handler.get_to_device_events(
            requester.user.to_string(),
            requester.device_id,
            since=since,
            limit=limit,
        )
        return 200, data


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaLiteConfigRestServlet(hs).register(http_server)
    SetkaLitePingRestServlet(hs).register(http_server)
    SetkaLiteProbeRestServlet(hs).register(http_server)
    SetkaLiteRoomsRestServlet(hs).register(http_server)
    SetkaLiteRoomEventsRestServlet(hs).register(http_server)
    SetkaLiteToDeviceRestServlet(hs).register(http_server)
