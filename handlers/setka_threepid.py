# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka 3PID helpers.

Matrix supports phone (msisdn) validation via SMS by default. For Setka we also support
an alternative flow where the verification code is delivered by email (per product
requirements), while still binding the 3PID as medium=msisdn so lookups work.
"""

from __future__ import annotations

import logging
import random
import secrets
import uuid
from typing import TYPE_CHECKING, TypedDict

from synapse.api.constants import AccountDataTypes
from synapse.api.errors import Codes, SynapseError, ThreepidValidationError
from synapse.metrics import SERVER_NAME_LABEL, threepid_send_requests
from synapse.push.mailer import Mailer
from synapse.util.duration import Duration
from synapse.util.stringutils import random_string
from synapse.util.threepids import canonicalise_email, check_3pid_allowed

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MsisdnEmailTokenResult(TypedDict):
    sid: str
    msisdn: str


class SetkaThreepidHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._auth_handler = hs.get_auth_handler()
        self._send_email_handler = hs.get_send_email_handler()
        self._identity_handler = hs.get_identity_handler()
        self._clock = hs.get_clock()
        self._server_name = hs.hostname
        self._config = hs.config
        self._mailer: Mailer | None = None
        if self._config.email.can_verify_email:
            self._mailer = Mailer(
                hs=hs,
                app_name=self._config.email.email_app_name,
                template_html=self._config.email.email_add_threepid_template_html,
                template_text=self._config.email.email_add_threepid_template_text,
            )

    async def request_email_token(
        self,
        email: str,
        client_secret: str,
        send_attempt: int,
        next_link: str | None = None,
    ) -> str:
        """Send a 6-digit verification code to bind an email 3PID (Setka flow)."""
        try:
            normalised_email = canonicalise_email(email.strip())
        except ValueError as e:
            raise SynapseError(400, str(e), Codes.INVALID_PARAM)

        if not await check_3pid_allowed(self._hs, "email", normalised_email):
            raise SynapseError(
                403,
                "Your email domain is not authorized on this server",
                Codes.THREEPID_DENIED,
            )

        existing_user_id = await self._store.get_user_id_by_threepid(
            "email", normalised_email
        )
        if existing_user_id is not None:
            if self._config.server.request_token_inhibit_3pid_errors:
                await self._hs.get_clock().sleep(
                    Duration(milliseconds=random.randint(100, 1000))
                )
                return random_string(16)
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        sid = uuid.uuid4().hex
        token = f"{secrets.randbelow(1_000_000):06d}"
        expires = self._clock.time_msec() + (15 * 60 * 1000)

        await self._store.start_or_continue_validation_session(
            medium="email",
            address=normalised_email,
            session_id=sid,
            client_secret=client_secret,
            send_attempt=send_attempt,
            next_link=next_link,
            token=token,
            token_expires=expires,
        )

        subject = "Код подтверждения почты"
        text = (
            f"Ваш код подтверждения почты {normalised_email}: {token}\n\n"
            "Если вы не запрашивали подтверждение, просто проигнорируйте это письмо."
        )
        html = (
            f"<p>Ваш код подтверждения почты <b>{normalised_email}</b>:</p>"
            f"<p style=\"font-size: 24px; letter-spacing: 2px;\"><b>{token}</b></p>"
            "<p>Если вы не запрашивали подтверждение, просто проигнорируйте это письмо.</p>"
        )
        await self._send_email_handler.send_email(
            email_address=normalised_email,
            subject=subject,
            app_name="Setka",
            html=html,
            text=text,
        )

        threepid_send_requests.labels(
            type="email",
            reason="add_threepid",
            **{SERVER_NAME_LABEL: self._server_name},
        ).observe(send_attempt)
        return sid

    async def submit_email_token_and_bind(
        self,
        requester_user_id: str,
        sid: str,
        client_secret: str,
        token: str,
    ) -> None:
        try:
            await self._store.validate_threepid_session(
                session_id=sid,
                client_secret=client_secret,
                token=token,
                current_ts=self._clock.time_msec(),
            )
        except ThreepidValidationError as e:
            raise SynapseError(400, str(e), Codes.THREEPID_AUTH_FAILED)

        session = await self._store.get_threepid_validation_session(
            medium="email",
            client_secret=client_secret,
            sid=sid,
            validated=True,
        )
        if session is None or session.validated_at is None:
            raise SynapseError(400, "Session not validated", Codes.THREEPID_AUTH_FAILED)

        existing_user = await self._store.get_user_id_by_threepid("email", session.address)
        if existing_user is not None and existing_user != requester_user_id:
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        await self._auth_handler.add_threepid(
            requester_user_id, "email", session.address, int(session.validated_at)
        )
        await self._store.delete_threepid_session(session.session_id)
        await self._mark_profile_email_validated(requester_user_id, session.address)

    async def add_email_threepid(
        self,
        requester_user_id: str,
        sid: str,
        client_secret: str,
    ) -> None:
        validation_session = await self._identity_handler.validate_threepid_session(
            client_secret, sid
        )
        if not validation_session:
            raise SynapseError(
                400, "No validated 3pid session found", Codes.THREEPID_AUTH_FAILED
            )

        await self._auth_handler.add_threepid(
            requester_user_id,
            validation_session["medium"],
            validation_session["address"],
            validation_session["validated_at"],
        )

    async def request_msisdn_token_via_email(
        self,
        requester_user_id: str,
        phone_number: str,
        client_secret: str,
        send_attempt: int,
        next_link: str | None = None,
        email: str | None = None,
    ) -> MsisdnEmailTokenResult:
        msisdn = (phone_number or "").strip()
        if not msisdn:
            raise SynapseError(400, "Missing phone_number", Codes.MISSING_PARAM)
        if not msisdn.startswith("+"):
            raise SynapseError(
                400, "Phone must be in E.164 format and start with '+'", Codes.INVALID_PARAM
            )

        # If client didn't specify where to receive the code, fall back to bound email
        # or the email stored in Setka profile account_data (registration / MAS flows).
        email = await self._resolve_delivery_email(requester_user_id, email)
        if not email:
            raise SynapseError(400, "Missing email", Codes.MISSING_PARAM)

        existing_user = await self._store.get_user_id_by_threepid("msisdn", msisdn)
        if existing_user is not None and existing_user != requester_user_id:
            raise SynapseError(400, "Phone number already in use", Codes.THREEPID_IN_USE)

        sid = uuid.uuid4().hex
        token = f"{secrets.randbelow(1_000_000):06d}"
        expires = self._clock.time_msec() + (15 * 60 * 1000)

        await self._store.start_or_continue_validation_session(
            medium="msisdn",
            address=msisdn,
            session_id=sid,
            client_secret=client_secret,
            send_attempt=send_attempt,
            next_link=next_link,
            token=token,
            token_expires=expires,
        )

        subject = "Код подтверждения номера"
        text = (
            f"Ваш код подтверждения номера {msisdn}: {token}\n\n"
            "Если вы не запрашивали подтверждение, просто проигнорируйте это письмо."
        )
        html = (
            f"<p>Ваш код подтверждения номера <b>{msisdn}</b>:</p>"
            f"<p style=\"font-size: 24px; letter-spacing: 2px;\"><b>{token}</b></p>"
            "<p>Если вы не запрашивали подтверждение, просто проигнорируйте это письмо.</p>"
        )
        # Some historical versions of this file accidentally contained mojibake in the
        # email templates. Override with readable Russian strings.
        subject = "Код подтверждения номера"
        text = (
            f"Ваш код подтверждения номера {msisdn}: {token}\n\n"
            "Если вы не запрашивали подтверждение, просто проигнорируйте это письмо."
        )
        html = (
            f"<p>Ваш код подтверждения номера <b>{msisdn}</b>:</p>"
            f"<p style=\"font-size: 24px; letter-spacing: 2px;\"><b>{token}</b></p>"
            "<p>Если вы не запрашивали подтверждение, просто проигнорируйте это письмо.</p>"
        )
        await self._send_email_handler.send_email(
            email_address=email,
            subject=subject,
            app_name="Setka",
            html=html,
            text=text,
        )

        return {"sid": sid, "msisdn": msisdn}

    async def submit_msisdn_token_and_bind(
        self,
        requester_user_id: str,
        sid: str,
        client_secret: str,
        token: str,
    ) -> None:
        try:
            await self._store.validate_threepid_session(
                session_id=sid,
                client_secret=client_secret,
                token=token,
                current_ts=self._clock.time_msec(),
            )
        except ThreepidValidationError as e:
            raise SynapseError(400, str(e), Codes.THREEPID_AUTH_FAILED)

        session = await self._store.get_threepid_validation_session(
            medium="msisdn",
            client_secret=client_secret,
            sid=sid,
            validated=True,
        )
        if session is None or session.validated_at is None:
            raise SynapseError(400, "Session not validated", Codes.THREEPID_AUTH_FAILED)

        existing_user = await self._store.get_user_id_by_threepid("msisdn", session.address)
        if existing_user is not None and existing_user != requester_user_id:
            raise SynapseError(400, "Phone number already in use", Codes.THREEPID_IN_USE)

        await self._auth_handler.add_threepid(
            requester_user_id, "msisdn", session.address, int(session.validated_at)
        )
        await self._store.delete_threepid_session(session.session_id)

    async def _resolve_delivery_email(
        self, user_id: str, email: str | None
    ) -> str | None:
        """Resolve where to send phone verification codes.

        Priority: explicit client param → bound email 3PID → `io.setka.profile` account_data.
        """
        if isinstance(email, str) and email.strip():
            return email.strip()

        threepids = await self._store.user_get_threepids(user_id)
        bound_email = next((t.address for t in threepids if t.medium == "email"), None)
        if bound_email:
            return bound_email

        profile_data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE
        )
        if isinstance(profile_data, dict):
            stored_email = profile_data.get("email")
            if isinstance(stored_email, str) and stored_email.strip():
                return stored_email.strip()

        return None

    async def admin_confirm_email(
        self,
        user_id: str,
        email: str | None = None,
    ) -> str:
        """Bind a user's email 3PID without the client verification flow (admin only)."""
        resolved_email = (email or "").strip()
        if not resolved_email:
            profile_data = await self._store.get_global_account_data_by_type_for_user(
                user_id, AccountDataTypes.SETKA_PROFILE
            )
            if isinstance(profile_data, dict):
                stored_email = profile_data.get("email")
                if isinstance(stored_email, str) and stored_email.strip():
                    resolved_email = stored_email.strip()

        if not resolved_email:
            raise SynapseError(400, "No email to confirm", Codes.MISSING_PARAM)

        try:
            resolved_email = canonicalise_email(resolved_email)
        except ValueError as e:
            raise SynapseError(400, str(e), Codes.INVALID_PARAM)

        existing_user = await self._store.get_user_id_by_threepid("email", resolved_email)
        if existing_user is not None and existing_user != user_id:
            raise SynapseError(400, "Email is already in use", Codes.THREEPID_IN_USE)

        threepids = await self._store.user_get_threepids(user_id)
        bound_email = next((tp.address for tp in threepids if tp.medium == "email"), None)
        validated_at = int(self._clock.time_msec())
        if bound_email != resolved_email:
            if bound_email:
                await self._auth_handler.delete_local_threepid(user_id, "email", bound_email)
            await self._auth_handler.add_threepid(
                user_id, "email", resolved_email, validated_at
            )

        await self._mark_profile_email_validated(user_id, resolved_email)
        return resolved_email

    async def _mark_profile_email_validated(self, user_id: str, email: str) -> None:
        """Keep io.setka.profile account_data in sync after email binding."""
        profile_data = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE
        )
        current = dict(profile_data) if isinstance(profile_data, dict) else {}
        current["email"] = email
        current["email_validated_at"] = int(self._clock.time_msec())
        current["email_confirmed"] = True
        await self._hs.get_account_data_handler().add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PROFILE, current
        )
