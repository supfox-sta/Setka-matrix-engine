# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka privacy settings backed by account_data.

The client currently uses the legacy discoverable booleans for 3PID lookup, but we
also persist Telegram-like visibility tiers for richer profile/privacy screens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from synapse.api.constants import AccountDataTypes
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SetkaPrivacy(TypedDict):
    discoverable_email: bool
    discoverable_msisdn: bool
    email_visibility: Literal["everyone", "contacts", "nobody"]
    phone_visibility: Literal["everyone", "contacts", "nobody"]
    last_seen_visibility: Literal["everyone", "contacts", "nobody"]


DEFAULT_PRIVACY: SetkaPrivacy = {
    "discoverable_email": True,
    "discoverable_msisdn": True,
    "email_visibility": "everyone",
    "phone_visibility": "everyone",
    "last_seen_visibility": "everyone",
}


def _normalize_visibility(value: object, *, default: str) -> Literal["everyone", "contacts", "nobody"]:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"everyone", "contacts", "nobody"}:
            return lowered  # type: ignore[return-value]
    return default  # type: ignore[return-value]


class SetkaPrivacyHandler:
    def __init__(self, hs: "HomeServer"):
        self._account_data_handler = hs.get_account_data_handler()
        self._store = hs.get_datastores().main

    async def get_privacy(self, user_id: str) -> SetkaPrivacy:
        data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PRIVACY
        )
        if not isinstance(data, dict):
            return dict(DEFAULT_PRIVACY)
        return {
            "discoverable_email": bool(
                data.get("discoverable_email", DEFAULT_PRIVACY["discoverable_email"])
            ),
            "discoverable_msisdn": bool(
                data.get("discoverable_msisdn", DEFAULT_PRIVACY["discoverable_msisdn"])
            ),
            "email_visibility": _normalize_visibility(
                data.get("email_visibility"),
                default=DEFAULT_PRIVACY["email_visibility"],
            ),
            "phone_visibility": _normalize_visibility(
                data.get("phone_visibility"),
                default=DEFAULT_PRIVACY["phone_visibility"],
            ),
            "last_seen_visibility": _normalize_visibility(
                data.get("last_seen_visibility"),
                default=DEFAULT_PRIVACY["last_seen_visibility"],
            ),
        }

    async def set_privacy(self, user_id: str, content: JsonDict) -> SetkaPrivacy:
        current = await self.get_privacy(user_id)
        if "discoverable_email" in content:
            current["discoverable_email"] = bool(content["discoverable_email"])
        if "discoverable_msisdn" in content:
            current["discoverable_msisdn"] = bool(content["discoverable_msisdn"])
        if "email_visibility" in content:
            current["email_visibility"] = _normalize_visibility(
                content["email_visibility"],
                default=current["email_visibility"],
            )
        if "phone_visibility" in content:
            current["phone_visibility"] = _normalize_visibility(
                content["phone_visibility"],
                default=current["phone_visibility"],
            )
        if "last_seen_visibility" in content:
            current["last_seen_visibility"] = _normalize_visibility(
                content["last_seen_visibility"],
                default=current["last_seen_visibility"],
            )

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PRIVACY, dict(current)
        )
        return current
