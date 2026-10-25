# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


def _client_ip(request: "SynapseRequest") -> str | None:
    forwarded = _request_header(request, b"X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip

    real_ip = _request_header(request, b"X-Real-IP")
    if real_ip:
        stripped = real_ip.strip()
        if stripped:
            return stripped

    try:
        ip = request.getClientIP()
    except Exception:
        return None
    if ip is None:
        return None
    if isinstance(ip, bytes):
        return ip.decode("utf-8", errors="replace")
    return str(ip)


def _request_header(request: "SynapseRequest", header: bytes) -> str | None:
    raw = request.getHeader(header)
    if not raw:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


class SetkaLoginStatusServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/status$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        status = await self._handler.get_status(
            requester.user.to_string(),
            requester.device_id or "",
            ip=_client_ip(request),
            user_agent=_request_header(request, b"User-Agent"),
        )
        return 200, status

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        return await self.on_GET(request)


class SetkaLoginVerifyServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/verify$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        code = body.get("code")
        if not isinstance(code, str):
            raise SynapseError(400, "Missing code", Codes.MISSING_PARAM)
        result = await self._handler.verify_code(
            requester.user.to_string(),
            requester.device_id or "",
            code,
        )
        return 200, result


class SetkaLoginSendEmailServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/send_email$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request, allow_empty_body=True)
        email = body.get("email")
        if email is not None and not isinstance(email, str):
            raise SynapseError(400, "email must be a string", Codes.INVALID_PARAM)
        result = await self._handler.send_code_via_email(
            requester.user.to_string(),
            requester.device_id or "",
            email=email.strip() if isinstance(email, str) and email.strip() else None,
            ip=_client_ip(request),
            user_agent=_request_header(request, b"User-Agent"),
        )
        return 200, result


class SetkaLoginCompleteFallbackServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/complete_fallback$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        method = body.get("method")
        if not isinstance(method, str):
            raise SynapseError(400, "Missing method", Codes.MISSING_PARAM)
        result = await self._handler.complete_fallback(
            requester.user.to_string(),
            requester.device_id or "",
            method,
            recovery_key=body.get("recovery_key") if isinstance(body.get("recovery_key"), str) else None,
        )
        return 200, result


class SetkaLoginRecoveryStoreServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/recovery/store$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        recovery_key = body.get("recovery_key")
        if not isinstance(recovery_key, str):
            raise SynapseError(400, "Missing recovery_key", Codes.MISSING_PARAM)
        result = await self._handler.store_recovery_key(
            requester.user.to_string(),
            requester.device_id or "",
            recovery_key,
        )
        return 200, result


class SetkaLoginRecoveryFetchServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/recovery/fetch$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        result = await self._handler.fetch_recovery_key(
            requester.user.to_string(),
            requester.device_id or "",
        )
        return 200, result


class SetkaLoginRecoveryDeleteServlet(RestServlet):
    PATTERNS = client_patterns("/setka/login/recovery/delete$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_login_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        result = await self._handler.delete_recovery_key(
            requester.user.to_string(),
            requester.device_id or "",
        )
        return 200, result


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaLoginStatusServlet(hs).register(http_server)
    SetkaLoginVerifyServlet(hs).register(http_server)
    SetkaLoginSendEmailServlet(hs).register(http_server)
    SetkaLoginCompleteFallbackServlet(hs).register(http_server)
    SetkaLoginRecoveryStoreServlet(hs).register(http_server)
    SetkaLoginRecoveryFetchServlet(hs).register(http_server)
    SetkaLoginRecoveryDeleteServlet(hs).register(http_server)
