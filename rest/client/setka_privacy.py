# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


class SetkaPrivacyBody(TypedDict, total=False):
    discoverable_email: bool
    discoverable_msisdn: bool
    email_visibility: Literal["everyone", "contacts", "nobody"]
    phone_visibility: Literal["everyone", "contacts", "nobody"]
    last_seen_visibility: Literal["everyone", "contacts", "nobody"]


class SetkaPrivacyRestServlet(RestServlet):
    """Get / set Setka privacy settings for a user."""

    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_privacy$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_privacy_handler()

    async def on_GET(self, request: "SynapseRequest", user_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise SynapseError(403, "Forbidden", Codes.FORBIDDEN)
        privacy = await self._handler.get_privacy(user_id)
        return 200, dict(privacy)

    async def on_PUT(self, request: "SynapseRequest", user_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        if requester.user.to_string() != user_id:
            raise SynapseError(403, "Forbidden", Codes.FORBIDDEN)
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)
        content: SetkaPrivacyBody = {}
        if "discoverable_email" in body:
            if not isinstance(body["discoverable_email"], bool):
                raise SynapseError(400, "discoverable_email must be boolean", Codes.INVALID_PARAM)
            content["discoverable_email"] = body["discoverable_email"]
        if "discoverable_msisdn" in body:
            if not isinstance(body["discoverable_msisdn"], bool):
                raise SynapseError(400, "discoverable_msisdn must be boolean", Codes.INVALID_PARAM)
            content["discoverable_msisdn"] = body["discoverable_msisdn"]
        for key in ("email_visibility", "phone_visibility", "last_seen_visibility"):
            if key in body:
                value = body[key]
                if value not in ("everyone", "contacts", "nobody"):
                    raise SynapseError(400, f"{key} must be one of everyone/contacts/nobody", Codes.INVALID_PARAM)
                content[key] = value
        privacy = await self._handler.set_privacy(user_id, dict(content))
        return 200, dict(privacy)


class SetkaThreepidLookupBody(TypedDict):
    medium: Literal["email", "msisdn"]
    address: str


class SetkaThreepidLookupRestServlet(RestServlet):
    """Lookup an mxid from a 3pid, with Setka privacy rules applied."""

    PATTERNS = client_patterns("/setka/3pid/lookup$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._privacy_handler = hs.get_setka_privacy_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)
        await self._auth.get_user_by_req(request)

        medium = body.get("medium")
        address = body.get("address")
        if medium not in ("email", "msisdn"):
            raise SynapseError(400, "Invalid medium", Codes.INVALID_PARAM)
        if not isinstance(address, str):
            raise SynapseError(400, "Invalid address", Codes.INVALID_PARAM)
        address = address.strip()
        if not address:
            raise SynapseError(400, "Missing address", Codes.MISSING_PARAM)
        if medium == "msisdn" and not address.startswith("+"):
            raise SynapseError(400, "Phone must be in E.164 format", Codes.INVALID_PARAM)

        user_id = await self._store.get_user_id_by_threepid(medium, address)
        if user_id is None:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)

        privacy = await self._privacy_handler.get_privacy(user_id)
        if medium == "email" and not privacy["discoverable_email"]:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)
        if medium == "msisdn" and not privacy["discoverable_msisdn"]:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)

        return 200, {"mxid": user_id}


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaPrivacyRestServlet(hs).register(http_server)
    SetkaThreepidLookupRestServlet(hs).register(http_server)
