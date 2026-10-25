# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_integer, parse_string
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


def _decode_room_id(room_id: str) -> str:
    return unquote(room_id)


class SetkaFeedCategoriesServlet(RestServlet):
    PATTERNS = client_patterns("/setka/feed/categories$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        await self._auth.get_user_by_req(request)
        try:
            return 200, await self._handler.list_categories()
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(
                500,
                f"Setka feed categories failed: {type(e).__name__}: {e}",
                Codes.UNKNOWN,
            ) from e


class SetkaFeedServlet(RestServlet):
    PATTERNS = client_patterns("/setka/feed$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        mode = parse_string(request, "mode", default="recommended") or "recommended"
        limit = parse_integer(request, "limit", 20)
        since = parse_string(request, "since")
        query = parse_string(request, "q")
        try:
            return 200, await self._handler.get_feed(
                requester.user.to_string(),
                mode=mode,
                limit=limit or 20,
                since=since,
                search_query=query,
            )
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(
                500,
                f"Setka feed failed: {type(e).__name__}: {e}",
                Codes.UNKNOWN,
            ) from e


class SetkaFeedSearchServlet(RestServlet):
    PATTERNS = client_patterns("/setka/feed/search$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        query = parse_string(request, "q", required=True)
        limit = parse_integer(request, "limit", 20)
        since = parse_string(request, "since")
        try:
            return 200, await self._handler.search_posts(
                requester.user.to_string(),
                query=query,
                limit=limit or 20,
                since=since,
            )
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(
                500,
                f"Setka feed search failed: {type(e).__name__}: {e}",
                Codes.UNKNOWN,
            ) from e


class SetkaFeedPostLikeServlet(RestServlet):
    PATTERNS = client_patterns("/setka/feed/posts/(?P<event_id>[^/]*)/like$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_POST(
        self, request: "SynapseRequest", event_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.toggle_like(
            requester.user.to_string(),
            unquote(event_id),
        )


class SetkaFeedPostViewServlet(RestServlet):
    PATTERNS = client_patterns("/setka/feed/posts/(?P<event_id>[^/]*)/view$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_POST(
        self, request: "SynapseRequest", event_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.record_view(
            requester.user.to_string(),
            unquote(event_id),
        )


class SetkaChannelSubscribeServlet(RestServlet):
    PATTERNS = client_patterns("/setka/channels/(?P<room_id>[^/]*)/subscribe$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_POST(
        self, request: "SynapseRequest", room_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.subscribe(
            requester.user.to_string(),
            _decode_room_id(room_id),
        )

    async def on_DELETE(
        self, request: "SynapseRequest", room_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.unsubscribe(
            requester.user.to_string(),
            _decode_room_id(room_id),
        )


class SetkaChannelSubscriptionServlet(RestServlet):
    PATTERNS = client_patterns("/setka/channels/(?P<room_id>[^/]*)/subscription$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_feed_handler()

    async def on_GET(
        self, request: "SynapseRequest", room_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.get_subscription(
            requester.user.to_string(),
            _decode_room_id(room_id),
        )


def register_servlets(hs: "HomeServer", http_server) -> None:
    hs.get_setka_feed_handler()
    SetkaFeedCategoriesServlet(hs).register(http_server)
    SetkaFeedServlet(hs).register(http_server)
    SetkaFeedSearchServlet(hs).register(http_server)
    SetkaFeedPostLikeServlet(hs).register(http_server)
    SetkaFeedPostViewServlet(hs).register(http_server)
    SetkaChannelSubscribeServlet(hs).register(http_server)
    SetkaChannelSubscriptionServlet(hs).register(http_server)
