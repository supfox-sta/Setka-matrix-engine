# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

from __future__ import annotations

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import RestServlet, parse_integer, parse_json_object_from_request
from synapse.rest.client._base import client_patterns

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer
    from synapse.types import JsonDict


class SetkaBotsListServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_GET(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.list_bots(requester.user.to_string())


class SetkaBotsCreateServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        username = body.get("username")
        display_name = body.get("display_name", "")
        if not isinstance(username, str) or not username.strip():
            raise SynapseError(400, "username is required", Codes.INVALID_PARAM)
        if not isinstance(display_name, str):
            raise SynapseError(400, "display_name must be a string", Codes.INVALID_PARAM)
        return 200, await self._handler.create_bot(
            requester.user.to_string(),
            username=username,
            display_name=display_name,
        )


class SetkaBotDeleteServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots/(?P<bot_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_PUT(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        display_name = body.get("display_name")
        webhook_url = body.get("webhook_url")
        if display_name is not None and not isinstance(display_name, str):
            raise SynapseError(400, "display_name must be a string", Codes.INVALID_PARAM)
        if "webhook_url" in body and webhook_url is not None and not isinstance(webhook_url, str):
            raise SynapseError(400, "webhook_url must be a string or null", Codes.INVALID_PARAM)
        return 200, await self._handler.update_bot(
            requester.user.to_string(),
            bot_id,
            display_name=display_name,
            webhook_url=webhook_url,
            unset_webhook=webhook_url is None and "webhook_url" in body,
        )

    async def on_DELETE(
        self, request: "SynapseRequest", bot_id: str
    ) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.delete_bot(requester.user.to_string(), bot_id)


class SetkaBotRegenerateTokenServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots/(?P<bot_id>[^/]*)/regenerate_token$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_POST(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.regenerate_token(requester.user.to_string(), bot_id)


class SetkaBotCommandsServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots/(?P<bot_id>[^/]*)/commands$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_GET(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.get_commands(requester.user.to_string(), bot_id)

    async def on_PUT(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        commands = body.get("commands", [])
        if not isinstance(commands, list):
            raise SynapseError(400, "commands must be a list", Codes.INVALID_PARAM)
        return 200, await self._handler.set_commands(
            requester.user.to_string(),
            bot_id,
            commands,
        )


class SetkaBotReplyMarkupServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots/(?P<bot_id>[^/]*)/reply_markup$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_GET(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        return 200, await self._handler.get_reply_markup(requester.user.to_string(), bot_id)

    async def on_PUT(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        reply_markup = body.get("reply_markup")
        if reply_markup is not None and not isinstance(reply_markup, dict):
            raise SynapseError(400, "reply_markup must be an object or null", Codes.INVALID_PARAM)
        return 200, await self._handler.set_reply_markup(
            requester.user.to_string(),
            bot_id,
            reply_markup,
        )


class SetkaBotCallbackServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bot/callback$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        bot_user_id = body.get("bot_user_id")
        event_id = body.get("event_id")
        callback_data = body.get("callback_data")
        if not isinstance(bot_user_id, str) or not bot_user_id.strip():
            raise SynapseError(400, "bot_user_id is required", Codes.INVALID_PARAM)
        if not isinstance(event_id, str) or not event_id.strip():
            raise SynapseError(400, "event_id is required", Codes.INVALID_PARAM)
        if not isinstance(callback_data, str):
            raise SynapseError(400, "callback_data is required", Codes.INVALID_PARAM)
        return 200, await self._handler.submit_callback_query(
            requester.user.to_string(),
            bot_user_id=bot_user_id.strip(),
            event_id=event_id.strip(),
            callback_data=callback_data,
        )


class SetkaBotCreatorDispatchServlet(RestServlet):
    PATTERNS = client_patterns("/setka/botcreator/dispatch$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._handler = hs.get_setka_bots_handler()

    async def on_POST(self, request: "SynapseRequest") -> tuple[int, "JsonDict"]:
        auth_header = request.getHeader(b"Authorization")
        service_token = None
        if auth_header and auth_header.startswith(b"Bearer "):
            service_token = auth_header[7:].decode("utf-8")
        body = parse_json_object_from_request(request)
        owner_user_id = body.get("owner_user_id")
        action = body.get("action")
        params = body.get("params", {})
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise SynapseError(400, "owner_user_id is required", Codes.INVALID_PARAM)
        if not isinstance(action, str) or not action.strip():
            raise SynapseError(400, "action is required", Codes.INVALID_PARAM)
        if not isinstance(params, dict):
            raise SynapseError(400, "params must be an object", Codes.INVALID_PARAM)
        return 200, await self._handler.dispatch_botcreator_action(
            service_token,
            owner_user_id=owner_user_id.strip(),
            action=action,
            params=params,
        )


class SetkaBotWebhookServlet(RestServlet):
    PATTERNS = client_patterns("/setka/bots/(?P<bot_id>[^/]*)/webhook$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._auth = hs.get_auth()
        self._handler = hs.get_setka_bots_handler()

    async def on_PUT(self, request: "SynapseRequest", bot_id: str) -> tuple[int, "JsonDict"]:
        requester = await self._auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        url = body.get("url")
        if url is not None and not isinstance(url, str):
            raise SynapseError(400, "url must be a string or null", Codes.INVALID_PARAM)
        return 200, await self._handler.set_webhook(
            requester.user.to_string(),
            bot_id,
            url,
        )


class SetkaBotApiServlet(RestServlet):
    """Telegram-style bot API. Auth = token in URL (no Matrix access token)."""

    PATTERNS = client_patterns("/setka/bot/api/(?P<token>[^/]+)/(?P<method>[a-zA-Z]+)$")
    CATEGORY = "Setka Bots"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._handler = hs.get_setka_bots_handler()

    async def on_GET(
        self, request: "SynapseRequest", token: str, method: str
    ) -> tuple[int, "JsonDict"]:
        method = method.lower()
        if method == "getme":
            return 200, await self._handler.get_me(token)
        if method == "getupdates":
            offset = parse_integer(request, "offset", default=0) or 0
            limit = parse_integer(request, "limit", default=100) or 100
            return 200, await self._handler.get_updates(token, offset=offset, limit=limit)
        raise SynapseError(404, f"Unknown method: {method}", Codes.NOT_FOUND)

    async def on_POST(
        self, request: "SynapseRequest", token: str, method: str
    ) -> tuple[int, "JsonDict"]:
        method = method.lower()
        body = parse_json_object_from_request(request)

        if method == "sendmessage":
            chat_id = body.get("chat_id")
            text = body.get("text")
            if not isinstance(chat_id, str) or not chat_id.strip():
                raise SynapseError(400, "chat_id is required", Codes.INVALID_PARAM)
            if not isinstance(text, str):
                raise SynapseError(400, "text is required", Codes.INVALID_PARAM)
            reply_markup = body.get("reply_markup")
            if reply_markup is not None and not isinstance(reply_markup, dict):
                raise SynapseError(400, "reply_markup must be an object", Codes.INVALID_PARAM)
            return 200, await self._handler.send_message(
                token,
                chat_id=chat_id.strip(),
                text=text,
                reply_markup=reply_markup,
            )

        if method == "setwebhook":
            url = body.get("url")
            if url is not None and not isinstance(url, str):
                raise SynapseError(400, "url must be a string or null", Codes.INVALID_PARAM)
            return 200, await self._handler.set_webhook_by_token(token, url)

        if method == "deletewebhook":
            return 200, await self._handler.set_webhook_by_token(token, None)

        if method == "acceptinvites":
            return 200, await self._handler.accept_pending_invites_by_token(token)

        raise SynapseError(404, f"Unknown method: {method}", Codes.NOT_FOUND)


def register_servlets(hs: "HomeServer", http_server) -> None:
    hs.get_setka_bots_handler()
    SetkaBotsListServlet(hs).register(http_server)
    SetkaBotsCreateServlet(hs).register(http_server)
    SetkaBotDeleteServlet(hs).register(http_server)
    SetkaBotRegenerateTokenServlet(hs).register(http_server)
    SetkaBotCommandsServlet(hs).register(http_server)
    SetkaBotReplyMarkupServlet(hs).register(http_server)
    SetkaBotCallbackServlet(hs).register(http_server)
    SetkaBotCreatorDispatchServlet(hs).register(http_server)
    SetkaBotWebhookServlet(hs).register(http_server)
    SetkaBotApiServlet(hs).register(http_server)
