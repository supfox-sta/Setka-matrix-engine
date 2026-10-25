# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka profile extras stored on the homeserver.

Besides editable profile cosmetics we expose a richer public payload for clients:
bio, profile color, optional badge/status/background, visible 3PIDs and rough last-seen.

Editable data is stored in global account_data under `io.setka.profile`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

from synapse.api.constants import AccountDataTypes
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SetkaProfile(TypedDict, total=False):
    bio: str
    color: str
    badge_emoji_mxc: str
    status_emoji_mxc: str
    background_mxc: str
    email: str
    email_confirmed: bool
    phone: str
    presence_state: Literal["online", "recently", "this_week", "long_ago"]
    last_seen_ts: int
    call_visibility: Literal["everyone", "contacts", "nobody"]
    privacy: JsonDict
    updated_at: int


DEFAULT_PROFILE: SetkaProfile = {
    "bio": "",
}


class SetkaProfileHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._account_data_handler = hs.get_account_data_handler()
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()
        self._privacy_handler = hs.get_setka_privacy_handler()

    async def get_profile(
        self,
        user_id: str,
        *,
        requester_user_id: str | None = None,
        include_private: bool = False,
    ) -> SetkaProfile:
        data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE
        )
        if not isinstance(data, dict):
            return dict(DEFAULT_PROFILE)

        bio = data.get("bio", DEFAULT_PROFILE["bio"])
        color = data.get("color")
        badge = data.get("badge_emoji_mxc")
        status = data.get("status_emoji_mxc")
        background = data.get("background_mxc")
        updated_at = data.get("updated_at")
        result: SetkaProfile = {
            "bio": str(bio) if isinstance(bio, str) else DEFAULT_PROFILE["bio"],
        }
        if isinstance(color, str) and color.strip():
            result["color"] = color.strip()
        if isinstance(badge, str) and badge.strip():
            result["badge_emoji_mxc"] = badge.strip()
        if isinstance(status, str) and status.strip():
            result["status_emoji_mxc"] = status.strip()
        if isinstance(background, str) and background.strip():
            result["background_mxc"] = background.strip()
        if isinstance(updated_at, int) and updated_at > 0:
            result["updated_at"] = updated_at

        privacy = await self._privacy_handler.get_privacy(user_id)
        reveal_email = include_private or requester_user_id == user_id or privacy["email_visibility"] != "nobody"
        reveal_phone = include_private or requester_user_id == user_id or privacy["phone_visibility"] != "nobody"
        reveal_last_seen = include_private or requester_user_id == user_id or privacy["last_seen_visibility"] != "nobody"

        profile_data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE
        )
        threepids = await self._store.user_get_threepids(user_id)
        if reveal_email:
            email = next((tp.address for tp in threepids if tp.medium == "email" and tp.address), None)
            if email:
                result["email"] = email
                if include_private:
                    result["email_confirmed"] = True
            elif include_private and isinstance(profile_data, dict):
                stored_email = profile_data.get("email")
                if isinstance(stored_email, str) and stored_email.strip():
                    result["email"] = stored_email.strip()
                    result["email_confirmed"] = profile_data.get("email_confirmed") is True
        if reveal_phone:
            phone = next((tp.address for tp in threepids if tp.medium == "msisdn" and tp.address), None)
            if phone:
                result["phone"] = phone
        if reveal_last_seen:
            presence = await self._build_presence(user_id)
            result.update(presence)

        result["call_visibility"] = privacy["call_visibility"]

        if include_private or requester_user_id == user_id:
            result["privacy"] = dict(privacy)
        return result

    async def set_profile(self, user_id: str, content: JsonDict) -> SetkaProfile:
        current = await self.get_profile(user_id, requester_user_id=user_id, include_private=True)

        if "bio" in content:
            bio = content["bio"]
            current["bio"] = str(bio) if isinstance(bio, str) else ""

        if "color" in content:
            color = content["color"]
            if isinstance(color, str) and color.strip():
                current["color"] = color.strip()
            else:
                current.pop("color", None)

        if "badge_emoji_mxc" in content:
            badge = content["badge_emoji_mxc"]
            if isinstance(badge, str) and badge.strip():
                current["badge_emoji_mxc"] = badge.strip()
            else:
                current.pop("badge_emoji_mxc", None)

        if "status_emoji_mxc" in content:
            status = content["status_emoji_mxc"]
            if isinstance(status, str) and status.strip():
                subscription = await self._hs.get_setka_plus_handler().get_subscription(user_id)
                if subscription.get("is_active") is not True:
                    raise SynapseError(
                        403,
                        "Setka Plus subscription is required for status emoji.",
                        Codes.FORBIDDEN,
                    )
                current["status_emoji_mxc"] = status.strip()
            else:
                current.pop("status_emoji_mxc", None)

        if "background_mxc" in content:
            background = content["background_mxc"]
            if isinstance(background, str) and background.strip():
                current["background_mxc"] = background.strip()
            else:
                current.pop("background_mxc", None)

        current["updated_at"] = int(self._clock.time_msec())
        current.pop("email", None)
        current.pop("email_confirmed", None)
        current.pop("phone", None)
        current.pop("presence_state", None)
        current.pop("last_seen_ts", None)
        current.pop("privacy", None)

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE, dict(current)
        )
        return await self.get_profile(user_id, requester_user_id=user_id, include_private=True)

    async def _build_presence(self, user_id: str) -> SetkaProfile:
        last_seen_ts = await self._store.get_last_seen_for_user_id(user_id)
        if not last_seen_ts:
            return {}

        now = int(self._clock.time_msec())
        elapsed = max(0, now - last_seen_ts)
        if elapsed <= 2 * 60 * 1000:
            presence_state: Literal["online", "recently", "this_week", "long_ago"] = "online"
        elif elapsed <= 2 * 24 * 60 * 60 * 1000:
            presence_state = "recently"
        elif elapsed <= 7 * 24 * 60 * 60 * 1000:
            presence_state = "this_week"
        else:
            presence_state = "long_ago"
        return {
            "presence_state": presence_state,
            "last_seen_ts": int(last_seen_ts),
        }
