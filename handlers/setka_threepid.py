# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka 3PID helpers.

Matrix supports phone (msisdn) validation via SMS by default. For Setka we also support
an alternative flow where the verification code is delivered by email (per product
requirements), while still binding the 3PID as medium=msisdn so lookups work.
"""

from __future__ import annotations

import secrets
import uuid
from typing import TYPE_CHECKING, TypedDict

from synapse.api.errors import Codes, SynapseError, ThreepidValidationError

if TYPE_CHECKING:
    from synapse.server import HomeServer


class MsisdnEmailTokenResult(TypedDict):
    sid: str
    msisdn: str


class SetkaThreepidHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._auth_handler = hs.get_auth_handler()
        self._send_email_handler = hs.get_send_email_handler()
        self._clock = hs.get_clock()

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

        # If client didn't specify where to receive the code, fall back to the first bound email.
        if email is None:
            threepids = await self._store.user_get_threepids(requester_user_id)
            email = next((t.address for t in threepids if t.medium == "email"), None)
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
