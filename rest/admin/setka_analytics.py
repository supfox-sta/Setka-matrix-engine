# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026 Setka Matrix

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.http.servlet import RestServlet, parse_integer
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


class SetkaAnalyticsOverviewServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/analytics/overview$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_analytics_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)
        days = parse_integer(request, "days", default=30)
        return 200, await self._handler.get_overview(days=days)


class SetkaAnalyticsRetentionServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/analytics/retention$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_analytics_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)
        days = parse_integer(request, "days", default=30)
        return 200, await self._handler.get_retention(cohort_days=days)


class SetkaAnalyticsOnlineServlet(RestServlet):
    PATTERNS = admin_patterns("/setka/analytics/online$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_analytics_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)
        return 200, await self._handler.list_online_users()


def register_servlets(hs: "HomeServer", http_server: object) -> None:
    SetkaAnalyticsOverviewServlet(hs).register(http_server)
    SetkaAnalyticsRetentionServlet(hs).register(http_server)
    SetkaAnalyticsOnlineServlet(hs).register(http_server)
