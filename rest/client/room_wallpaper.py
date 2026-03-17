# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import TYPE_CHECKING

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, RoomID

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer


class RoomWallpaperListServlet(RestServlet):
    """
    GET /user/{user_id}/room_wallpaper
    """

    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/room_wallpaper$")
    CATEGORY = "Room wallpaper"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_room_wallpaper_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot read room wallpaper for other users.")

        wallpapers = await self.handler.get_wallpapers(user_id)
        return 200, {"rooms": wallpapers}


class RoomWallpaperMetadataServlet(RestServlet):
    """
    GET/PUT/DELETE /user/{user_id}/room_wallpaper/rooms/{room_id}
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/room_wallpaper/rooms/(?P<room_id>[^/]*)"
    )
    CATEGORY = "Room wallpaper"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_room_wallpaper_handler()
        self.room_member_handler = hs.get_room_member_handler()

    async def on_GET(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot read room wallpaper for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        metadata = await self.handler.get_wallpaper(user_id, room_id)
        return 200, metadata

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot edit room wallpaper for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(
                400,
                "Wallpaper metadata must be a JSON object",
                Codes.INVALID_PARAM,
            )

        await self.handler.update_wallpaper(user_id, room_id, body)
        return 200, {}

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot edit room wallpaper for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        await self.handler.remove_wallpaper(user_id, room_id)
        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    RoomWallpaperListServlet(hs).register(http_server)
    RoomWallpaperMetadataServlet(hs).register(http_server)
