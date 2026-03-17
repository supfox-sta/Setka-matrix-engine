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
"""Handler for persisting custom wallaper data inside account_data."""

from typing import TYPE_CHECKING

from synapse.api.constants import AccountDataTypes
from synapse.handlers.account_data import AccountDataHandler
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class RoomWallpaperHandler:
    """Store per-room wallpaper metadata in account_data."""

    def __init__(self, hs: "HomeServer"):
        self._account_data_handler = hs.get_account_data_handler()
        self._store = hs.get_datastores().main

    async def get_wallpapers(self, user_id: str) -> dict[str, JsonDict]:
        account_data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.ROOM_WALLPAPER
        )
        return self._coerce_rooms(account_data.get("rooms") if account_data else None)

    async def get_wallpaper(self, user_id: str, room_id: str) -> JsonDict:
        rooms = await self.get_wallpapers(user_id)
        metadata = rooms.get(room_id)
        return dict(metadata) if metadata else {}

    async def update_wallpaper(self, user_id: str, room_id: str, metadata: JsonDict) -> None:
        rooms = await self.get_wallpapers(user_id)
        rooms[room_id] = dict(metadata)
        await self._write_wallpapers(user_id, rooms)

    async def remove_wallpaper(self, user_id: str, room_id: str) -> None:
        rooms = await self.get_wallpapers(user_id)
        if room_id not in rooms:
            return
        rooms.pop(room_id)
        await self._write_wallpapers(user_id, rooms)

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

    async def _write_wallpapers(self, user_id: str, rooms: dict[str, JsonDict]) -> None:
        if rooms:
            content: JsonDict = {"rooms": {room_id: dict(metadata) for room_id, metadata in rooms.items()}}
            await self._account_data_handler.add_account_data_for_user(
                user_id, AccountDataTypes.ROOM_WALLPAPER, content
            )
        else:
            await self._account_data_handler.remove_account_data_for_user(
                user_id, AccountDataTypes.ROOM_WALLPAPER
            )
