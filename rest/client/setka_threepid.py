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


class RequestMsisdnEmailTokenBody(TypedDict):
    phone_number: str
    client_secret: str
    send_attempt: int
    next_link: str | None
    email: str | None


class SubmitMsisdnEmailTokenBody(TypedDict):
    sid: str
    client_secret: str
    token: str


class SetkaMsisdnRequestTokenEmailServlet(RestServlet):
    """Request a phone-number verification token, delivered by email."""

    PATTERNS = client_patterns("/setka/3pid/msisdn/requestTokenEmail$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_threepid_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)

        phone_number = body.get("phone_number")
        client_secret = body.get("client_secret")
        send_attempt = body.get("send_attempt")
        next_link = body.get("next_link")
        email = body.get("email")

        if not isinstance(phone_number, str):
            raise SynapseError(400, "phone_number must be a string", Codes.INVALID_PARAM)
        if not isinstance(client_secret, str) or not client_secret:
            raise SynapseError(400, "Missing client_secret", Codes.MISSING_PARAM)
        if not isinstance(send_attempt, int) or send_attempt <= 0:
            raise SynapseError(400, "send_attempt must be a positive integer", Codes.INVALID_PARAM)
        if next_link is not None and not isinstance(next_link, str):
            raise SynapseError(400, "next_link must be a string", Codes.INVALID_PARAM)
        if email is not None and not isinstance(email, str):
            raise SynapseError(400, "email must be a string", Codes.INVALID_PARAM)

        res = await self._handler.request_msisdn_token_via_email(
            requester_user_id=requester.user.to_string(),
            phone_number=phone_number,
            client_secret=client_secret,
            send_attempt=send_attempt,
            next_link=next_link,
            email=email,
        )
        return 200, dict(res)


class SetkaMsisdnSubmitTokenEmailServlet(RestServlet):
    """Submit the token and bind the phone number as a local msisdn 3PID."""

    PATTERNS = client_patterns("/setka/3pid/msisdn/submit_token$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_threepid_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)

        sid = body.get("sid")
        client_secret = body.get("client_secret")
        token = body.get("token")
        if not isinstance(sid, str) or not sid:
            raise SynapseError(400, "Missing sid", Codes.MISSING_PARAM)
        if not isinstance(client_secret, str) or not client_secret:
            raise SynapseError(400, "Missing client_secret", Codes.MISSING_PARAM)
        if not isinstance(token, str) or not token:
            raise SynapseError(400, "Missing token", Codes.MISSING_PARAM)

        await self._handler.submit_msisdn_token_and_bind(
            requester_user_id=requester.user.to_string(),
            sid=sid,
            client_secret=client_secret,
            token=token,
        )
        return 200, {}


class SetkaThreepidDeleteBody(TypedDict):
    medium: str
    address: str


class SetkaThreepidDeleteServlet(RestServlet):
    """Delete a local 3PID association for the current user (Setka extension).

    Some deployments disable upstream 3PID changes or proxy them differently. We provide
    a narrow, authenticated endpoint to remove the local association.
    """

    PATTERNS = client_patterns("/setka/3pid/delete$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._auth_handler = hs.get_auth_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        if not isinstance(body, dict):
            raise SynapseError(400, "Body must be a JSON object", Codes.INVALID_PARAM)

        medium = body.get("medium")
        address = body.get("address")
        if medium not in ("email", "msisdn"):
            raise SynapseError(400, "Invalid medium", Codes.INVALID_PARAM)
        if not isinstance(address, str) or not address.strip():
            raise SynapseError(400, "Invalid address", Codes.INVALID_PARAM)

        await self._auth_handler.delete_local_threepid(
            requester.user.to_string(), medium, address.strip()
        )
        return 200, {}


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaMsisdnRequestTokenEmailServlet(hs).register(http_server)
    SetkaMsisdnSubmitTokenEmailServlet(hs).register(http_server)
    SetkaThreepidDeleteServlet(hs).register(http_server)
