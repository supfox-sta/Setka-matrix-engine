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

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SetkaPlusUserServlet(RestServlet):
    PATTERNS = admin_patterns("/setka_plus/users/(?P<user_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()
        self.profile_handler = hs.get_setka_profile_handler()
        self.privacy_handler = hs.get_setka_privacy_handler()
        self.contact_list_handler = hs.get_contact_list_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        self._validate_user_id(user_id)

        subscription = await self.handler.get_subscription(user_id)
        packs = await self.handler.get_sticker_packs(user_id)
        payments = await self.handler.get_payments(user_id)
        profile = await self.profile_handler.get_profile(
            user_id,
            requester_user_id=user_id,
            include_private=True,
        )
        privacy = await self.privacy_handler.get_privacy(user_id)
        contacts = await self.contact_list_handler.get_contact_list(user_id)
        rooms = contacts.get("rooms")
        contacts_count = len(rooms) if isinstance(rooms, dict) else 0
        return 200, {
            "user_id": user_id,
            "subscription": subscription,
            "payments": payments,
            "sticker_packs_count": len(packs),
            "sticker_packs": packs,
            "profile": profile,
            "privacy": privacy,
            "contacts_count": contacts_count,
            "contacts": contacts,
        }

    async def on_PUT(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        self._validate_user_id(user_id)

        body = parse_json_object_from_request(request)
        subscription = await self.handler.set_subscription(user_id, body)
        return 200, {"user_id": user_id, "subscription": subscription}

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        # Some proxies block PUT/PATCH by default; keep POST as a compatibility path.
        return await self.on_PUT(request, user_id)

    def _validate_user_id(self, user_id: str) -> None:
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)


class SetkaPlusActivateUserServlet(RestServlet):
    PATTERNS = admin_patterns("/setka_plus/users/(?P<user_id>[^/]*)/activate$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)

        body = parse_json_object_from_request(request, allow_empty_body=True)
        payment_id = body.get("payment_id", "admin_activation")
        if not isinstance(payment_id, str):
            raise SynapseError(400, "payment_id must be a string", Codes.INVALID_PARAM)
        provider = body.get("provider", "admin")
        if not isinstance(provider, str):
            raise SynapseError(400, "provider must be a string", Codes.INVALID_PARAM)
        amount = body.get("amount", 0)
        if not isinstance(amount, (int, float)):
            raise SynapseError(400, "amount must be numeric", Codes.INVALID_PARAM)
        plan_id = body.get("plan_id")
        if plan_id is not None and not isinstance(plan_id, str):
            raise SynapseError(400, "plan_id must be a string", Codes.INVALID_PARAM)

        subscription = await self.handler.activate_subscription(
            user_id,
            payment_id=payment_id,
            payment_provider=provider,
            amount=float(amount) if amount else 0.0,
            plan_id=plan_id,
            currency="RUB",
        )
        return 200, {"user_id": user_id, "subscription": subscription}


class SetkaPlusPaymentsServlet(RestServlet):
    PATTERNS = admin_patterns("/setka_plus/users/(?P<user_id>[^/]*)/payments$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        payments = await self.handler.get_payments(user_id)
        return 200, {"user_id": user_id, "payments": payments}


class SetkaPlusPlansServlet(RestServlet):
    PATTERNS = admin_patterns("/setka_plus/plans$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        plans = await self.handler.get_plans()
        return 200, {"plans": plans}

    async def on_PUT(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        body = parse_json_object_from_request(request)
        plans = await self.handler.set_plans(body.get("plans"))
        return 200, {"plans": plans}

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        return await self.on_PUT(request)


class SetkaAdminProfileServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/profile$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.profile_handler = hs.get_setka_profile_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        profile = await self.profile_handler.get_profile(
            user_id,
            requester_user_id=user_id,
            include_private=True,
        )
        return 200, {"user_id": user_id, "profile": profile}

    async def on_PUT(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        body = parse_json_object_from_request(request)
        profile = await self.profile_handler.set_profile(user_id, body)
        return 200, {"user_id": user_id, "profile": profile}

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        return await self.on_PUT(request, user_id)


class SetkaAdminPrivacyServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/privacy$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.privacy_handler = hs.get_setka_privacy_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        privacy = await self.privacy_handler.get_privacy(user_id)
        return 200, {"user_id": user_id, "privacy": privacy}

    async def on_PUT(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        body = parse_json_object_from_request(request)
        privacy = await self.privacy_handler.set_privacy(user_id, body)
        return 200, {"user_id": user_id, "privacy": privacy}

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        return await self.on_PUT(request, user_id)


class SetkaAdminStickerPacksServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/sticker_packs$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        packs = await self.handler.get_sticker_packs(user_id)
        return 200, {
            "user_id": user_id,
            "count": len(packs),
            "sticker_packs": packs,
        }


class SetkaAdminStickerPackDetailServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/sticker_packs/(?P<pack_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str, pack_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        packs = await self.handler.get_sticker_packs(user_id)
        pack = next((item for item in packs if item.get("id") == pack_id), None)
        if not isinstance(pack, dict):
            raise SynapseError(404, "Sticker pack not found", Codes.NOT_FOUND)
        return 200, {"user_id": user_id, "pack": pack}

    async def on_PUT(self, request: SynapseRequest, user_id: str, pack_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        body = parse_json_object_from_request(request)
        pack = await self.handler.upsert_sticker_pack(user_id, pack_id, body)
        return 200, {"user_id": user_id, "pack": pack}

    async def on_POST(self, request: SynapseRequest, user_id: str, pack_id: str) -> tuple[int, JsonDict]:
        return await self.on_PUT(request, user_id, pack_id)

    async def on_DELETE(self, request: SynapseRequest, user_id: str, pack_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        await self.handler.delete_sticker_pack(user_id, pack_id)
        return 200, {"user_id": user_id, "deleted": pack_id}


class SetkaAdminStickerPackShareServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/sticker_packs/(?P<pack_id>[^/]*)/share$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_POST(self, request: SynapseRequest, user_id: str, pack_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        token = await self.handler.create_share_token(user_id, pack_id)
        pack = await self.handler.resolve_shared_pack(token)
        return 200, {
            "user_id": user_id,
            "pack_id": pack_id,
            "token": token,
            "url": self.handler.build_share_url(token),
            "pack": pack,
        }


class SetkaAdminContactsServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/users/(?P<user_id>[^/]*)/contacts$")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.contact_list_handler = hs.get_contact_list_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        if not UserID.is_valid(user_id):
            raise SynapseError(400, "Invalid user_id", Codes.INVALID_PARAM)
        contacts = await self.contact_list_handler.get_contact_list(user_id)
        rooms = contacts.get("rooms")
        return 200, {
            "user_id": user_id,
            "count": len(rooms) if isinstance(rooms, dict) else 0,
            "contacts": contacts,
        }


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SetkaPlusUserServlet(hs).register(http_server)
    SetkaPlusActivateUserServlet(hs).register(http_server)
    SetkaPlusPaymentsServlet(hs).register(http_server)
    SetkaPlusPlansServlet(hs).register(http_server)
    SetkaAdminProfileServlet(hs).register(http_server)
    SetkaAdminPrivacyServlet(hs).register(http_server)
    SetkaAdminStickerPacksServlet(hs).register(http_server)
    SetkaAdminStickerPackDetailServlet(hs).register(http_server)
    SetkaAdminStickerPackShareServlet(hs).register(http_server)
    SetkaAdminContactsServlet(hs).register(http_server)
