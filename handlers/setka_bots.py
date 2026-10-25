# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka Bot API — Telegram-style bots on Matrix."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from typing import TYPE_CHECKING, Any

from synapse.api.constants import EventTypes, Membership, UserTypes
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict, UserID, create_requester
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

BOT_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,30}[a-z0-9]$")
BOT_TOKEN_PREFIX = "setka_"
SETKA_BOT_BUTTONS_KEY = "io.setka.bot.reply_markup"
MAX_UPDATES_PER_POLL = 100
WEBHOOK_TIMEOUT_SEC = 10


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return f"{BOT_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _bot_localpart(username: str) -> str:
    base = username.lower().removesuffix("_bot")
    return f"{base}_bot"


class SetkaBotsHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._registration_handler = hs.get_registration_handler()
        self._room_member_handler = hs.get_room_member_handler()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._http_client = hs.get_proxied_http_client()
        self._server_name = hs.hostname
        self._tables_ready = False
        self._bot_user_ids: set[str] = set()
        self._botcreator_service_token = os.environ.get("SETKA_BOTCREATOR_SERVICE_TOKEN", "")

        hs.get_module_api().register_third_party_rules_callbacks(
            on_new_event=self._on_new_event,
        )
        self._clock.looping_call_now(
            lambda: run_as_background_process(
                "setka_bot_reconcile_invites",
                self._hs.hostname,
                self._reconcile_pending_invites,
            ),
            Duration(seconds=30),
        )
        logger.info("Setka bots: on_new_event hook and invite reconcile registered")

    async def _ensure_tables(self) -> None:
        if self._tables_ready:
            return

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_bots (
                    bot_id TEXT NOT NULL PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    bot_user_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    webhook_url TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
                """
            )
            txn.execute(
                """
                CREATE INDEX IF NOT EXISTS setka_bots_owner_idx
                ON setka_bots (owner_user_id)
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_bot_updates (
                    bot_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    PRIMARY KEY (bot_id, update_id)
                )
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_bot_update_offsets (
                    bot_id TEXT NOT NULL PRIMARY KEY,
                    next_update_id INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_bot_meta (
                    bot_id TEXT NOT NULL PRIMARY KEY,
                    commands_json TEXT,
                    default_reply_markup_json TEXT
                )
                """
            )

        await self._store.db_pool.runInteraction("setka_bots_create_tables", _txn)
        await self._refresh_bot_user_cache()
        self._tables_ready = True

    async def _refresh_bot_user_cache(self) -> None:
        def _txn(txn: Any) -> set[str]:
            txn.execute(
                "SELECT bot_user_id FROM setka_bots WHERE is_active = 1",
            )
            return {str(row[0]) for row in txn.fetchall()}

        self._bot_user_ids = await self._store.db_pool.runInteraction(
            "setka_bots_refresh_cache",
            _txn,
        )

    async def list_bots(self, owner_user_id: str) -> JsonDict:
        await self._ensure_tables()

        def _txn(txn: Any) -> list[JsonDict]:
            txn.execute(
                """
                SELECT bot_id, bot_user_id, username, display_name, webhook_url, created_at
                FROM setka_bots
                WHERE owner_user_id = ? AND is_active = 1
                ORDER BY created_at DESC
                """,
                (owner_user_id,),
            )
            rows = []
            for bot_id, bot_user_id, username, display_name, webhook_url, created_at in txn.fetchall():
                rows.append(
                    {
                        "bot_id": bot_id,
                        "bot_user_id": bot_user_id,
                        "username": username,
                        "display_name": display_name,
                        "webhook_url": webhook_url,
                        "created_at": int(created_at),
                    }
                )
            return rows

        bots = await self._store.db_pool.runInteraction("setka_bots_list", _txn)
        return {"bots": bots}

    async def create_bot(
        self,
        owner_user_id: str,
        *,
        username: str,
        display_name: str,
    ) -> JsonDict:
        await self._ensure_tables()

        username = username.strip().lower()
        if not BOT_USERNAME_RE.match(username):
            raise SynapseError(
                400,
                "username must be 3-32 chars: a-z, 0-9, underscore",
                Codes.INVALID_PARAM,
            )

        localpart = _bot_localpart(username)
        bot_user_id = f"@{localpart}:{self._server_name}"

        def _exists(txn: Any) -> bool:
            txn.execute("SELECT 1 FROM setka_bots WHERE username = ?", (username,))
            if txn.fetchone():
                return True
            txn.execute("SELECT 1 FROM users WHERE name = ?", (bot_user_id,))
            return txn.fetchone() is not None

        if await self._store.db_pool.runInteraction("setka_bots_exists", _exists):
            raise SynapseError(409, "Bot username already taken", Codes.USER_IN_USE)

        await self._registration_handler.register_user(
            localpart=localpart,
            password_hash=None,
            user_type=UserTypes.BOT,
            default_display_name=display_name.strip() or username,
            by_admin=True,
            approved=True,
        )

        token = _new_token()
        bot_id = str(uuid.uuid4())
        now = self._clock.time_msec()

        def _insert(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO setka_bots (
                    bot_id, owner_user_id, bot_user_id, username, display_name,
                    token_hash, webhook_url, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                """,
                (
                    bot_id,
                    owner_user_id,
                    bot_user_id,
                    username,
                    display_name.strip() or username,
                    _hash_token(token),
                    now,
                    now,
                ),
            )
            txn.execute(
                "INSERT INTO setka_bot_update_offsets (bot_id, next_update_id) VALUES (?, 1)",
                (bot_id,),
            )

        await self._store.db_pool.runInteraction("setka_bots_insert", _insert)
        await self._refresh_bot_user_cache()

        return {
            "ok": True,
            "bot": {
                "bot_id": bot_id,
                "bot_user_id": bot_user_id,
                "username": username,
                "display_name": display_name.strip() or username,
            },
            "token": token,
        }

    async def update_bot(
        self,
        owner_user_id: str,
        bot_id: str,
        *,
        display_name: str | None = None,
        webhook_url: str | None = None,
        unset_webhook: bool = False,
    ) -> JsonDict:
        await self._ensure_tables()
        now = self._clock.time_msec()
        if webhook_url is not None and webhook_url and not webhook_url.startswith("https://"):
            raise SynapseError(400, "webhook url must use https", Codes.INVALID_PARAM)

        def _txn(txn: Any) -> JsonDict | None:
            txn.execute(
                """
                SELECT bot_user_id, username, display_name, webhook_url, created_at
                FROM setka_bots
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (bot_id, owner_user_id),
            )
            row = txn.fetchone()
            if not row:
                return None
            next_display_name = display_name.strip() if display_name else str(row[2])
            next_webhook = row[3]
            if unset_webhook:
                next_webhook = None
            elif webhook_url is not None:
                next_webhook = webhook_url or None
            txn.execute(
                """
                UPDATE setka_bots
                SET display_name = ?, webhook_url = ?, updated_at = ?
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (next_display_name, next_webhook, now, bot_id, owner_user_id),
            )
            return {
                "bot_id": bot_id,
                "bot_user_id": row[0],
                "username": row[1],
                "display_name": next_display_name,
                "webhook_url": next_webhook,
                "created_at": int(row[4]),
            }

        bot = await self._store.db_pool.runInteraction("setka_bots_update", _txn)
        if not bot:
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)
        return {"ok": True, "bot": bot}

    async def regenerate_token(self, owner_user_id: str, bot_id: str) -> JsonDict:
        await self._ensure_tables()
        token = _new_token()
        now = self._clock.time_msec()

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                UPDATE setka_bots
                SET token_hash = ?, updated_at = ?
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (_hash_token(token), now, bot_id, owner_user_id),
            )
            return txn.rowcount > 0

        if not await self._store.db_pool.runInteraction("setka_bots_regen_token", _txn):
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)
        return {"ok": True, "token": token}

    async def get_commands(self, owner_user_id: str, bot_id: str) -> JsonDict:
        await self._ensure_tables()
        await self._assert_bot_owner(owner_user_id, bot_id)

        def _txn(txn: Any) -> list[JsonDict]:
            txn.execute(
                "SELECT commands_json FROM setka_bot_meta WHERE bot_id = ?",
                (bot_id,),
            )
            row = txn.fetchone()
            if not row or not row[0]:
                return []
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                return []
            return data if isinstance(data, list) else []

        commands = await self._store.db_pool.runInteraction("setka_bots_get_commands", _txn)
        return {"ok": True, "commands": commands}

    async def set_commands(
        self,
        owner_user_id: str,
        bot_id: str,
        commands: list[JsonDict],
    ) -> JsonDict:
        await self._ensure_tables()
        await self._assert_bot_owner(owner_user_id, bot_id)
        cleaned: list[JsonDict] = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command", "")).strip().lstrip("/")
            description = str(item.get("description", "")).strip()
            if not command:
                continue
            cleaned.append({"command": command, "description": description})

        payload = json.dumps(cleaned, separators=(",", ":"))

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO setka_bot_meta (bot_id, commands_json, default_reply_markup_json)
                VALUES (?, ?, NULL)
                ON CONFLICT(bot_id) DO UPDATE SET commands_json = excluded.commands_json
                """,
                (bot_id, payload),
            )

        await self._store.db_pool.runInteraction("setka_bots_set_commands", _txn)
        return {"ok": True, "commands": cleaned}

    async def get_reply_markup(self, owner_user_id: str, bot_id: str) -> JsonDict:
        await self._ensure_tables()
        await self._assert_bot_owner(owner_user_id, bot_id)

        def _txn(txn: Any) -> JsonDict | None:
            txn.execute(
                "SELECT default_reply_markup_json FROM setka_bot_meta WHERE bot_id = ?",
                (bot_id,),
            )
            row = txn.fetchone()
            if not row or not row[0]:
                return None
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None

        markup = await self._store.db_pool.runInteraction("setka_bots_get_markup", _txn)
        return {"ok": True, "reply_markup": markup}

    async def set_reply_markup(
        self,
        owner_user_id: str,
        bot_id: str,
        reply_markup: JsonDict | None,
    ) -> JsonDict:
        await self._ensure_tables()
        await self._assert_bot_owner(owner_user_id, bot_id)
        payload = json.dumps(reply_markup, separators=(",", ":")) if reply_markup else None

        def _txn(txn: Any) -> None:
            txn.execute(
                """
                INSERT INTO setka_bot_meta (bot_id, commands_json, default_reply_markup_json)
                VALUES (?, NULL, ?)
                ON CONFLICT(bot_id) DO UPDATE SET default_reply_markup_json = excluded.default_reply_markup_json
                """,
                (bot_id, payload),
            )

        await self._store.db_pool.runInteraction("setka_bots_set_markup", _txn)
        return {"ok": True, "reply_markup": reply_markup}

    async def submit_callback_query(
        self,
        user_id: str,
        *,
        bot_user_id: str,
        event_id: str,
        callback_data: str,
    ) -> JsonDict:
        await self._ensure_tables()
        event = await self._store.get_event(event_id)
        users = await self._store.get_users_in_room(event.room_id)
        if user_id not in users:
            raise SynapseError(403, "Not in room", Codes.FORBIDDEN)

        bot_id = await self._bot_id_for_user(bot_user_id)
        if not bot_id:
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)

        update = {
            "callback_query": {
                "id": str(uuid.uuid4()),
                "from": {"user_id": user_id},
                "chat_id": event.room_id,
                "message_id": event_id,
                "data": callback_data,
            }
        }
        update_id = await self._enqueue_update(bot_id, update)
        return {"ok": True, "update_id": update_id}

    def _check_botcreator_service_token(self, token: str | None) -> None:
        if not self._botcreator_service_token:
            raise SynapseError(503, "BotCreator service is not configured", Codes.UNAVAILABLE)
        if not token or token != self._botcreator_service_token:
            raise SynapseError(401, "Invalid BotCreator service token", Codes.UNAUTHORIZED)

    async def dispatch_botcreator_action(
        self,
        service_token: str | None,
        *,
        owner_user_id: str,
        action: str,
        params: JsonDict,
    ) -> JsonDict:
        self._check_botcreator_service_token(service_token)
        action = action.lower().strip()
        if action == "list_bots":
            return await self.list_bots(owner_user_id)
        if action == "create_bot":
            return await self.create_bot(
                owner_user_id,
                username=str(params.get("username", "")),
                display_name=str(params.get("display_name", "")),
            )
        if action == "delete_bot":
            return await self.delete_bot(owner_user_id, str(params.get("bot_id", "")))
        if action == "regenerate_token":
            return await self.regenerate_token(owner_user_id, str(params.get("bot_id", "")))
        if action == "set_webhook":
            return await self.set_webhook(
                owner_user_id,
                str(params.get("bot_id", "")),
                params.get("url"),
            )
        if action == "set_commands":
            commands = params.get("commands", [])
            if not isinstance(commands, list):
                raise SynapseError(400, "commands must be a list", Codes.INVALID_PARAM)
            return await self.set_commands(owner_user_id, str(params.get("bot_id", "")), commands)
        if action == "get_commands":
            return await self.get_commands(owner_user_id, str(params.get("bot_id", "")))
        if action == "set_reply_markup":
            markup = params.get("reply_markup")
            if markup is not None and not isinstance(markup, dict):
                raise SynapseError(400, "reply_markup must be an object", Codes.INVALID_PARAM)
            return await self.set_reply_markup(
                owner_user_id,
                str(params.get("bot_id", "")),
                markup,
            )
        raise SynapseError(400, f"Unknown action: {action}", Codes.INVALID_PARAM)

    async def _assert_bot_owner(self, owner_user_id: str, bot_id: str) -> None:
        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                SELECT 1 FROM setka_bots
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (bot_id, owner_user_id),
            )
            return txn.fetchone() is not None

        if not await self._store.db_pool.runInteraction("setka_bots_assert_owner", _txn):
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)

    async def delete_bot(self, owner_user_id: str, bot_id: str) -> JsonDict:
        await self._ensure_tables()
        now = self._clock.time_msec()

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                UPDATE setka_bots
                SET is_active = 0, updated_at = ?, webhook_url = NULL
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (now, bot_id, owner_user_id),
            )
            return txn.rowcount > 0

        if not await self._store.db_pool.runInteraction("setka_bots_deactivate", _txn):
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)

        await self._refresh_bot_user_cache()
        return {"ok": True}

    async def set_webhook(self, owner_user_id: str, bot_id: str, url: str | None) -> JsonDict:
        await self._ensure_tables()
        now = self._clock.time_msec()
        if url is not None and not url.startswith("https://"):
            raise SynapseError(400, "webhook url must use https", Codes.INVALID_PARAM)

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                UPDATE setka_bots
                SET webhook_url = ?, updated_at = ?
                WHERE bot_id = ? AND owner_user_id = ? AND is_active = 1
                """,
                (url, now, bot_id, owner_user_id),
            )
            return txn.rowcount > 0

        if not await self._store.db_pool.runInteraction("setka_bots_webhook", _txn):
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)

        return {"ok": True, "webhook_url": url}

    async def _get_bot_by_token(self, token: str) -> JsonDict:
        await self._ensure_tables()
        if not token.startswith(BOT_TOKEN_PREFIX):
            raise SynapseError(401, "Invalid bot token", Codes.UNAUTHORIZED)

        token_hash = _hash_token(token)

        def _txn(txn: Any) -> JsonDict | None:
            txn.execute(
                """
                SELECT bot_id, bot_user_id, username, display_name, webhook_url
                FROM setka_bots
                WHERE token_hash = ? AND is_active = 1
                """,
                (token_hash,),
            )
            row = txn.fetchone()
            if not row:
                return None
            return {
                "bot_id": row[0],
                "bot_user_id": row[1],
                "username": row[2],
                "display_name": row[3],
                "webhook_url": row[4],
            }

        bot = await self._store.db_pool.runInteraction("setka_bots_by_token", _txn)
        if not bot:
            raise SynapseError(401, "Invalid bot token", Codes.UNAUTHORIZED)
        return bot

    async def get_me(self, token: str) -> JsonDict:
        bot = await self._get_bot_by_token(token)
        return {
            "ok": True,
            "result": {
                "id": bot["bot_id"],
                "username": bot["username"],
                "display_name": bot["display_name"],
                "bot_user_id": bot["bot_user_id"],
                "is_bot": True,
            },
        }

    async def accept_pending_invites_by_token(self, token: str) -> JsonDict:
        bot = await self._get_bot_by_token(token)
        result = await self.accept_pending_invites_for_user(bot["bot_user_id"])
        return {"ok": True, "result": result}

    async def accept_pending_invites_for_user(self, bot_user_id: str) -> JsonDict:
        await self._ensure_tables()
        if bot_user_id not in self._bot_user_ids:
            await self._refresh_bot_user_cache()
        if bot_user_id not in self._bot_user_ids:
            raise SynapseError(404, "Unknown Setka bot user", Codes.NOT_FOUND)

        invited = await self._store.get_invited_rooms_for_local_user(bot_user_id)
        joined: list[str] = []
        failed: list[str] = []
        for invite in invited:
            if await self._join_bot_to_room(bot_user_id, invite.room_id):
                joined.append(invite.room_id)
            else:
                failed.append(invite.room_id)

        if joined:
            logger.info(
                "Setka bot %s accepted %s pending invite(s): %s",
                bot_user_id,
                len(joined),
                joined,
            )
        return {"joined": joined, "failed": failed}

    async def send_message(
        self,
        token: str,
        *,
        chat_id: str,
        text: str,
        reply_markup: JsonDict | None = None,
    ) -> JsonDict:
        bot = await self._get_bot_by_token(token)
        room_id = chat_id
        body = text.strip()
        if not body:
            raise SynapseError(400, "text is required", Codes.INVALID_PARAM)

        users = await self._store.get_users_in_room(room_id)
        if bot["bot_user_id"] not in users:
            raise SynapseError(403, "Bot is not a member of this room", Codes.FORBIDDEN)

        content: JsonDict = {"msgtype": "m.text", "body": body}
        if reply_markup:
            content[SETKA_BOT_BUTTONS_KEY] = reply_markup

        requester = create_requester(bot["bot_user_id"])
        event_dict = {
            "type": EventTypes.Message,
            "room_id": room_id,
            "sender": bot["bot_user_id"],
            "content": content,
        }
        event, _ = await self._event_creation_handler.create_and_send_nonmember_event(
            requester,
            event_dict,
            ratelimit=False,
        )
        return {
            "ok": True,
            "result": {
                "message_id": event.event_id,
                "chat_id": room_id,
                "date": int(event.origin_server_ts),
            },
        }

    async def get_updates(
        self,
        token: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> JsonDict:
        bot = await self._get_bot_by_token(token)
        limit = min(max(limit, 1), MAX_UPDATES_PER_POLL)

        def _txn(txn: Any) -> list[JsonDict]:
            txn.execute(
                """
                SELECT update_id, payload
                FROM setka_bot_updates
                WHERE bot_id = ? AND update_id > ?
                ORDER BY update_id ASC
                LIMIT ?
                """,
                (bot["bot_id"], offset, limit),
            )
            return [
                {"update_id": int(row[0]), "update": json.loads(row[1])}
                for row in txn.fetchall()
            ]

        updates = await self._store.db_pool.runInteraction("setka_bots_poll", _txn)
        return {"ok": True, "result": updates}

    async def set_webhook_by_token(self, token: str, url: str | None) -> JsonDict:
        bot = await self._get_bot_by_token(token)
        now = self._clock.time_msec()
        if url is not None and not url.startswith("https://"):
            raise SynapseError(400, "webhook url must use https", Codes.INVALID_PARAM)

        def _txn(txn: Any) -> bool:
            txn.execute(
                """
                UPDATE setka_bots
                SET webhook_url = ?, updated_at = ?
                WHERE bot_id = ? AND is_active = 1
                """,
                (url, now, bot["bot_id"]),
            )
            return txn.rowcount > 0

        if not await self._store.db_pool.runInteraction("setka_bots_webhook_token", _txn):
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)

        return {"ok": True, "webhook_url": url}

    async def _get_owner_for_bot(self, bot_id: str) -> str:
        def _txn(txn: Any) -> str | None:
            txn.execute(
                "SELECT owner_user_id FROM setka_bots WHERE bot_id = ? AND is_active = 1",
                (bot_id,),
            )
            row = txn.fetchone()
            return str(row[0]) if row else None

        owner = await self._store.db_pool.runInteraction("setka_bots_owner", _txn)
        if not owner:
            raise SynapseError(404, "Bot not found", Codes.NOT_FOUND)
        return owner

    async def _enqueue_update(self, bot_id: str, update: JsonDict) -> int:
        now = self._clock.time_msec()
        payload = json.dumps(update, separators=(",", ":"))

        def _txn(txn: Any) -> tuple[int, str | None]:
            txn.execute(
                "SELECT next_update_id FROM setka_bot_update_offsets WHERE bot_id = ?",
                (bot_id,),
            )
            row = txn.fetchone()
            if not row:
                return 0, None
            update_id = int(row[0])
            txn.execute(
                """
                INSERT INTO setka_bot_updates (bot_id, update_id, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (bot_id, update_id, payload, now),
            )
            txn.execute(
                "UPDATE setka_bot_update_offsets SET next_update_id = ? WHERE bot_id = ?",
                (update_id + 1, bot_id),
            )
            txn.execute(
                "SELECT webhook_url FROM setka_bots WHERE bot_id = ? AND is_active = 1",
                (bot_id,),
            )
            webhook_row = txn.fetchone()
            webhook_url = str(webhook_row[0]) if webhook_row and webhook_row[0] else None
            return update_id, webhook_url

        update_id, webhook_url = await self._store.db_pool.runInteraction(
            "setka_bots_enqueue",
            _txn,
        )
        if update_id:
            update["update_id"] = update_id
            if webhook_url:
                await self._deliver_webhook(
                    webhook_url,
                    {"update_id": update_id, "update": update},
                )
        return update_id

    async def _deliver_webhook(self, url: str, body: JsonDict) -> None:
        try:
            await self._http_client.request(
                "POST",
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={b"Content-Type": b"application/json"},
                timeout=WEBHOOK_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.warning("Setka bot webhook delivery failed for %s: %s", url, e)

    async def _send_bot_room_message(
        self,
        bot_user_id: str,
        room_id: str,
        text: str,
        reply_markup: JsonDict | None = None,
    ) -> None:
        body = text.strip()
        if not body:
            return

        content: JsonDict = {"msgtype": "m.text", "body": body}
        if reply_markup:
            content[SETKA_BOT_BUTTONS_KEY] = reply_markup

        requester = create_requester(bot_user_id)
        event_dict = {
            "type": EventTypes.Message,
            "room_id": room_id,
            "sender": bot_user_id,
            "content": content,
        }
        await self._event_creation_handler.create_and_send_nonmember_event(
            requester,
            event_dict,
            ratelimit=False,
        )

    async def _join_bot_to_room(self, bot_user_id: str, room_id: str) -> bool:
        try:
            requester = create_requester(bot_user_id, authenticated_entity=self._server_name)
            await self._room_member_handler.update_membership(
                requester=requester,
                target=UserID.from_string(bot_user_id),
                room_id=room_id,
                action=Membership.JOIN,
                ratelimit=False,
            )
            logger.info("Setka bot %s joined room %s", bot_user_id, room_id)
        except Exception as e:
            logger.warning(
                "Setka bot join failed for %s in %s: %s",
                bot_user_id,
                room_id,
                e,
            )
            return False

        await self._maybe_send_botcreator_welcome(bot_user_id, room_id)
        return True

    async def _maybe_send_botcreator_welcome(self, bot_user_id: str, room_id: str) -> None:
        localpart = UserID.from_string(bot_user_id).localpart
        if localpart not in ("botcreator_bot", "botcreator"):
            return
        try:
            await self._send_bot_room_message(
                bot_user_id,
                room_id,
                (
                    "Привет! Я BotCreator — помогу создать и настроить ботов Setka.\n"
                    "Напишите /help или /newbot"
                ),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Создать бота", "callback_data": "newbot"}],
                        [{"text": "Мои боты", "callback_data": "mybots"}],
                    ]
                },
            )
        except Exception as e:
            logger.warning("Setka botcreator welcome failed in %s: %s", room_id, e)

    async def _auto_join_bot_invite(self, event: "EventBase") -> None:
        state_key = event.state_key
        if not state_key or state_key not in self._bot_user_ids:
            return
        await self._join_bot_to_room(state_key, event.room_id)

    async def _reconcile_pending_invites(self) -> None:
        try:
            await self._ensure_tables()
            if not self._bot_user_ids:
                await self._refresh_bot_user_cache()
            for bot_user_id in list(self._bot_user_ids):
                await self.accept_pending_invites_for_user(bot_user_id)
        except Exception as e:
            logger.warning("Setka bots invite reconcile failed: %s", e)

    async def _on_new_event(
        self,
        event: "EventBase",
        state_events: dict | None = None,
    ) -> None:
        try:
            await self._ensure_tables()
        except Exception:
            return
        if not self._bot_user_ids:
            await self._refresh_bot_user_cache()
        if not self._bot_user_ids:
            return

        if (
            event.type == EventTypes.Member
            and event.is_state()
            and event.membership == Membership.INVITE
            and event.state_key in self._bot_user_ids
        ):
            logger.info(
                "Setka bot invite detected: %s -> %s",
                event.state_key,
                event.room_id,
            )
            run_as_background_process(
                "setka_bot_auto_join",
                self._hs.hostname,
                self._auto_join_bot_invite,
                event,
            )
            return

        if event.type != EventTypes.Message:
            return
        if event.sender in self._bot_user_ids:
            return

        content = event.content or {}
        if not isinstance(content, dict):
            return

        users = await self._store.get_users_in_room(event.room_id)
        bot_ids_in_room = [u for u in users if u in self._bot_user_ids]
        if not bot_ids_in_room:
            return

        msgtype = str(content.get("msgtype") or "m.text")
        body = str(content.get("body") or "")
        update_message = {
            "message_id": event.event_id,
            "chat_id": event.room_id,
            "date": int(event.origin_server_ts),
            "from": {
                "user_id": event.sender,
            },
            "text": body if msgtype == "m.text" else None,
            "msgtype": msgtype,
        }

        for bot_user_id in bot_ids_in_room:
            bot_id = await self._bot_id_for_user(bot_user_id)
            if not bot_id:
                continue
            await self._enqueue_update(
                bot_id,
                {"message": update_message},
            )

    async def _bot_id_for_user(self, bot_user_id: str) -> str | None:
        def _txn(txn: Any) -> str | None:
            txn.execute(
                "SELECT bot_id FROM setka_bots WHERE bot_user_id = ? AND is_active = 1",
                (bot_user_id,),
            )
            row = txn.fetchone()
            return str(row[0]) if row else None

        return await self._store.db_pool.runInteraction("setka_bots_id_for_user", _txn)
