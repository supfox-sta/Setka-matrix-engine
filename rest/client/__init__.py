# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright 2014-2016 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "account",
    "account_data",
    "account_validity",
    "appservice_ping",
    "auth",
    "auth_metadata",
    "capabilities",
    "delayed_events",
    "devices",
    "directory",
    "events",
    "filter",
    "initial_sync",
    "keys",
    "knock",
    "login",
    "login_token_request",
    "logout",
    "matrixrtc",
    "mutual_rooms",
    "notifications",
    "openid",
    "password_policy",
    "presence",
    "profile",
    "push_rule",
    "pusher",
    "read_marker",
    "receipts",
    "register",
    "relations",
    "rendezvous",
    "reporting",
    "room",
    "room_keys",
    "room_upgrade_rest_servlet",
    "room_wallpaper",
    "setka_plus",
    "setka_profile",
    "setka_privacy",
    "setka_threepid",
    "sendtodevice",
    "sync",
    "tags",
    "contact_list",
    "thirdparty",
    "thread_subscriptions",
    "tokenrefresh",
    "user_directory",
    "versions",
    "voip",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
