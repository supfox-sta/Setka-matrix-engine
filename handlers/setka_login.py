# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026

"""Setka login verification via server notice codes."""

from __future__ import annotations

import hashlib
import base64
import logging
import os
import secrets
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from synapse.api.constants import AccountDataTypes, EventTypes
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_CHALLENGE_TTL_MS = 15 * 60 * 1000
_MAX_ATTEMPTS = 8
_RECOVERY_BACKUP_KEY = "recovery_backup"
_RECOVERY_BACKUP_VERSION = 1


def _now_ms(clock) -> int:
    return clock.time_msec()


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _clean_str(value: object, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_len]


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + ("*" * (len(local) - 2)) + local[-1]
    return f"{masked_local}@{domain}"


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


class SetkaLoginHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._account_data_handler = hs.get_account_data_handler()
        self._server_notices_manager = hs.get_server_notices_manager()
        self._send_email_handler = hs.get_send_email_handler()
        self._clock = hs.get_clock()

    async def get_status(
        self,
        user_id: str,
        device_id: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"

        ip = _as_str(ip)
        user_agent = _as_str(user_agent)

        data = await self._load_data(user_id)
        verified = data.get("verified_devices")
        if isinstance(verified, dict):
            if device_key in verified:
                return {
                    "required": False,
                    "verified": True,
                    "recovery_backup_available": self._has_recovery_backup(data),
                }

            # Account was already verified on another device/session (e.g. after re-login
            # Matrix assigns a new device id). Trust the account and verify this device.
            if verified:
                await self._mark_verified(user_id, device_key, data)
                return {
                    "required": False,
                    "verified": True,
                    "recovery_backup_available": self._has_recovery_backup(data),
                }

        recovery_backup_available = self._has_recovery_backup(data)
        if recovery_backup_available:
            await self._record_skey_silent_success(user_id)
            await self._mark_verified(user_id, device_key, data)
            return {
                "required": False,
                "verified": True,
                "ip": ip or "",
                "region": self._resolve_region(ip),
                "expires_at": 0,
                "email_available": False,
                "email_hint": "",
                "recovery_backup_available": True,
            }

        return {
            "required": True,
            "verified": False,
            "ip": ip or "",
            "region": self._resolve_region(ip),
            "expires_at": 0,
            "email_available": False,
            "email_hint": "",
            "recovery_backup_available": False,
        }

    async def send_code_via_email(
        self,
        user_id: str,
        device_id: str,
        *,
        email: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"

        delivery_email = await self._resolve_delivery_email(user_id, email)
        if not delivery_email:
            raise SynapseError(
                400,
                "No email address is linked to this account",
                Codes.NOT_FOUND,
            )

        ip = _as_str(ip)
        user_agent = _as_str(user_agent)

        data = await self._load_data(user_id)
        if not self._has_recovery_backup(data):
            raise SynapseError(
                403,
                "Email verification requires server recovery backup",
                Codes.FORBIDDEN,
            )

        challenges = data.setdefault("challenges", {})
        if not isinstance(challenges, dict):
            challenges = {}
            data["challenges"] = challenges

        challenge = challenges.get(device_key)
        now = _now_ms(self._clock)
        if not isinstance(challenge, dict) or int(challenge.get("expires_at", 0)) <= now:
            await self._ensure_challenge(
                user_id,
                device_key,
                data,
                ip=ip,
                user_agent=user_agent,
            )
            data = await self._load_data(user_id)
            challenges = data.setdefault("challenges", {})
            if not isinstance(challenges, dict):
                challenges = {}
                data["challenges"] = challenges
            challenge = challenges.get(device_key)
            if not isinstance(challenge, dict):
                raise SynapseError(500, "Failed to create login challenge", Codes.UNKNOWN)
            now = _now_ms(self._clock)

        code = self._rotate_code(challenge, now)
        ip_line = ip or str(challenge.get("ip") or "")
        region = str(challenge.get("region") or self._resolve_region(ip_line))
        challenge["ip"] = ip_line
        challenge["region"] = region
        await self._save_data(user_id, data)

        try:
            await self._send_email(
                delivery_email,
                code=code,
                ip=ip_line,
                region=region,
            )
        except Exception as exc:
            logger.exception(
                "Failed to send Setka login verification email to %s for %s",
                delivery_email,
                user_id,
            )
            raise SynapseError(
                503,
                f"Email delivery failed: {exc}",
                Codes.UNKNOWN,
            ) from exc

        notice_sent = await self._send_notice(
            user_id,
            ip=ip_line,
            region=region,
            code=code,
        )
        challenge["notice_sent"] = notice_sent
        await self._save_data(user_id, data)

        return {
            "sent": True,
            "email": _mask_email(delivery_email),
        }

    async def verify_code(self, user_id: str, device_id: str, code: str) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"
        cleaned = _clean_str(code, max_len=6)
        if not cleaned or not cleaned.isdigit() or len(cleaned) != 6:
            raise SynapseError(400, "Code must be 6 digits", Codes.INVALID_PARAM)

        data = await self._load_data(user_id)
        challenges = data.setdefault("challenges", {})
        if not isinstance(challenges, dict):
            challenges = {}
            data["challenges"] = challenges

        challenge = challenges.get(device_key)
        if not isinstance(challenge, dict):
            raise SynapseError(404, "No active login challenge", Codes.NOT_FOUND)

        expires_at = int(challenge.get("expires_at", 0))
        if expires_at <= _now_ms(self._clock):
            challenges.pop(device_key, None)
            await self._save_data(user_id, data)
            raise SynapseError(403, "Verification code expired", Codes.FORBIDDEN)

        attempts = int(challenge.get("attempts", 0)) + 1
        challenge["attempts"] = attempts
        if attempts > _MAX_ATTEMPTS:
            challenges.pop(device_key, None)
            await self._save_data(user_id, data)
            raise SynapseError(403, "Too many attempts", Codes.FORBIDDEN)

        expected_hash = challenge.get("code_hash")
        if not isinstance(expected_hash, str) or _hash_code(cleaned) != expected_hash:
            await self._save_data(user_id, data)
            raise SynapseError(403, "Invalid verification code", Codes.FORBIDDEN)

        await self._mark_verified(user_id, device_key, data)
        data = await self._load_data(user_id)
        return {
            "verified": True,
            "method": "code",
            "recovery_backup_available": self._has_recovery_backup(data),
        }

    async def store_recovery_key(
        self, user_id: str, device_id: str, recovery_key: str
    ) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"

        cleaned = _clean_str(recovery_key, max_len=512)
        if not cleaned:
            raise SynapseError(400, "Invalid recovery key", Codes.INVALID_PARAM)

        data = await self._load_data(user_id)
        had_backup = isinstance(data.get(_RECOVERY_BACKUP_KEY), dict)
        data[_RECOVERY_BACKUP_KEY] = self._encrypt_recovery_backup(user_id, cleaned)
        await self._save_data(user_id, data)
        if not had_backup:
            await self._safe_record_analytics(
                event_name="skey.enabled",
                user_id=user_id,
                source="server",
            )
        return {
            "stored": True,
            "recovery_backup_available": True,
        }

    async def fetch_recovery_key(self, user_id: str, device_id: str) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"

        data = await self._load_data(user_id)
        backup = data.get(_RECOVERY_BACKUP_KEY)
        if not isinstance(backup, dict):
            return {"available": False}

        try:
            recovery_key = self._decrypt_recovery_backup(user_id, backup)
        except Exception:
            logger.warning(
                "Corrupt or legacy Setka recovery backup for %s; clearing server copy",
                user_id,
                exc_info=True,
            )
            data.pop(_RECOVERY_BACKUP_KEY, None)
            await self._save_data(user_id, data)
            return {"available": False}

        await self._record_skey_silent_success(user_id)
        await self._mark_verified(user_id, device_key, data)

        return {"available": True, "recovery_key": recovery_key}

    async def delete_recovery_key(self, user_id: str, device_id: str) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"

        data = await self._load_data(user_id)
        if _RECOVERY_BACKUP_KEY in data:
            data.pop(_RECOVERY_BACKUP_KEY, None)
            await self._save_data(user_id, data)
            await self._safe_record_analytics(
                event_name="skey.disabled",
                user_id=user_id,
                source="server",
            )
        return {
            "deleted": True,
            "recovery_backup_available": False,
        }

    async def complete_fallback(
        self,
        user_id: str,
        device_id: str,
        method: str,
        recovery_key: str | None = None,
    ) -> JsonDict:
        device_key = device_id.strip() if device_id else ""
        if not device_key:
            device_key = "_session"
        lowered = method.strip().lower()
        if lowered not in {"emoji", "recovery_key"}:
            raise SynapseError(400, "Unsupported fallback method", Codes.INVALID_PARAM)

        fallback_event = (
            "login.fallback.emoji"
            if lowered == "emoji"
            else "login.fallback.recovery_key"
        )
        await self._safe_record_analytics(
            event_name=fallback_event,
            user_id=user_id,
            source="server",
        )
        if lowered == "recovery_key":
            await self._record_skey_silent_success(user_id)

        data = await self._load_data(user_id)
        await self._mark_verified(user_id, device_key, data)

        cleaned_key = _clean_str(recovery_key, max_len=512) if recovery_key else None
        if cleaned_key:
            data = await self._load_data(user_id)
            data[_RECOVERY_BACKUP_KEY] = self._encrypt_recovery_backup(user_id, cleaned_key)
            await self._save_data(user_id, data)

        data = await self._load_data(user_id)
        return {
            "verified": True,
            "method": lowered,
            "recovery_backup_available": self._has_recovery_backup(data),
        }

    def _recovery_storage_materials(self) -> list[bytes]:
        materials: list[bytes] = []
        seen: set[bytes] = set()

        def add_material(raw: bytes) -> None:
            if raw and raw not in seen:
                seen.add(raw)
                materials.append(raw)

        env_secret = os.environ.get("SETKA_LOGIN_RECOVERY_SECRET", "").strip()
        if env_secret:
            add_material(env_secret.encode("utf-8"))

        registration_secret = self._hs.config.registration.registration_shared_secret
        if registration_secret:
            add_material(registration_secret.strip().encode("utf-8"))

        macaroon = self._hs.config.key.macaroon_secret_key
        if isinstance(macaroon, str):
            add_material(macaroon.encode("utf-8"))
        else:
            add_material(macaroon)

        return materials

    def _recovery_storage_secret(self) -> bytes:
        materials = self._recovery_storage_materials()
        if not materials:
            raise SynapseError(
                500,
                "Setka recovery encryption is not configured",
                Codes.UNKNOWN,
            )
        return hashlib.sha256(b"setka-login-recovery:" + materials[0]).digest()

    def _recovery_storage_key(self, user_id: str, *, secret: bytes | None = None) -> bytes:
        storage_secret = secret if secret is not None else self._recovery_storage_secret()
        return hashlib.sha256(storage_secret + user_id.encode("utf-8")).digest()

    def _encrypt_recovery_backup(self, user_id: str, recovery_key: str) -> JsonDict:
        key = self._recovery_storage_key(user_id)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, recovery_key.encode("utf-8"), None)
        return {
            "version": _RECOVERY_BACKUP_VERSION,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "updated_at": _now_ms(self._clock),
        }

    def _decrypt_recovery_backup(self, user_id: str, backup: JsonDict) -> str:
        version = int(backup.get("version", 0))
        if version != _RECOVERY_BACKUP_VERSION:
            raise ValueError("Unsupported recovery backup version")

        nonce_raw = backup.get("nonce")
        ciphertext_raw = backup.get("ciphertext")
        if not isinstance(nonce_raw, str) or not isinstance(ciphertext_raw, str):
            raise ValueError("Malformed recovery backup")

        nonce = base64.b64decode(nonce_raw.encode("ascii"))
        ciphertext = base64.b64decode(ciphertext_raw.encode("ascii"))

        for material in self._recovery_storage_materials():
            secret = hashlib.sha256(b"setka-login-recovery:" + material).digest()
            key = self._recovery_storage_key(user_id, secret=secret)
            try:
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
                return plaintext.decode("utf-8")
            except Exception:
                continue

        raise ValueError("Unable to decrypt recovery backup with configured secrets")

    def _has_recovery_backup(self, data: JsonDict) -> bool:
        backup = data.get(_RECOVERY_BACKUP_KEY)
        return isinstance(backup, dict) and bool(backup.get("ciphertext"))

    async def _is_device_verified(self, user_id: str, device_id: str) -> bool:
        data = await self._load_data(user_id)
        verified = data.get("verified_devices")
        return isinstance(verified, dict) and device_id in verified

    async def _ensure_challenge(
        self,
        user_id: str,
        device_id: str,
        data: JsonDict,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> JsonDict:
        ip = _as_str(ip)
        user_agent = _as_str(user_agent)

        challenges = data.setdefault("challenges", {})
        if not isinstance(challenges, dict):
            challenges = {}
            data["challenges"] = challenges

        now = _now_ms(self._clock)
        existing = challenges.get(device_id)
        if isinstance(existing, dict):
            expires_at = int(existing.get("expires_at", 0))
            if expires_at > now:
                if not existing.get("notice_sent"):
                    code = self._rotate_code(existing, now)
                    ip_line = ip or str(existing.get("ip") or "")
                    region = str(existing.get("region") or self._resolve_region(ip_line))
                    existing["ip"] = ip_line
                    existing["region"] = region
                    await self._save_data(user_id, data)
                    notice_sent = await self._send_notice(
                        user_id,
                        ip=ip_line,
                        region=region,
                        code=code,
                    )
                    existing["notice_sent"] = notice_sent
                    await self._deliver_code_email(
                        user_id,
                        code=code,
                        ip=ip_line,
                        region=region,
                        challenge=existing,
                    )
                    await self._save_data(user_id, data)
                return existing

        code = f"{secrets.randbelow(1_000_000):06d}"
        region = self._resolve_region(ip)
        challenge: JsonDict = {
            "code_hash": _hash_code(code),
            "expires_at": now + _CHALLENGE_TTL_MS,
            "attempts": 0,
            "ip": ip or "",
            "region": region,
            "created_at": now,
            "notice_sent": False,
        }
        if user_agent:
            challenge["user_agent"] = user_agent[:255]
        challenges[device_id] = challenge
        await self._save_data(user_id, data)

        notice_sent = await self._send_notice(
            user_id,
            ip=ip or "",
            region=region,
            code=code,
        )
        challenge["notice_sent"] = notice_sent
        await self._deliver_code_email(
            user_id,
            code=code,
            ip=ip or "",
            region=region,
            challenge=challenge,
        )
        await self._save_data(user_id, data)
        return challenge

    async def _deliver_code_email(
        self,
        user_id: str,
        *,
        code: str,
        ip: str,
        region: str,
        challenge: JsonDict,
    ) -> None:
        delivery_email = await self._resolve_delivery_email(user_id, None)
        if not delivery_email:
            challenge["email_sent"] = False
            return
        try:
            await self._send_email(
                delivery_email,
                code=code,
                ip=ip,
                region=region,
            )
            challenge["email_sent"] = True
        except Exception:
            logger.exception(
                "Failed to send Setka login verification email to %s for %s",
                delivery_email,
                user_id,
            )
            challenge["email_sent"] = False

    def _rotate_code(self, challenge: JsonDict, now: int) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge["code_hash"] = _hash_code(code)
        challenge["expires_at"] = now + _CHALLENGE_TTL_MS
        challenge["attempts"] = 0
        challenge["notice_sent"] = False
        return code

    async def _mark_verified(self, user_id: str, device_id: str, data: JsonDict) -> None:
        verified = data.setdefault("verified_devices", {})
        if not isinstance(verified, dict):
            verified = {}
            data["verified_devices"] = verified
        was_new = device_id not in verified
        verified[device_id] = _now_ms(self._clock)
        challenges = data.get("challenges")
        if isinstance(challenges, dict):
            challenges.pop(device_id, None)
        await self._save_data(user_id, data)
        if was_new:
            await self._safe_record_analytics(
                event_name="login.verified",
                user_id=user_id,
                source="server",
            )

    async def _safe_record_analytics(
        self,
        *,
        event_name: str,
        user_id: str | None = None,
        properties: JsonDict | None = None,
        source: str = "server",
    ) -> None:
        try:
            await self._hs.get_setka_analytics_handler().record_event(
                event_name=event_name,
                user_id=user_id,
                properties=properties,
                source=source,
            )
        except Exception:
            logger.warning(
                "Setka analytics skipped for %s (user=%s)",
                event_name,
                user_id,
                exc_info=True,
            )

    async def _record_skey_silent_success(self, user_id: str) -> None:
        await self._safe_record_analytics(
            event_name="login.skey.silent_success",
            user_id=user_id,
            source="server",
        )

    async def _send_notice(
        self,
        user_id: str,
        *,
        ip: str,
        region: str,
        code: str | None = None,
    ) -> bool:
        if not self._server_notices_manager.is_enabled():
            logger.warning(
                "Server notices disabled; login notice for %s was not delivered",
                user_id,
            )
            return False

        ip_line = ip if ip else "неизвестен"
        region_line = region if region else "неизвестен"
        code_line = code.strip() if code else ""
        body = (
            f"Новый вход в аккаунт.\n"
            f"IP-адрес: {ip_line}\n"
            f"Регион: {region_line}\n"
        )
        if code_line:
            body += f"Код подтверждения: {code_line}\n"
        body += "\nЕсли это были не вы, немедленно смените пароль."
        formatted_body = (
            "<p><strong>Новый вход в аккаунт</strong></p>"
            f"<p>IP-адрес: {ip_line}<br/>Регион: {region_line}</p>"
        )
        if code_line:
            formatted_body += (
                f"<p>Код подтверждения: <b>{code_line}</b></p>"
            )
        formatted_body += "<p>Если это были не вы, немедленно смените пароль.</p>"
        try:
            await self._server_notices_manager.send_notice(
                user_id,
                {
                    "msgtype": "m.notice",
                    "body": body,
                    "format": "org.matrix.custom.html",
                    "formatted_body": formatted_body,
                },
                type=EventTypes.Message,
            )
            return True
        except Exception:
            logger.exception("Failed to send Setka login verification notice to %s", user_id)
            return False

    async def _send_email(
        self,
        email_address: str,
        *,
        code: str,
        ip: str,
        region: str,
    ) -> None:
        ip_line = ip if ip else "неизвестен"
        region_line = region if region else "неизвестен"
        subject = "Код подтверждения входа"
        text = (
            f"Новый вход в ваш аккаунт Setka.\n"
            f"Ваш код: {code}\n"
            f"IP-адрес: {ip_line}\n"
            f"Регион: {region_line}\n\n"
            "Если это были не вы, немедленно смените пароль."
        )
        html = (
            "<p><strong>Новый вход в ваш аккаунт Setka</strong></p>"
            f"<p style=\"font-size: 24px; letter-spacing: 2px;\"><b>{code}</b></p>"
            f"<p>IP-адрес: {ip_line}<br/>Регион: {region_line}</p>"
            "<p>Если это были не вы, немедленно смените пароль.</p>"
        )
        await self._send_email_handler.send_email(
            email_address=email_address,
            subject=subject,
            app_name="Setka",
            html=html,
            text=text,
        )

    async def _resolve_delivery_email(
        self, user_id: str, email: str | None
    ) -> str | None:
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

    def _resolve_region(self, ip: str | None) -> str:
        ip = _as_str(ip)
        if not ip:
            return "Неизвестно"
        if ip.startswith("127.") or ip == "::1":
            return "Локальная сеть"
        return "Неизвестно"

    async def _load_data(self, user_id: str) -> JsonDict:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_LOGIN
        )
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    async def _save_data(self, user_id: str, data: JsonDict) -> None:
        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_LOGIN, data
        )
