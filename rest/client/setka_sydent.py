# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


class SetkaSydentSyncServlet(RestServlet):
    """Sync the current user's local 3PID bindings to the co-located Sydent instance."""

    PATTERNS = client_patterns("/setka/sydent/sync$")
    CATEGORY = "Setka Sydent"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_sydent_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        if not self._handler.is_enabled():
            raise SynapseError(
                503,
                "Sydent integration is not configured on this homeserver.",
                Codes.UNKNOWN,
            )
        synced = await self._handler.sync_user_threepids(requester.user.to_string())
        return 200, {"synced": synced}


class SetkaSydentStatusServlet(RestServlet):
    """Return whether Sydent bridge is configured (for client diagnostics)."""

    PATTERNS = client_patterns("/setka/sydent/status$")
    CATEGORY = "Setka Sydent"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_sydent_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        await self._auth.get_user_by_req(request)
        return 200, {
            "enabled": self._handler.is_enabled(),
            "lookup_configured": self._handler.is_lookup_configured(),
            "bind_configured": self._handler.is_bind_configured(),
            "auto_sync_on_bind": self._handler.is_bind_configured(),
        }


def register_servlets(hs: "HomeServer", http_server) -> None:
    SetkaSydentSyncServlet(hs).register(http_server)
    SetkaSydentStatusServlet(hs).register(http_server)
