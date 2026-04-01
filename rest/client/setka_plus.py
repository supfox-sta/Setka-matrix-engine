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

import re
import uuid
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer


async def _assert_self(auth, request: SynapseRequest, user_id: str) -> None:
    requester = await auth.get_user_by_req(request)
    if requester.user.to_string() != user_id:
        raise AuthError(403, "Cannot access Setka Plus data for other users.")


class SetkaPlusSubscriptionServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/subscription$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        return 200, await self.handler.get_subscription(user_id)


class SetkaPlusPlansServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/plans$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        plans = await self.handler.get_plans()
        return 200, {"plans": plans}


class SetkaPlusStickerPacksServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/sticker_packs$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        packs = await self.handler.get_sticker_packs(user_id)
        return 200, {"packs": packs}

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        body = parse_json_object_from_request(request)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SynapseError(400, "Sticker pack name is required", Codes.MISSING_PARAM)
        pack_id = body.get("id")
        if not isinstance(pack_id, str) or not pack_id.strip():
            pack_id = uuid.uuid4().hex
        payload: JsonDict = {
            "name": name.strip(),
            "kind": body.get("kind"),
            "stickers": body.get("stickers", []),
        }
        created = await self.handler.upsert_sticker_pack(user_id, pack_id.strip(), payload)
        return 200, created


class SetkaPlusStickerPackServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/sticker_packs/(?P<pack_id>[^/]*)$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, pack_id: str
    ) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        body = parse_json_object_from_request(request)
        updated = await self.handler.upsert_sticker_pack(user_id, pack_id, body)
        return 200, updated

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, pack_id: str
    ) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        await self.handler.delete_sticker_pack(user_id, pack_id)
        return 200, {}


class SetkaPlusPaymentsServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/payments$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_GET(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        payments = await self.handler.get_payments(user_id)
        return 200, {"payments": payments}


class SetkaPlusYooMoneyCreateServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/payments/yoomoney/create$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        body = parse_json_object_from_request(request, allow_empty_body=True)
        amount = body.get("amount")
        if amount is not None and not isinstance(amount, (int, float)):
            raise SynapseError(400, "amount must be numeric", Codes.INVALID_PARAM)
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            raise SynapseError(400, "description must be a string", Codes.INVALID_PARAM)
        plan_id = body.get("plan_id")
        if plan_id is not None and not isinstance(plan_id, str):
            raise SynapseError(400, "plan_id must be a string", Codes.INVALID_PARAM)
        response = await self.handler.create_yoomoney_payment_request(
            user_id,
            amount=float(amount) if amount is not None else None,
            description=description,
            plan_id=plan_id,
        )
        return 200, response


class SetkaPlusYooMoneyProcessServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/setka_plus/payments/yoomoney/process$")
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.handler = hs.get_setka_plus_handler()

    async def on_POST(self, request: SynapseRequest, user_id: str) -> tuple[int, JsonDict]:
        await _assert_self(self.auth, request, user_id)
        body = parse_json_object_from_request(request)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise SynapseError(400, "request_id is required", Codes.MISSING_PARAM)
        money_source = body.get("money_source", "wallet")
        if not isinstance(money_source, str) or not money_source.strip():
            raise SynapseError(400, "money_source must be a string", Codes.INVALID_PARAM)
        plan_id = body.get("plan_id")
        if plan_id is not None and not isinstance(plan_id, str):
            raise SynapseError(400, "plan_id must be a string", Codes.INVALID_PARAM)

        response = await self.handler.process_yoomoney_payment(
            user_id,
            request_id=request_id.strip(),
            money_source=money_source.strip(),
            plan_id=plan_id.strip() if plan_id else None,
        )
        return 200, response


class SetkaPlusYooMoneyWebhookServlet(RestServlet):
    PATTERNS = [re.compile(r"^/_synapse/client/setka_plus/payments/yoomoney/webhook$")]
    CATEGORY = "Setka Plus"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.handler = hs.get_setka_plus_handler()

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        payload = self._read_payload(request)
        if not payload:
            raise SynapseError(400, "Webhook payload is empty", Codes.BAD_JSON)
        response = await self.handler.handle_yoomoney_webhook(payload)
        return 200, response

    def _read_payload(self, request: SynapseRequest) -> JsonDict:
        if request.args:
            form_payload: JsonDict = {}
            for key, values in request.args.items():
                if not values:
                    continue
                form_payload[key.decode("utf-8")] = values[-1].decode("utf-8")
            if form_payload:
                return form_payload

        raw = request.content.read()
        if not raw:
            return {}

        try:
            request.content.seek(0)
            json_payload = parse_json_object_from_request(request)
            if isinstance(json_payload, dict):
                normalized: JsonDict = {}
                for key, value in json_payload.items():
                    if isinstance(value, (str, int, float, bool)):
                        normalized[key] = str(value)
                return normalized
        except Exception:
            pass

        decoded = raw.decode("utf-8", errors="ignore")
        if not decoded:
            return {}
        parsed = parse_qs(decoded, keep_blank_values=True)
        payload: JsonDict = {}
        for key, values in parsed.items():
            if values:
                payload[key] = values[-1]
        return payload


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SetkaPlusSubscriptionServlet(hs).register(http_server)
    SetkaPlusPlansServlet(hs).register(http_server)
    SetkaPlusStickerPacksServlet(hs).register(http_server)
    SetkaPlusStickerPackServlet(hs).register(http_server)
    SetkaPlusPaymentsServlet(hs).register(http_server)
    SetkaPlusYooMoneyCreateServlet(hs).register(http_server)
    SetkaPlusYooMoneyProcessServlet(hs).register(http_server)
    SetkaPlusYooMoneyWebhookServlet(hs).register(http_server)
