#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020-2021 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
#

import logging
import re
from typing import TYPE_CHECKING

from synapse.http.server import set_corp_headers, set_cors_headers
from synapse.http.servlet import RestServlet, parse_integer, parse_string
from synapse.http.site import SynapseRequest
from synapse.media._base import (
    DEFAULT_MAX_TIMEOUT_MS,
    MAXIMUM_ALLOWED_MAX_TIMEOUT_MS,
    respond_404,
)
from synapse.media.media_storage import MediaStorage
from synapse.media.thumbnailer import ThumbnailProvider
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.media.media_repository import MediaRepository
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ThumbnailResource(RestServlet):
    PATTERNS = [
        re.compile(
            "/_matrix/media/(r0|v3|v1)/thumbnail/(?P<server_name>[^/]*)/(?P<media_id>[^/]*)$"
        )
    ]

    def __init__(
        self,
        hs: "HomeServer",
        media_repo: "MediaRepository",
        media_storage: MediaStorage,
    ):
        super().__init__()

        self.store = hs.get_datastores().main
        self.media_repo = media_repo
        self.media_storage = media_storage
        self._is_mine_server_name = hs.is_mine_server_name
        self._server_name = hs.hostname
        self.prevent_media_downloads_from = hs.config.media.prevent_media_downloads_from
        self.dynamic_thumbnails = hs.config.media.dynamic_thumbnails
        self.thumbnail_provider = ThumbnailProvider(hs, media_repo, media_storage)

    async def _setka_is_public_media(self, server_name: str, media_id: str) -> bool:
        mxc = f"mxc://{server_name}/{media_id}"

        def _txn(txn) -> bool:  # type: ignore[no-untyped-def]
            txn.execute("SELECT 1 FROM profiles WHERE avatar_url = ? LIMIT 1", (mxc,))
            if txn.fetchone():
                return True
            txn.execute(
                "SELECT 1 FROM room_stats_state WHERE avatar = ? LIMIT 1", (mxc,)
            )
            if txn.fetchone():
                return True
            txn.execute(
                "SELECT 1 FROM account_data WHERE account_data_type = ? "
                "AND content LIKE ? LIMIT 1",
                ("io.setka.profile", f"%{mxc}%"),
            )
            return txn.fetchone() is not None

        try:
            return await self.store.db_pool.runInteraction("setka_public_media", _txn)
        except Exception:
            logger.exception("setka public media check failed")
            return False

    async def on_GET(
        self, request: SynapseRequest, server_name: str, media_id: str
    ) -> None:
        # Validate the server name, raising if invalid
        parse_and_validate_server_name(server_name)

        set_cors_headers(request)
        set_corp_headers(request)
        width = parse_integer(request, "width", required=True)
        height = parse_integer(request, "height", required=True)
        method = parse_string(request, "method", "scale")
        # TODO Parse the Accept header to get an prioritised list of thumbnail types.
        m_type = "image/png"
        max_timeout_ms = parse_integer(
            request, "timeout_ms", default=DEFAULT_MAX_TIMEOUT_MS
        )
        max_timeout_ms = min(max_timeout_ms, MAXIMUM_ALLOWED_MAX_TIMEOUT_MS)

        if self._is_mine_server_name(server_name):
            # Setka: avatars / profile banners / room avatars are public (share page), so
            # small thumbnails of exactly that media are served without authentication.
            public = False
            if width <= 800 and height <= 800:
                public = await self._setka_is_public_media(server_name, media_id)
            if self.dynamic_thumbnails:
                await self.thumbnail_provider.select_or_generate_local_thumbnail(
                    request,
                    media_id,
                    width,
                    height,
                    method,
                    m_type,
                    max_timeout_ms,
                    False,
                    allow_authenticated=public,
                )
            else:
                await self.thumbnail_provider.respond_local_thumbnail(
                    request,
                    media_id,
                    width,
                    height,
                    method,
                    m_type,
                    max_timeout_ms,
                    False,
                    allow_authenticated=public,
                )
            self.media_repo.mark_recently_accessed(None, media_id)
        else:
            # Don't let users download media from configured domains, even if it
            # is already downloaded. This is Trust & Safety tooling to make some
            # media inaccessible to local users.
            # See `prevent_media_downloads_from` config docs for more info.
            if server_name in self.prevent_media_downloads_from:
                respond_404(request)
                return

            ip_address = request.getClientAddress().host
            remote_resp_function = (
                self.thumbnail_provider.select_or_generate_remote_thumbnail
                if self.dynamic_thumbnails
                else self.thumbnail_provider.respond_remote_thumbnail
            )
            await remote_resp_function(
                request,
                server_name,
                media_id,
                width,
                height,
                method,
                m_type,
                max_timeout_ms,
                ip_address,
                use_federation=False,
                allow_authenticated=False,
            )
            self.media_repo.mark_recently_accessed(server_name, media_id)
