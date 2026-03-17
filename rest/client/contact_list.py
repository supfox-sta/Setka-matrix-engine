#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import TYPE_CHECKING

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, RoomID

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer


_ALLOWED_METADATA_KEYS = {
    "display_name",
    "avatar_url",
    "note",
    "order",
    "tags",
    "pinned",
    "first_name",
    "last_name",
    "email",
    "phone",
}


def _validate_metadata(metadata: object) -> JsonDict:
    if not isinstance(metadata, dict):
        raise SynapseError(
            400,
            "Contact metadata must be a JSON object",
            Codes.INVALID_PARAM,
        )

    normalized: JsonDict = {}

    for key, value in metadata.items():
        if key not in _ALLOWED_METADATA_KEYS:
            raise SynapseError(
                400,
                f"Unknown contact metadata field: {key}",
                Codes.INVALID_PARAM,
            )

        if key in {"display_name", "avatar_url", "note"}:
            if not isinstance(value, str):
                raise SynapseError(
                    400,
                    f"{key} must be a string",
                    Codes.INVALID_PARAM,
                )
            normalized[key] = value
        elif key in {"first_name", "last_name", "email", "phone"}:
            if not isinstance(value, str):
                raise SynapseError(
                    400,
                    f"{key} must be a string",
                    Codes.INVALID_PARAM,
                )
            normalized[key] = value
        elif key == "order":
            if not isinstance(value, int):
                raise SynapseError(
                    400,
                    "order must be an integer",
                    Codes.INVALID_PARAM,
                )
            normalized[key] = value
        elif key == "tags":
            if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
                raise SynapseError(
                    400,
                    "tags must be a list of strings",
                    Codes.INVALID_PARAM,
                )
            normalized[key] = value
        elif key == "pinned":
            if not isinstance(value, bool):
                raise SynapseError(
                    400,
                    "pinned must be a boolean",
                    Codes.INVALID_PARAM,
                )
            normalized[key] = value

    return normalized


class ContactListServlet(RestServlet):
    """
    GET /user/{user_id}/contact_list HTTP/1.1
    """

    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/contact_list$")
    CATEGORY = "Contact list requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self.handler = hs.get_contact_list_handler()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot read contact list for other users.")

        return 200, await self.handler.get_contact_list(user_id)


class ContactMetadataServlet(RestServlet):
    """
    PUT /user/{user_id}/contact_list/rooms/{room_id}
    DELETE /user/{user_id}/contact_list/rooms/{room_id}
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/contact_list/rooms/(?P<room_id>[^/]*)"
    )
    CATEGORY = "Contact list requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_contact_list_handler()
        self.room_member_handler = hs.get_room_member_handler()

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot edit contact metadata for other users.")

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
        metadata = _validate_metadata(body)

        await self.handler.update_contact_metadata(user_id, room_id, metadata)

        return 200, metadata

    async def on_GET(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot read contact metadata for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        metadata = await self.handler.get_contact_metadata(user_id, room_id)
        return 200, metadata

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise AuthError(403, "Cannot edit contact metadata for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        await self.room_member_handler.check_for_any_membership_in_room(
            user_id=user_id, room_id=room_id
        )

        await self.handler.remove_contact_metadata(user_id, room_id)

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ContactListServlet(hs).register(http_server)
    ContactMetadataServlet(hs).register(http_server)
