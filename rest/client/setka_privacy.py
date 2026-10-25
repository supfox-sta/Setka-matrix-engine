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
    call_visibility: Literal["everyone", "contacts", "nobody"]


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
        for key in (
            "email_visibility",
            "phone_visibility",
            "last_seen_visibility",
            "call_visibility",
        ):
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


def _lookup_variants(medium: str, address: str) -> list[str]:
    """Address spellings to try. Phones are stored in E.164 (+7999...) but clients send them
    as '+7 (999) 123-45-67', '79991234567' or '8 999 ...'; emails are matched case-insensitively."""
    address = address.strip()
    if medium == "email":
        out = [address]
        if address.lower() != address:
            out.append(address.lower())
        return out
    digits = "".join(ch for ch in address if ch.isdigit())
    if not digits:
        return []
    if not address.lstrip().startswith("+"):
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits
    return ["+" + digits, digits]


async def _resolve_threepid(store, sydent_handler, medium: str, address: str):
    for variant in _lookup_variants(medium, address):
        user_id = await store.get_user_id_by_threepid(medium, variant)
        if user_id is None:
            user_id = await sydent_handler.lookup_mxid(medium, variant)
        if user_id is not None:
            return user_id
    return None


class SetkaThreepidLookupRestServlet(RestServlet):
    """Lookup an mxid from a 3pid, with Setka privacy rules applied."""

    PATTERNS = client_patterns("/setka/3pid/lookup$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._privacy_handler = hs.get_setka_privacy_handler()
        self._sydent_handler = hs.get_setka_sydent_handler()

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

        user_id = await _resolve_threepid(self._store, self._sydent_handler, medium, address)
        if user_id is None:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)

        privacy = await self._privacy_handler.get_privacy(user_id)
        if medium == "email" and not privacy["discoverable_email"]:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)
        if medium == "msisdn" and not privacy["discoverable_msisdn"]:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)

        return 200, {"mxid": user_id}


class SetkaPublicThreepidLookupRestServlet(RestServlet):
    """Anonymous 3PID -> mxid lookup for the public share page (share.setka-matrix.ru).

    Only users who left "discoverable" on are returned; requests are limited per IP so the
    endpoint cannot be used to enumerate the user base.
    """

    PATTERNS = client_patterns("/setka/3pid/public_lookup$", releases=(), unstable=True)
    CATEGORY = "Setka 3PID"
    _WINDOW_S = 60.0
    _MAX_PER_WINDOW = 15

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._store = hs.get_datastores().main
        self._privacy_handler = hs.get_setka_privacy_handler()
        self._sydent_handler = hs.get_setka_sydent_handler()
        self._clock = hs.get_clock()
        self._hits: dict[str, list[float]] = {}

    def _check_rate(self, ip: str) -> None:
        now = self._clock.time()
        recent = [t for t in self._hits.get(ip, []) if now - t < self._WINDOW_S]
        if len(recent) >= self._MAX_PER_WINDOW:
            self._hits[ip] = recent
            raise SynapseError(429, "Too many requests", Codes.LIMIT_EXCEEDED)
        recent.append(now)
        self._hits[ip] = recent
        if len(self._hits) > 5000:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self._WINDOW_S}

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        self._check_rate(request.getClientAddress().host)
        body = parse_json_object_from_request(request)
        medium = body.get("medium")
        address = body.get("address")
        if medium not in ("email", "msisdn") or not isinstance(address, str) or not address.strip():
            raise SynapseError(400, "Invalid medium or address", Codes.INVALID_PARAM)

        user_id = await _resolve_threepid(self._store, self._sydent_handler, medium, address)
        if user_id is None:
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)
        privacy = await self._privacy_handler.get_privacy(user_id)
        flag = "discoverable_email" if medium == "email" else "discoverable_msisdn"
        visibility = privacy["email_visibility"] if medium == "email" else privacy["phone_visibility"]
        if not privacy[flag] or visibility == "nobody":
            raise SynapseError(404, "Not found", Codes.NOT_FOUND)
        return 200, {"mxid": user_id}


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaPrivacyRestServlet(hs).register(http_server)
    SetkaThreepidLookupRestServlet(hs).register(http_server)
    SetkaPublicThreepidLookupRestServlet(hs).register(http_server)
