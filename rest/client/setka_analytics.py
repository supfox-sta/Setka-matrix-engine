# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026 Setka Matrix

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.rest.client._base import client_patterns
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


def _platform_from_ua(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if "android" in ua:
        return "android"
    if "iphone" in ua or "ipad" in ua:
        return "ios"
    if "mozilla" in ua or "chrome" in ua or "safari" in ua:
        return "web"
    return "unknown"


class SetkaAnalyticsCaptureServlet(RestServlet):
    """Batch client event ingest for server-side BI (mirrors PostHog)."""

    PATTERNS = client_patterns("/setka/analytics/capture$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_analytics_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)

        platform = body.get("platform")
        if not isinstance(platform, str):
            platform = _platform_from_ua(
                request.getHeader(b"User-Agent").decode("utf-8", errors="replace")
                if request.getHeader(b"User-Agent")
                else None
            )

        app_version = body.get("app_version")
        if not isinstance(app_version, str):
            app_version = None

        events = body.get("events")
        if not isinstance(events, list):
            events = []

        # Single-event shorthand
        if not events and isinstance(body.get("event_name"), str):
            events = [{"event_name": body["event_name"], "properties": body.get("properties") or {}}]

        recorded = await self._handler.record_events_batch(
            user_id=requester.user.to_string(),
            device_id=requester.device_id,
            platform=platform,
            app_version=app_version,
            events=events,
        )

        # Deduped daily active signal
        await self._handler.record_event(
            event_name="session.active",
            user_id=requester.user.to_string(),
            platform=platform,
            app_version=app_version,
            device_id=requester.device_id,
            source="client",
        )

        return 200, {"recorded": recorded}


def register_servlets(hs: "HomeServer", http_server: object) -> None:
    SetkaAnalyticsCaptureServlet(hs).register(http_server)
