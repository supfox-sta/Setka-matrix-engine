# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Bridge between Synapse Setka 3PID flows and a co-located Sydent instance.

Bindings are mirrored to Sydent via the internal bind API (no access token).
That keeps Element Web, admin tools and standard IS lookups in sync with Setka
custom 3PID flows.

Optional Sydent v2 lookup fallback uses a service Matrix account whose IS token
is obtained automatically — no manual SETKA_SYDENT_ACCESS_TOKEN rotation.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import TYPE_CHECKING, Optional

from synapse.util.threepids import canonicalise_email

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def _sha256_urlsafe_base64(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _normalise_address(address: str, medium: str) -> str:
    if medium == "email":
        try:
            return canonicalise_email(address)
        except ValueError:
            return address.casefold()
    return address


class SetkaSydentHandler:
    """Optional integration with Sydent for 3PID discovery and binding sync."""

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._http = hs.get_proxied_http_client()
        self._server_name = hs.config.server.server_name

        base = os.environ.get("SETKA_SYDENT_URL", "").strip().rstrip("/")
        self._base_url = base or None
        internal = os.environ.get("SETKA_SYDENT_INTERNAL_URL", "").strip().rstrip("/")
        self._internal_url = internal or None

        self._lookup_pepper = os.environ.get("SETKA_SYDENT_LOOKUP_PEPPER", "").strip()
        self._static_access_token = os.environ.get(
            "SETKA_SYDENT_ACCESS_TOKEN", ""
        ).strip()
        self._service_mxid = os.environ.get("SETKA_SYDENT_SERVICE_MXID", "").strip()
        self._service_password = os.environ.get(
            "SETKA_SYDENT_SERVICE_PASSWORD", ""
        ).strip()

        self._cached_lookup_token: str | None = (
            self._static_access_token or None
        )

        hs.get_module_api().register_third_party_rules_callbacks(
            on_add_user_third_party_identifier=self._on_threepid_added,
            on_remove_user_third_party_identifier=self._on_threepid_removed,
        )

    def is_enabled(self) -> bool:
        return self._base_url is not None

    def is_bind_configured(self) -> bool:
        return bool(self._internal_url)

    def is_lookup_configured(self) -> bool:
        if not self._base_url or not self._lookup_pepper:
            return False
        if self._static_access_token:
            return True
        return bool(self._service_mxid and self._service_password)

    async def _on_threepid_added(
        self, user_id: str, medium: str, address: str
    ) -> None:
        await self.sync_binding(user_id, medium, address)

    async def _on_threepid_removed(
        self, user_id: str, medium: str, address: str
    ) -> None:
        await self.remove_binding(medium, address, mxid=user_id)

    async def lookup_mxid(self, medium: str, address: str) -> Optional[str]:
        """Try to resolve an mxid from Sydent when Synapse local store misses."""
        if not self._base_url or not self._lookup_pepper:
            return None

        access_token = await self._get_lookup_access_token()
        if not access_token:
            return None

        hashed_address = self._hash_address(medium, address)
        body = {
            "addresses": [hashed_address],
            "algorithm": "sha256",
            "pepper": self._lookup_pepper,
        }
        headers = {
            b"Authorization": [f"Bearer {access_token}".encode("utf-8")],
            b"Content-Type": [b"application/json"],
        }
        try:
            url = f"{self._base_url}/_matrix/identity/v2/lookup"
            response = await self._http.post_json_get_json(url, body, headers=headers)
            mappings = response.get("mappings", {})
            if isinstance(mappings, dict):
                return mappings.get(hashed_address)
        except Exception as exc:
            logger.warning("Setka Sydent lookup failed: %s", exc)
            if not self._static_access_token:
                self._cached_lookup_token = None
        return None

    async def sync_binding(self, user_id: str, medium: str, address: str) -> None:
        """Best-effort push of a validated binding to Sydent internal API."""
        if not self._internal_url:
            return

        address = _normalise_address(address, medium)
        body = {
            "mxid": user_id,
            "medium": medium,
            "address": address,
        }
        headers = {b"Content-Type": [b"application/json"]}
        try:
            url = f"{self._internal_url}/_matrix/identity/internal/bind"
            await self._http.post_json_get_json(url, body, headers=headers)
            logger.info(
                "Synced 3PID binding to Sydent: %s %s -> %s",
                medium,
                address,
                user_id,
            )
        except Exception as exc:
            logger.warning("Setka Sydent bind sync failed for %s: %s", user_id, exc)

    async def remove_binding(
        self, medium: str, address: str, *, mxid: str | None = None
    ) -> None:
        """Best-effort removal of a binding from Sydent."""
        if not self._internal_url:
            return

        address = _normalise_address(address, medium)
        body: dict[str, str] = {
            "medium": medium,
            "address": address,
        }
        if mxid:
            body["mxid"] = mxid
        headers = {b"Content-Type": [b"application/json"]}
        try:
            url = f"{self._internal_url}/_matrix/identity/internal/unbind"
            await self._http.post_json_get_json(url, body, headers=headers)
            logger.info("Removed 3PID binding from Sydent: %s %s", medium, address)
        except Exception as exc:
            logger.warning("Setka Sydent unbind failed: %s", exc)

    async def sync_user_threepids(self, user_id: str) -> int:
        """Push all validated local 3PIDs for a user to Sydent."""
        if not self._internal_url:
            return 0

        threepids = await self._store.user_get_threepids(user_id)
        synced = 0
        for threepid in threepids:
            if threepid.medium not in ("email", "msisdn"):
                continue
            await self.sync_binding(user_id, threepid.medium, threepid.address)
            synced += 1
        return synced

    async def _get_lookup_access_token(self) -> str | None:
        if self._cached_lookup_token:
            return self._cached_lookup_token

        if not self._service_mxid or not self._service_password:
            return None

        try:
            matrix_token = await self._login_service_account()
            is_token = await self._register_identity_account(matrix_token)
            self._cached_lookup_token = is_token
            return is_token
        except Exception as exc:
            logger.warning("Failed to obtain Sydent lookup token: %s", exc)
            return None

    async def _login_service_account(self) -> str:
        localpart = self._service_mxid
        if localpart.startswith("@"):
            localpart = localpart[1:]
        if ":" in localpart:
            localpart = localpart.split(":", 1)[0]

        login_url = (
            f"{self._hs.config.server.public_baseurl}_matrix/client/v3/login"
        )
        body = {
            "type": "m.login.password",
            "identifier": {
                "type": "m.id.user",
                "user": localpart,
            },
            "password": self._service_password,
        }
        response = await self._http.post_json_get_json(login_url, body)
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Matrix login for Sydent service account failed")
        return access_token

    async def _register_identity_account(self, matrix_access_token: str) -> str:
        if not self._base_url:
            raise RuntimeError("SETKA_SYDENT_URL is not configured")

        url = f"{self._base_url}/_matrix/identity/v2/account/register"
        body = {
            "matrix_server_name": self._server_name,
            "access_token": matrix_access_token,
        }
        headers = {b"Content-Type": [b"application/json"]}
        response = await self._http.post_json_get_json(url, body, headers=headers)
        token = response.get("access_token") or response.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Sydent account registration failed")
        return token

    def _hash_address(self, medium: str, address: str) -> str:
        normalised = _normalise_address(address, medium)
        str_to_hash = " ".join([normalised, medium, self._lookup_pepper])
        return _sha256_urlsafe_base64(str_to_hash)
