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

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)
        self._validate_user_id(user_id)

        subscription = await self.handler.get_subscription(user_id)
        packs = await self.handler.get_sticker_packs(user_id)
        return 200, {
            "user_id": user_id,
            "subscription": subscription,
            "sticker_packs_count": len(packs),
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


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SetkaPlusUserServlet(hs).register(http_server)
    SetkaPlusActivateUserServlet(hs).register(http_server)
    SetkaPlusPaymentsServlet(hs).register(http_server)
    SetkaPlusPlansServlet(hs).register(http_server)
