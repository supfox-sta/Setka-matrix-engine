# This file is licensed under the Affero General Public License (AGPL) version 3.
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
"""Handler for per-user contact metadata backed by account_data."""

from typing import TYPE_CHECKING, TypedDict

from synapse.api.constants import AccountDataTypes
from synapse.handlers.account_data import AccountDataHandler
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ContactMetadata(TypedDict, total=False):
    """Fields that can be stored for an entry in the contact list."""

    display_name: str
    avatar_url: str
    note: str
    order: int
    tags: list[str]
    pinned: bool
    first_name: str
    last_name: str
    email: str
    phone: str


class ContactList(TypedDict, total=False):
    """Structure sent back to clients."""

    rooms: dict[str, JsonDict]


class ContactListHandler:
    """Store per-room contact metadata in account_data."""

    def __init__(self, hs: "HomeServer"):
        self._account_data_handler = hs.get_account_data_handler()
        self._store = hs.get_datastores().main

    async def get_contact_list(self, user_id: str) -> ContactList:
        """Return the contact list for a user."""

        rooms = await self._get_rooms_dict(user_id)
        return {"rooms": rooms}

    async def get_contact_metadata(
        self, user_id: str, room_id: str
    ) -> JsonDict:
        """Return metadata for a specific room."""

        rooms = await self._get_rooms_dict(user_id)
        metadata = rooms.get(room_id)
        return dict(metadata) if metadata else {}

    async def update_contact_metadata(
        self, user_id: str, room_id: str, metadata: JsonDict
    ) -> None:
        """Set metadata for a single room."""

        rooms = await self._get_rooms_dict(user_id)
        rooms[room_id] = dict(metadata)
        await self._write_contact_list(user_id, rooms)

    async def remove_contact_metadata(self, user_id: str, room_id: str) -> None:
        """Drop metadata for a room."""

        rooms = await self._get_rooms_dict(user_id)
        if room_id not in rooms:
            return
        rooms.pop(room_id)
        await self._write_contact_list(user_id, rooms)

    async def _get_rooms_dict(self, user_id: str) -> dict[str, JsonDict]:
        account_data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.CONTACT_LIST
        )
        return self._coerce_rooms(account_data.get("rooms") if account_data else None)

    def _coerce_rooms(self, raw_rooms: object | None) -> dict[str, JsonDict]:
        normalized: dict[str, JsonDict] = {}

        if not isinstance(raw_rooms, dict):
            return normalized

        for room_id, metadata in raw_rooms.items():
            if not isinstance(room_id, str):
                continue
            if not isinstance(metadata, dict):
                continue
            normalized[room_id] = dict(metadata)

        return normalized

    async def _write_contact_list(
        self, user_id: str, rooms: dict[str, JsonDict]
    ) -> None:
        if rooms:
            content: JsonDict = {
                "rooms": {room_id: dict(metadata) for room_id, metadata in rooms.items()}
            }
            await self._account_data_handler.add_account_data_for_user(
                user_id, AccountDataTypes.CONTACT_LIST, content
            )
        else:
            await self._account_data_handler.remove_account_data_for_user(
                user_id, AccountDataTypes.CONTACT_LIST
            )
