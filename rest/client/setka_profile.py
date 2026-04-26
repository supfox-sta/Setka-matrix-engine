# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


class SetkaProfileBody(TypedDict, total=False):
    bio: str
    color: str
    badge_emoji_mxc: str
    status_emoji_mxc: str
    background_mxc: str


class SetkaMyProfileRestServlet(RestServlet):
    """Get / set Setka profile extras for the current user."""

    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_profile$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_profile_handler()

    async def on_GET(self, request: "SynapseRequest", user_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise SynapseError(403, "Forbidden", Codes.FORBIDDEN)
        data = await self._handler.get_profile(
            user_id,
            requester_user_id=user_id,
            include_private=True,
        )
        return 200, dict(data)

    async def on_PUT(self, request: "SynapseRequest", user_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise SynapseError(403, "Forbidden", Codes.FORBIDDEN)
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)
        content: SetkaProfileBody = {}
        if "bio" in body:
            if not isinstance(body["bio"], str):
                raise SynapseError(400, "bio must be a string", Codes.INVALID_PARAM)
            content["bio"] = body["bio"]
        if "color" in body:
            if body["color"] is not None and not isinstance(body["color"], str):
                raise SynapseError(400, "color must be a string", Codes.INVALID_PARAM)
            content["color"] = body.get("color") or ""
        if "badge_emoji_mxc" in body:
            if body["badge_emoji_mxc"] is not None and not isinstance(body["badge_emoji_mxc"], str):
                raise SynapseError(400, "badge_emoji_mxc must be a string", Codes.INVALID_PARAM)
            content["badge_emoji_mxc"] = body.get("badge_emoji_mxc") or ""
        if "status_emoji_mxc" in body:
            if body["status_emoji_mxc"] is not None and not isinstance(body["status_emoji_mxc"], str):
                raise SynapseError(400, "status_emoji_mxc must be a string", Codes.INVALID_PARAM)
            content["status_emoji_mxc"] = body.get("status_emoji_mxc") or ""
        if "background_mxc" in body:
            if body["background_mxc"] is not None and not isinstance(body["background_mxc"], str):
                raise SynapseError(400, "background_mxc must be a string", Codes.INVALID_PARAM)
            content["background_mxc"] = body.get("background_mxc") or ""
        data = await self._handler.set_profile(user_id, dict(content))
        return 200, dict(data)


class SetkaPublicProfileRestServlet(RestServlet):
    """Read Setka profile extras for any user (authenticated)."""

    PATTERNS = client_patterns("/profile/(?P<user_id>[^/]*)/setka_profile$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_profile_handler()

    async def on_GET(self, request: "SynapseRequest", user_id: str) -> tuple[int, "JsonDict"]:
        await self._auth.get_user_by_req(request)
        requester = await self._auth.get_user_by_req(request)
        data = await self._handler.get_profile(
            user_id,
            requester_user_id=requester.user.to_string(),
            include_private=False,
        )
        return 200, dict(data)


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaMyProfileRestServlet(hs).register(http_server)
    SetkaPublicProfileRestServlet(hs).register(http_server)
