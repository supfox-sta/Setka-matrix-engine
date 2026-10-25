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
"""Handler for Setka Plus subscriptions, payments and sticker packs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import random
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Mapping
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from twisted.web.http_headers import Headers

from synapse.api.constants import AccountDataTypes
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from synapse.server import HomeServer


_SUBSCRIPTION_STATUS = {"inactive", "pending", "active", "expired", "canceled"}
_PACK_KIND = {"sticker", "emoji"}
_PAYMENT_MODES = {"acquiring_and_vouchers", "vouchers_only"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _clean_str(value: object, *, max_len: int = 256) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:max_len]


def _clean_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None
    return None


def _clean_timestamp_ms(value: object, *, allow_zero: bool = False) -> int | None:
    timestamp = _clean_int(value)
    if timestamp is None:
        return None
    if timestamp == 0:
        return 0 if allow_zero else None
    if timestamp < 0:
        return None
    # If a seconds-based value is supplied, convert it to milliseconds.
    if timestamp < 10_000_000_000:
        return timestamp * 1000
    return timestamp


def _clean_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        amount = float(value)
    elif isinstance(value, str):
        try:
            amount = float(value)
        except ValueError:
            return None
    else:
        return None
    return amount if amount > 0 else None


def _clean_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


class SetkaPlusHandler:
    """Stores all Setka Plus metadata in account_data."""

    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._account_data_handler = hs.get_account_data_handler()
        self._http_client = hs.get_proxied_http_client()

        self._plus_price_rub = _env_float("SETKA_PLUS_PRICE_RUB", 299.0)
        self._plus_duration_days = _env_int("SETKA_PLUS_DURATION_DAYS", 30)
        self._yoomoney_token = os.environ.get("SETKA_PLUS_YOOMONEY_OAUTH_TOKEN", "").strip()
        self._yoomoney_receiver = os.environ.get(
            "SETKA_PLUS_YOOMONEY_RECEIVER",
            "4100117689566171",
        ).strip()
        self._yoomoney_return_url = os.environ.get("SETKA_PLUS_YOOMONEY_RETURN_URL", "").strip()
        self._yoomoney_notification_secret = os.environ.get("SETKA_PLUS_YOOMONEY_NOTIFICATION_SECRET", "").strip()

        self._robokassa_merchant_login = os.environ.get(
            "SETKA_PLUS_ROBOKASSA_MERCHANT_LOGIN",
            "setkamatrix",
        ).strip()
        self._robokassa_password1 = os.environ.get("SETKA_PLUS_ROBOKASSA_PASSWORD1", "").strip()
        self._robokassa_password2 = os.environ.get("SETKA_PLUS_ROBOKASSA_PASSWORD2", "").strip()
        self._robokassa_test_password1 = os.environ.get(
            "SETKA_PLUS_ROBOKASSA_TEST_PASSWORD1",
            "",
        ).strip()
        self._robokassa_test_password2 = os.environ.get(
            "SETKA_PLUS_ROBOKASSA_TEST_PASSWORD2",
            "",
        ).strip()
        self._robokassa_is_test = os.environ.get("SETKA_PLUS_ROBOKASSA_IS_TEST", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._robokassa_payment_url = os.environ.get(
            "SETKA_PLUS_ROBOKASSA_PAYMENT_URL",
            "https://auth.robokassa.ru/Merchant/Index.aspx",
        ).strip()
        self._robokassa_callback_base_url = os.environ.get(
            "SETKA_PLUS_ROBOKASSA_CALLBACK_BASE_URL",
            "https://matrix.setka-matrix.ru/setka/robokassa",
        ).strip().rstrip("/")

        self._payanyway_mnt_id = os.environ.get("SETKA_PLUS_PAYANYWAY_MNT_ID", "").strip()
        self._payanyway_integrity_code = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_INTEGRITY_CODE", ""
        ).strip()
        self._payanyway_is_test = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_IS_TEST", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._payanyway_payment_url = self._normalize_payanyway_assistant_url(
            os.environ.get(
                "SETKA_PLUS_PAYANYWAY_PAYMENT_URL",
                "https://www.payanyway.ru/assistant.htm",
            )
        )
        self._payanyway_callback_base_url = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_CALLBACK_BASE_URL",
            "https://matrix.setka-matrix.ru/setka/payanyway",
        ).strip().rstrip("/")
        self._payanyway_api_username = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_API_USERNAME", ""
        ).strip()
        self._payanyway_api_password = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_API_PASSWORD", ""
        ).strip()
        self._payanyway_api_payer = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_API_PAYER", "364"
        ).strip() or "364"
        self._payanyway_api_service_url = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_API_SERVICE_URL",
            "https://service.moneta.ru/services",
        ).strip()
        self._payanyway_simpla_self = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_SIMPLA_SELF", "1"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._payanyway_override_urls = os.environ.get(
            "SETKA_PLUS_PAYANYWAY_OVERRIDE_URLS", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # payment_id -> user_id while checkout is pending (Pay URL fallback).
        self._payanyway_pending_users: dict[str, str] = {}

        configured_plans_file = os.environ.get("SETKA_PLUS_PLANS_FILE", "").strip()
        self._plans_file = configured_plans_file or os.path.join(
            tempfile.gettempdir(),
            "setka_plus_plans.json",
        )
        self._plans_cache: list[JsonDict] | None = None

        self._share_link_prefix = (
            os.environ.get(
                "SETKA_PLUS_SHARE_LINK_PREFIX",
                "https://matrix.setka-matrix.ru/setka-pack/",
            ).strip()
        )
        if not self._share_link_prefix:
            self._share_link_prefix = "https://matrix.setka-matrix.ru/setka-pack/"

        share_secret = os.environ.get("SETKA_PLUS_SHARE_SECRET", "").strip()
        if share_secret:
            self._share_secret = share_secret.encode("utf-8")
        else:
            # Stable secret from Synapse config.
            self._share_secret = hs.config.key.macaroon_secret_key

        share_ttl_days = _env_int("SETKA_PLUS_SHARE_TOKEN_TTL_DAYS", 90)
        self._share_token_ttl_ms = share_ttl_days * 24 * 60 * 60 * 1000

        self._support_email = os.environ.get(
            "SETKA_PLUS_SUPPORT_EMAIL",
            "support@setka-matrix.ru",
        ).strip() or "support@setka-matrix.ru"

        config_dir = os.path.dirname(self._plans_file) or tempfile.gettempdir()
        self._server_config_file = os.environ.get(
            "SETKA_PLUS_SERVER_CONFIG_FILE",
            os.path.join(config_dir, "setka_server_config.json"),
        ).strip()
        self._payment_mode = "acquiring_and_vouchers"
        self._voucher_footer_text = ""
        self._voucher_tables_ready = False
        self._load_and_apply_server_config()

    def get_support_email(self) -> str:
        return self._support_email

    def get_server_config(self) -> JsonDict:
        return {
            "robokassa_merchant_login": self._robokassa_merchant_login,
            "robokassa_password1_set": bool(self._robokassa_password1),
            "robokassa_password2_set": bool(self._robokassa_password2),
            "robokassa_test_password1_set": bool(self._robokassa_test_password1),
            "robokassa_test_password2_set": bool(self._robokassa_test_password2),
            "robokassa_is_test": self._robokassa_is_test,
            "robokassa_callback_base_url": self._robokassa_callback_base_url,
            "robokassa_payment_url": self._robokassa_payment_url,
            "payanyway_mnt_id": self._payanyway_mnt_id,
            "payanyway_integrity_code_set": bool(self._payanyway_integrity_code),
            "payanyway_is_test": self._payanyway_is_test,
            "payanyway_callback_base_url": self._payanyway_callback_base_url,
            "payanyway_payment_url": self._payanyway_payment_url,
            "payanyway_api_username_set": bool(self._payanyway_api_username),
            "payanyway_api_password_set": bool(self._payanyway_api_password),
            "payanyway_api_payer": self._payanyway_api_payer,
            "plus_price_rub": round(self._plus_price_rub, 2),
            "plus_duration_days": self._plus_duration_days,
            "share_link_prefix": self._share_link_prefix,
            "support_email": self._support_email,
            "yoomoney_receiver": self._yoomoney_receiver,
            "yoomoney_return_url": self._yoomoney_return_url,
            "yoomoney_token_set": bool(self._yoomoney_token),
            "yoomoney_notification_secret_set": bool(self._yoomoney_notification_secret),
            "payment_mode": self._payment_mode,
            "voucher_footer_text": self._voucher_footer_text,
            "config_file": self._server_config_file,
        }

    def get_client_checkout_config(self) -> JsonDict:
        checkout_enabled = self._payment_mode == "acquiring_and_vouchers"
        return {
            "payment_mode": self._payment_mode,
            "checkout_enabled": checkout_enabled,
            "vouchers_enabled": True,
            "voucher_footer_text": self._voucher_footer_text,
        }

    def _assert_acquiring_enabled(self) -> None:
        if self._payment_mode == "vouchers_only":
            raise SynapseError(
                403,
                "Online checkout is disabled; use a voucher code",
                Codes.FORBIDDEN,
            )

    async def set_server_config(self, updates: JsonDict) -> JsonDict:
        if not isinstance(updates, dict):
            raise SynapseError(400, "Expected JSON object", Codes.BAD_JSON)

        stored = self._read_server_config_file()
        merged = dict(stored)

        string_fields = {
            "robokassa_merchant_login",
            "robokassa_callback_base_url",
            "robokassa_payment_url",
            "payanyway_mnt_id",
            "payanyway_callback_base_url",
            "payanyway_payment_url",
            "share_link_prefix",
            "support_email",
            "yoomoney_receiver",
            "yoomoney_return_url",
        }
        secret_fields = {
            "robokassa_password1",
            "robokassa_password2",
            "robokassa_test_password1",
            "robokassa_test_password2",
            "payanyway_integrity_code",
            "yoomoney_token",
            "yoomoney_notification_secret",
        }

        for key in string_fields:
            if key not in updates:
                continue
            value = updates.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise SynapseError(400, f"{key} must be a string", Codes.INVALID_PARAM)
            cleaned = value.strip()
            if cleaned:
                merged[key] = cleaned

        for key in secret_fields:
            if key not in updates:
                continue
            value = updates.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise SynapseError(400, f"{key} must be a string", Codes.INVALID_PARAM)
            cleaned = value.strip()
            if cleaned:
                merged[key] = cleaned

        if "robokassa_is_test" in updates:
            merged["robokassa_is_test"] = _clean_bool(updates.get("robokassa_is_test"), default=False)

        if "payanyway_is_test" in updates:
            merged["payanyway_is_test"] = _clean_bool(
                updates.get("payanyway_is_test"), default=False
            )

        if "plus_price_rub" in updates:
            amount = _clean_float(updates.get("plus_price_rub"))
            if amount is not None and amount > 0:
                merged["plus_price_rub"] = round(amount, 2)

        if "plus_duration_days" in updates:
            days = _clean_int(updates.get("plus_duration_days"))
            if days is not None and days > 0:
                merged["plus_duration_days"] = days

        if "payment_mode" in updates:
            mode = _clean_str(updates.get("payment_mode"), max_len=64)
            if mode in _PAYMENT_MODES:
                merged["payment_mode"] = mode

        if "voucher_footer_text" in updates:
            value = updates.get("voucher_footer_text")
            if value is None:
                merged.pop("voucher_footer_text", None)
            elif not isinstance(value, str):
                raise SynapseError(
                    400, "voucher_footer_text must be a string", Codes.INVALID_PARAM
                )
            else:
                cleaned = value.strip()[:2000]
                if cleaned:
                    merged["voucher_footer_text"] = cleaned
                else:
                    merged.pop("voucher_footer_text", None)

        self._persist_server_config_file(merged)
        self._load_and_apply_server_config()
        return self.get_server_config()

    def _read_server_config_file(self) -> JsonDict:
        if not self._server_config_file:
            return {}
        try:
            with open(self._server_config_file, "r", encoding="utf-8") as stream:
                parsed = json.load(stream)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _persist_server_config_file(self, config: JsonDict) -> None:
        directory = os.path.dirname(self._server_config_file)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                return
        try:
            with open(self._server_config_file, "w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_and_apply_server_config(self) -> None:
        config = self._read_server_config_file()
        if not config:
            return

        merchant = _clean_str(config.get("robokassa_merchant_login"), max_len=128)
        if merchant:
            self._robokassa_merchant_login = merchant

        for attr, key in (
            ("_robokassa_password1", "robokassa_password1"),
            ("_robokassa_password2", "robokassa_password2"),
            ("_robokassa_test_password1", "robokassa_test_password1"),
            ("_robokassa_test_password2", "robokassa_test_password2"),
            ("_yoomoney_token", "yoomoney_token"),
            ("_yoomoney_notification_secret", "yoomoney_notification_secret"),
        ):
            value = _clean_str(config.get(key), max_len=512)
            if value:
                setattr(self, attr, value)

        callback_base = _clean_str(config.get("robokassa_callback_base_url"), max_len=512)
        if callback_base:
            self._robokassa_callback_base_url = callback_base.rstrip("/")

        payment_url = _clean_str(config.get("robokassa_payment_url"), max_len=512)
        if payment_url:
            self._robokassa_payment_url = payment_url

        if "robokassa_is_test" in config:
            self._robokassa_is_test = _clean_bool(
                config.get("robokassa_is_test"), default=self._robokassa_is_test
            )

        mnt_id = _clean_str(config.get("payanyway_mnt_id"), max_len=64)
        if mnt_id:
            self._payanyway_mnt_id = mnt_id

        integrity = _clean_str(config.get("payanyway_integrity_code"), max_len=512)
        if integrity:
            self._payanyway_integrity_code = integrity

        paw_callback = _clean_str(config.get("payanyway_callback_base_url"), max_len=512)
        if paw_callback:
            self._payanyway_callback_base_url = paw_callback.rstrip("/")

        paw_payment_url = _clean_str(config.get("payanyway_payment_url"), max_len=512)
        if paw_payment_url:
            self._payanyway_payment_url = self._normalize_payanyway_assistant_url(
                paw_payment_url
            )

        if "payanyway_is_test" in config:
            self._payanyway_is_test = _clean_bool(
                config.get("payanyway_is_test"), default=self._payanyway_is_test
            )

        api_username = _clean_str(config.get("payanyway_api_username"), max_len=128)
        if api_username:
            self._payanyway_api_username = api_username

        api_password = _clean_str(config.get("payanyway_api_password"), max_len=512)
        if api_password:
            self._payanyway_api_password = api_password

        api_payer = _clean_str(config.get("payanyway_api_payer"), max_len=64)
        if api_payer:
            self._payanyway_api_payer = api_payer

        api_service_url = _clean_str(config.get("payanyway_api_service_url"), max_len=512)
        if api_service_url:
            self._payanyway_api_service_url = api_service_url

        price = _clean_float(config.get("plus_price_rub"))
        if price is not None and price > 0:
            self._plus_price_rub = round(price, 2)

        duration = _clean_int(config.get("plus_duration_days"))
        if duration is not None and duration > 0:
            self._plus_duration_days = duration

        share_prefix = _clean_str(config.get("share_link_prefix"), max_len=512)
        if share_prefix:
            self._share_link_prefix = share_prefix

        support_email = _clean_str(config.get("support_email"), max_len=255)
        if support_email:
            self._support_email = support_email

        receiver = _clean_str(config.get("yoomoney_receiver"), max_len=64)
        if receiver:
            self._yoomoney_receiver = receiver

        return_url = _clean_str(config.get("yoomoney_return_url"), max_len=512)
        if return_url:
            self._yoomoney_return_url = return_url

        mode = _clean_str(config.get("payment_mode"), max_len=64)
        if mode in _PAYMENT_MODES:
            self._payment_mode = mode

        footer = config.get("voucher_footer_text")
        if isinstance(footer, str):
            self._voucher_footer_text = footer.strip()[:2000]
        else:
            self._voucher_footer_text = ""

    async def _ensure_voucher_tables(self) -> None:
        if self._voucher_tables_ready:
            return

        def _txn(txn: object) -> None:
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_plus_vouchers (
                    code TEXT NOT NULL PRIMARY KEY,
                    plan_id TEXT,
                    duration_days INTEGER,
                    expires_at BIGINT NOT NULL,
                    max_redemptions INTEGER NOT NULL DEFAULT 1,
                    redemption_count INTEGER NOT NULL DEFAULT 0,
                    max_per_user INTEGER NOT NULL DEFAULT 1,
                    note TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at BIGINT NOT NULL
                )
                """
            )
            txn.execute(
                """
                CREATE TABLE IF NOT EXISTS setka_plus_voucher_redemptions (
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    redeemed_at BIGINT NOT NULL,
                    PRIMARY KEY (code, user_id)
                )
                """
            )

        await self._store.db_pool.runInteraction("setka_plus_voucher_create_tables", _txn)
        self._voucher_tables_ready = True

    @staticmethod
    def _normalize_voucher_code(code: object) -> str | None:
        if not isinstance(code, str):
            return None
        normalized = code.strip().upper().replace(" ", "")
        if not normalized or len(normalized) > 64:
            return None
        if not all(ch.isalnum() or ch == "-" for ch in normalized):
            return None
        return normalized

    def _voucher_row_to_dict(self, row: tuple[object, ...]) -> JsonDict:
        return {
            "code": str(row[0]),
            "plan_id": row[1],
            "duration_days": row[2],
            "expires_at": int(row[3]),
            "max_redemptions": int(row[4]),
            "redemption_count": int(row[5]),
            "max_per_user": int(row[6]),
            "note": row[7],
            "is_active": bool(row[8]),
            "created_at": int(row[9]),
        }

    async def list_vouchers(self) -> list[JsonDict]:
        await self._ensure_voucher_tables()

        def _txn(txn: object) -> list[JsonDict]:
            txn.execute(
                """
                SELECT code, plan_id, duration_days, expires_at, max_redemptions,
                       redemption_count, max_per_user, note, is_active, created_at
                FROM setka_plus_vouchers
                ORDER BY created_at DESC
                """
            )
            return [self._voucher_row_to_dict(row) for row in txn.fetchall()]

        return await self._store.db_pool.runInteraction("setka_plus_list_vouchers", _txn)

    async def get_voucher(self, code: str) -> JsonDict:
        await self._ensure_voucher_tables()
        normalized = self._normalize_voucher_code(code)
        if not normalized:
            raise SynapseError(400, "Invalid voucher code", Codes.INVALID_PARAM)

        def _txn(txn: object) -> JsonDict | None:
            txn.execute(
                """
                SELECT code, plan_id, duration_days, expires_at, max_redemptions,
                       redemption_count, max_per_user, note, is_active, created_at
                FROM setka_plus_vouchers WHERE code = ?
                """,
                (normalized,),
            )
            row = txn.fetchone()
            return self._voucher_row_to_dict(row) if row else None

        voucher = await self._store.db_pool.runInteraction("setka_plus_get_voucher", _txn)
        if voucher is None:
            raise SynapseError(404, "Voucher not found", Codes.NOT_FOUND)
        return voucher

    async def create_voucher(self, body: JsonDict) -> JsonDict:
        await self._ensure_voucher_tables()
        if not isinstance(body, dict):
            raise SynapseError(400, "Expected JSON object", Codes.BAD_JSON)

        code = self._normalize_voucher_code(body.get("code") or "")
        if not code:
            code = f"SETKA-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"

        plan_id = _clean_str(body.get("plan_id"), max_len=64)
        duration_days = _clean_int(body.get("duration_days"))
        if not plan_id and (duration_days is None or duration_days <= 0):
            raise SynapseError(
                400,
                "plan_id or duration_days is required",
                Codes.MISSING_PARAM,
            )
        if plan_id and self._find_plan_by_id(plan_id) is None:
            raise SynapseError(400, "Unknown plan_id", Codes.INVALID_PARAM)

        expires_at = _clean_timestamp_ms(body.get("expires_at"))
        if expires_at is None or expires_at <= _now_ms():
            raise SynapseError(400, "expires_at must be in the future", Codes.INVALID_PARAM)

        max_redemptions = _clean_int(body.get("max_redemptions")) or 1
        max_per_user = _clean_int(body.get("max_per_user")) or 1
        if max_redemptions <= 0 or max_per_user <= 0:
            raise SynapseError(400, "Invalid redemption limits", Codes.INVALID_PARAM)

        note = _clean_str(body.get("note"), max_len=512)
        is_active = _clean_bool(body.get("is_active"), default=True)
        created_at = _now_ms()

        def _txn(txn: object) -> None:
            txn.execute(
                """
                INSERT INTO setka_plus_vouchers (
                    code, plan_id, duration_days, expires_at, max_redemptions,
                    redemption_count, max_per_user, note, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    code,
                    plan_id,
                    duration_days,
                    expires_at,
                    max_redemptions,
                    max_per_user,
                    note,
                    1 if is_active else 0,
                    created_at,
                ),
            )

        try:
            await self._store.db_pool.runInteraction("setka_plus_create_voucher", _txn)
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                raise SynapseError(409, "Voucher code already exists", Codes.NOT_FOUND) from exc
            raise

        return await self.get_voucher(code)

    async def update_voucher(self, code: str, body: JsonDict) -> JsonDict:
        await self._ensure_voucher_tables()
        existing = await self.get_voucher(code)
        normalized = str(existing["code"])

        plan_id = existing.get("plan_id")
        duration_days = existing.get("duration_days")
        expires_at = existing.get("expires_at")
        max_redemptions = existing.get("max_redemptions")
        max_per_user = existing.get("max_per_user")
        note = existing.get("note")
        is_active = existing.get("is_active")

        if "plan_id" in body:
            plan_id = _clean_str(body.get("plan_id"), max_len=64)
            if plan_id and self._find_plan_by_id(plan_id) is None:
                raise SynapseError(400, "Unknown plan_id", Codes.INVALID_PARAM)

        if "duration_days" in body:
            duration_days = _clean_int(body.get("duration_days"))

        if "expires_at" in body:
            parsed = _clean_timestamp_ms(body.get("expires_at"))
            if parsed is None:
                raise SynapseError(400, "Invalid expires_at", Codes.INVALID_PARAM)
            expires_at = parsed

        if "max_redemptions" in body:
            parsed = _clean_int(body.get("max_redemptions"))
            if parsed is None or parsed <= 0:
                raise SynapseError(400, "Invalid max_redemptions", Codes.INVALID_PARAM)
            max_redemptions = parsed

        if "max_per_user" in body:
            parsed = _clean_int(body.get("max_per_user"))
            if parsed is None or parsed <= 0:
                raise SynapseError(400, "Invalid max_per_user", Codes.INVALID_PARAM)
            max_per_user = parsed

        if "note" in body:
            note = _clean_str(body.get("note"), max_len=512)

        if "is_active" in body:
            is_active = _clean_bool(body.get("is_active"), default=True)

        if not plan_id and (duration_days is None or int(duration_days or 0) <= 0):
            raise SynapseError(
                400,
                "plan_id or duration_days is required",
                Codes.MISSING_PARAM,
            )

        def _txn(txn: object) -> None:
            txn.execute(
                """
                UPDATE setka_plus_vouchers
                SET plan_id = ?, duration_days = ?, expires_at = ?, max_redemptions = ?,
                    max_per_user = ?, note = ?, is_active = ?
                WHERE code = ?
                """,
                (
                    plan_id,
                    duration_days,
                    expires_at,
                    max_redemptions,
                    max_per_user,
                    note,
                    1 if is_active else 0,
                    normalized,
                ),
            )

        await self._store.db_pool.runInteraction("setka_plus_update_voucher", _txn)
        return await self.get_voucher(normalized)

    async def delete_voucher(self, code: str) -> JsonDict:
        await self._ensure_voucher_tables()
        existing = await self.get_voucher(code)

        def _txn(txn: object) -> None:
            txn.execute(
                "UPDATE setka_plus_vouchers SET is_active = 0 WHERE code = ?",
                (str(existing["code"]),),
            )

        await self._store.db_pool.runInteraction("setka_plus_delete_voucher", _txn)
        return {"code": existing["code"], "deleted": True}

    async def get_voucher_redemptions(self, code: str) -> list[JsonDict]:
        await self._ensure_voucher_tables()
        voucher = await self.get_voucher(code)

        def _txn(txn: object) -> list[JsonDict]:
            txn.execute(
                """
                SELECT user_id, redeemed_at
                FROM setka_plus_voucher_redemptions
                WHERE code = ?
                ORDER BY redeemed_at DESC
                """,
                (str(voucher["code"]),),
            )
            return [
                {"user_id": str(row[0]), "redeemed_at": int(row[1])}
                for row in txn.fetchall()
            ]

        return await self._store.db_pool.runInteraction(
            "setka_plus_voucher_redemptions",
            _txn,
        )

    async def redeem_voucher(self, user_id: str, code: str) -> JsonDict:
        await self._ensure_voucher_tables()
        normalized = self._normalize_voucher_code(code)
        if not normalized:
            raise SynapseError(400, "Недействительный промокод", Codes.INVALID_PARAM)

        now = _now_ms()

        def _reserve(txn: object) -> JsonDict:
            txn.execute(
                """
                SELECT code, plan_id, duration_days, expires_at, max_redemptions,
                       redemption_count, max_per_user, note, is_active, created_at
                FROM setka_plus_vouchers
                WHERE code = ?
                """,
                (normalized,),
            )
            row = txn.fetchone()
            if row is None:
                raise SynapseError(404, "Промокод не найден", Codes.NOT_FOUND)
            voucher = self._voucher_row_to_dict(row)
            if not voucher.get("is_active"):
                raise SynapseError(403, "Промокод недействителен", Codes.FORBIDDEN)
            if int(voucher["expires_at"]) <= now:
                raise SynapseError(403, "Срок действия промокода истёк", Codes.FORBIDDEN)
            if int(voucher["redemption_count"]) >= int(voucher["max_redemptions"]):
                raise SynapseError(403, "Промокод уже использован максимальное число раз", Codes.FORBIDDEN)

            txn.execute(
                """
                SELECT COUNT(*) FROM setka_plus_voucher_redemptions
                WHERE code = ? AND user_id = ?
                """,
                (normalized, user_id),
            )
            user_count_row = txn.fetchone()
            user_count = int(user_count_row[0]) if user_count_row else 0
            if user_count >= int(voucher["max_per_user"]):
                raise SynapseError(403, "Вы уже активировали этот промокод", Codes.FORBIDDEN)

            txn.execute(
                """
                INSERT INTO setka_plus_voucher_redemptions (code, user_id, redeemed_at)
                VALUES (?, ?, ?)
                """,
                (normalized, user_id, now),
            )
            txn.execute(
                """
                UPDATE setka_plus_vouchers
                SET redemption_count = redemption_count + 1
                WHERE code = ?
                """,
                (normalized,),
            )
            return voucher

        voucher = await self._store.db_pool.runInteraction(
            "setka_plus_redeem_voucher",
            _reserve,
        )

        plan_id = _clean_str(voucher.get("plan_id"), max_len=64)
        duration_days = _clean_int(voucher.get("duration_days"))
        if duration_days is None or duration_days <= 0:
            if plan_id:
                plan = self._find_plan_by_id(plan_id)
                duration_days = _clean_int(plan.get("duration_days") if plan else None)
            if duration_days is None or duration_days <= 0:
                duration_days = self._plus_duration_days

        subscription = await self.activate_subscription(
            user_id,
            payment_id=f"voucher:{normalized}",
            payment_provider="voucher",
            amount=0.0,
            plan_id=plan_id,
            duration_days=duration_days,
            currency="RUB",
        )
        await self._safe_record_analytics(
            event_name="plus.voucher_redeemed",
            user_id=user_id,
            source="server",
            properties={"voucher_code": normalized, "plan_id": plan_id},
        )
        return {"voucher_code": normalized, "subscription": subscription}

    async def get_subscription(self, user_id: str) -> JsonDict:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_SUBSCRIPTION
        )
        return self._normalize_subscription(raw)

    async def set_subscription(self, user_id: str, updates: JsonDict) -> JsonDict:
        existing = await self.get_subscription(user_id)
        was_active = existing.get("is_active") is True
        payload = dict(existing)

        status = _clean_str(updates.get("status"), max_len=32)
        if status:
            lowered = status.lower()
            if lowered not in _SUBSCRIPTION_STATUS:
                raise SynapseError(400, f"Unsupported status: {status}", Codes.INVALID_PARAM)
            payload["status"] = lowered

        tier = _clean_str(updates.get("tier"), max_len=64)
        if tier:
            payload["tier"] = tier
            plan = self._find_plan_by_id(tier)
            if plan:
                payload["plan_name"] = plan.get("name")
                payload["duration_days"] = plan.get("duration_days")
                payload["price_rub"] = plan.get("price_rub")

        started_at = _clean_timestamp_ms(updates.get("started_at"), allow_zero=True)
        if started_at is not None:
            payload["started_at"] = started_at

        expires_at = _clean_timestamp_ms(updates.get("expires_at"), allow_zero=True)
        if expires_at is not None:
            payload["expires_at"] = expires_at

        last_payment_id = _clean_str(updates.get("last_payment_id"), max_len=255)
        if last_payment_id:
            payload["last_payment_id"] = last_payment_id

        payment_provider = _clean_str(updates.get("payment_provider"), max_len=64)
        if payment_provider:
            payload["payment_provider"] = payment_provider

        amount = _clean_float(updates.get("amount"))
        if amount is not None:
            payload["amount"] = round(amount, 2)

        currency = _clean_str(updates.get("currency"), max_len=16)
        if currency:
            payload["currency"] = currency

        payload["updated_at"] = _now_ms()
        payload = self._normalize_subscription(payload)

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_SUBSCRIPTION, payload
        )
        if payload.get("is_active") and not was_active:
            await self._safe_record_analytics(
                event_name="plus.subscription_activated",
                user_id=user_id,
                source="server",
                properties={
                    "provider": str(payload.get("payment_provider") or "admin"),
                    "plan_id": str(payload.get("tier") or "setka_plus"),
                },
            )
        return payload

    async def get_plans(self) -> list[JsonDict]:
        return [dict(plan) for plan in self._load_plans()]

    async def set_plans(self, plans: object) -> list[JsonDict]:
        normalized = self._normalize_plans(plans)
        self._plans_cache = [dict(plan) for plan in normalized]
        self._persist_plans_file(normalized)
        return [dict(plan) for plan in normalized]

    async def activate_subscription(
        self,
        user_id: str,
        *,
        payment_id: str,
        payment_provider: str,
        amount: float | None = None,
        plan_id: str | None = None,
        duration_days: int | None = None,
        currency: str = "RUB",
    ) -> JsonDict:
        plan = self._resolve_plan(plan_id)
        effective_duration_days = (
            duration_days if isinstance(duration_days, int) and duration_days > 0 else int(plan.get("duration_days", 30))
        )
        effective_amount = (
            round(float(amount), 2)
            if amount is not None and amount > 0
            else round(float(plan.get("price_rub", self._plus_price_rub)), 2)
        )

        now = _now_ms()
        existing = await self.get_subscription(user_id)
        previous_expiry = _clean_timestamp_ms(existing.get("expires_at"), allow_zero=True) or 0
        start = previous_expiry if previous_expiry > now else now
        expires_at = start + effective_duration_days * 24 * 60 * 60 * 1000

        payload: JsonDict = {
            "tier": str(plan.get("id", "setka_plus")),
            "plan_name": str(plan.get("name", "Setka Plus")),
            "status": "active",
            "started_at": existing.get("started_at") or now,
            "expires_at": expires_at,
            "last_payment_id": payment_id,
            "payment_provider": payment_provider,
            "amount": effective_amount,
            "currency": currency,
            "duration_days": effective_duration_days,
            "price_rub": effective_amount,
            "updated_at": now,
        }

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_SUBSCRIPTION, payload
        )
        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "provider": payment_provider,
                "status": "success",
                "amount": effective_amount,
                "currency": currency,
                "plan_id": str(plan.get("id", "setka_plus")),
                "created_at": now,
            },
        )
        await self._safe_record_analytics(
            event_name="plus.subscription_activated",
            user_id=user_id,
            source="server",
            properties={
                "provider": payment_provider,
                "plan_id": str(plan.get("id", "setka_plus")),
            },
        )
        return self._normalize_subscription(payload)

    async def get_sticker_packs(self, user_id: str) -> list[JsonDict]:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_STICKER_PACKS
        )
        if not isinstance(raw, dict):
            return []
        return self._normalize_sticker_packs(raw.get("packs"))

    async def upsert_sticker_pack(
        self,
        user_id: str,
        pack_id: str,
        content: JsonDict,
        *,
        allow_emoji_import: bool = False,
        admin_override: bool = False,
    ) -> JsonDict:
        normalized_pack_id = _clean_str(pack_id, max_len=128)
        if not normalized_pack_id:
            raise SynapseError(400, "pack_id is required", Codes.INVALID_PARAM)

        packs = await self.get_sticker_packs(user_id)
        existing = next((pack for pack in packs if pack.get("id") == normalized_pack_id), None)

        pack_name = _clean_str(content.get("name"), max_len=120)
        if not pack_name and not existing:
            raise SynapseError(400, "Sticker pack name is required", Codes.MISSING_PARAM)

        pack_kind = _clean_str(content.get("kind"), max_len=24)
        if pack_kind:
            pack_kind = pack_kind.lower()
        if pack_kind not in _PACK_KIND:
            existing_kind = _clean_str(existing.get("kind") if isinstance(existing, dict) else None, max_len=24)
            pack_kind = existing_kind.lower() if existing_kind and existing_kind.lower() in _PACK_KIND else "sticker"

        if pack_kind == "emoji" and not admin_override:
            if not allow_emoji_import:
                raise SynapseError(
                    403,
                    "Emoji pack management is not available.",
                    Codes.FORBIDDEN,
                )
            await self._assert_plus_active(user_id)

        stickers = content.get("stickers")
        normalized_stickers = (
            self._normalize_stickers(stickers, pack_kind=pack_kind)
            if stickers is not None
            else (existing.get("stickers", []) if existing else [])
        )

        now = _now_ms()
        updated_pack: JsonDict = {
            "id": normalized_pack_id,
            "name": pack_name or existing.get("name"),
            "kind": pack_kind,
            "stickers": normalized_stickers,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }

        next_packs = [pack for pack in packs if pack.get("id") != normalized_pack_id]
        next_packs.append(updated_pack)
        next_packs.sort(key=lambda pack: str(pack.get("name", "")).lower())

        await self._account_data_handler.add_account_data_for_user(
            user_id,
            AccountDataTypes.SETKA_PLUS_STICKER_PACKS,
            {"packs": next_packs},
        )

        return updated_pack

    async def delete_sticker_pack(
        self, user_id: str, pack_id: str, *, admin_override: bool = False
    ) -> None:
        packs = await self.get_sticker_packs(user_id)
        existing = next((pack for pack in packs if pack.get("id") == pack_id), None)
        if (
            not admin_override
            and isinstance(existing, dict)
            and str(existing.get("kind", "sticker")).lower() == "emoji"
        ):
            raise SynapseError(
                403,
                "Emoji pack management is not available.",
                Codes.FORBIDDEN,
            )
        next_packs = [pack for pack in packs if pack.get("id") != pack_id]
        if len(next_packs) == len(packs):
            return
        if next_packs:
            await self._account_data_handler.add_account_data_for_user(
                user_id,
                AccountDataTypes.SETKA_PLUS_STICKER_PACKS,
                {"packs": next_packs},
            )
        else:
            await self._account_data_handler.remove_account_data_for_user(
                user_id, AccountDataTypes.SETKA_PLUS_STICKER_PACKS
            )

    async def _assert_plus_active(self, user_id: str) -> None:
        subscription = await self.get_subscription(user_id)
        if subscription.get("is_active") is not True:
            raise SynapseError(403, "Setka Plus subscription is required.", Codes.FORBIDDEN)

    def build_share_url(self, token: str) -> str:
        return f"{self._share_link_prefix}{token}"

    async def create_share_token(
        self, user_id: str, pack_id: str, *, admin_override: bool = False
    ) -> str:
        normalized_pack_id = _clean_str(pack_id, max_len=128)
        if not normalized_pack_id:
            raise SynapseError(400, "pack_id is required", Codes.INVALID_PARAM)

        packs = await self.get_sticker_packs(user_id)
        pack = next(
            (
                p
                for p in packs
                if isinstance(p, dict) and p.get("id") == normalized_pack_id
            ),
            None,
        )
        if not pack:
            raise SynapseError(404, "Sticker pack not found", Codes.NOT_FOUND)

        if not admin_override and str(pack.get("kind", "sticker")).lower() == "emoji":
            raise SynapseError(
                403,
                "Emoji pack sharing is not available.",
                Codes.FORBIDDEN,
            )

        stickers = pack.get("stickers", [])
        preview_url = None
        if isinstance(stickers, list) and stickers:
            first = stickers[0]
            if isinstance(first, dict):
                preview_url = _clean_str(first.get("mxc_url") or first.get("mxcUrl"), max_len=1024)

        payload: JsonDict = {
            "v": 1,
            "u": user_id,
            "p": normalized_pack_id,
            "iat": _now_ms(),
            "n": _clean_str(pack.get("name"), max_len=120),
            "k": _clean_str(pack.get("kind"), max_len=24),
            "i": preview_url,
        }
        payload_b64 = self._b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        sig = hmac.new(self._share_secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = self._b64encode(sig)
        return f"v1.{payload_b64}.{sig_b64}"

    async def resolve_shared_pack(self, token: str) -> JsonDict:
        payload = self._decode_share_token(token)

        iat = _clean_timestamp_ms(payload.get("iat")) or 0
        if iat and self._share_token_ttl_ms > 0 and _now_ms() - iat > self._share_token_ttl_ms:
            raise SynapseError(404, "Shared pack link has expired", Codes.NOT_FOUND)

        owner_user_id = _clean_str(payload.get("u"), max_len=255)
        pack_id = _clean_str(payload.get("p"), max_len=128)
        if not owner_user_id or not pack_id:
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)

        packs = await self.get_sticker_packs(owner_user_id)
        pack = next((p for p in packs if isinstance(p, dict) and p.get("id") == pack_id), None)
        if not pack:
            raise SynapseError(404, "Sticker pack not found", Codes.NOT_FOUND)

        result = dict(pack)
        result["owner_user_id"] = owner_user_id
        return result

    async def import_shared_pack(self, user_id: str, token: str) -> JsonDict:
        shared_pack = await self.resolve_shared_pack(token)
        pack_kind = str(shared_pack.get("kind", "sticker")).lower()
        allow_emoji_import = pack_kind == "emoji"
        if allow_emoji_import:
            await self._assert_plus_active(user_id)
        new_pack_id = uuid.uuid4().hex
        payload: JsonDict = {
            "name": shared_pack.get("name"),
            "kind": shared_pack.get("kind"),
            "stickers": shared_pack.get("stickers", []),
        }
        return await self.upsert_sticker_pack(
            user_id,
            new_pack_id,
            payload,
            allow_emoji_import=allow_emoji_import,
        )

    def _decode_share_token(self, token: str) -> JsonDict:
        if not isinstance(token, str) or not token.strip():
            raise SynapseError(400, "token is required", Codes.MISSING_PARAM)
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)

        payload_b64 = parts[1]
        sig_b64 = parts[2]
        if not payload_b64 or not sig_b64:
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)

        expected_sig = hmac.new(self._share_secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        supplied_sig = self._b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, supplied_sig):
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)

        payload_raw = self._b64decode(payload_b64)
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except Exception:
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)
        if not isinstance(payload, dict):
            raise SynapseError(400, "Invalid shared pack token", Codes.INVALID_PARAM)
        return payload

    @staticmethod
    def _b64encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        raw = data.encode("ascii")
        padding = b"=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding)

    async def get_payments(self, user_id: str) -> list[JsonDict]:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_PAYMENTS
        )
        if not isinstance(raw, dict):
            return []
        normalized: list[JsonDict] = []
        raw_payments = raw.get("payments")
        if not isinstance(raw_payments, list):
            return []
        for item in raw_payments:
            if not isinstance(item, dict):
                continue
            payment_id = _clean_str(item.get("payment_id"), max_len=255)
            status = _clean_str(item.get("status"), max_len=32)
            provider = _clean_str(item.get("provider"), max_len=64)
            if not payment_id or not status:
                continue
            created_at = _clean_timestamp_ms(item.get("created_at")) or _now_ms()
            normalized_item: JsonDict = {
                "payment_id": payment_id,
                "status": status.lower(),
                "provider": provider or "unknown",
                "created_at": created_at,
            }
            amount = _clean_float(item.get("amount"))
            if amount is not None:
                normalized_item["amount"] = round(amount, 2)
            currency = _clean_str(item.get("currency"), max_len=16)
            if currency:
                normalized_item["currency"] = currency
            request_id = _clean_str(item.get("request_id"), max_len=255)
            if request_id:
                normalized_item["request_id"] = request_id
            label = _clean_str(item.get("label"), max_len=255)
            if label:
                normalized_item["label"] = label
            plan_id = _clean_str(item.get("plan_id"), max_len=64)
            if plan_id:
                normalized_item["plan_id"] = plan_id
            message = _clean_str(item.get("message"), max_len=255)
            if message:
                normalized_item["message"] = message
            assistant_fields = item.get("assistant_fields")
            if isinstance(assistant_fields, dict):
                cleaned_fields = self._clean_assistant_fields(assistant_fields)
                if cleaned_fields:
                    normalized_item["assistant_fields"] = cleaned_fields
            normalized.append(normalized_item)
        normalized.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
        return normalized[:100]

    async def get_payment_status(self, user_id: str, payment_id: str) -> JsonDict | None:
        payments = await self.get_payments(user_id)
        for item in payments:
            if item.get("payment_id") != payment_id:
                continue
            status = str(item.get("status", "pending")).lower()
            if status == "success":
                status = "paid"
            subscription = await self.get_subscription(user_id)
            response: JsonDict = {
                "payment_id": payment_id,
                "status": status,
                "subscription_active": subscription.get("is_active") is True,
            }
            plan_id = item.get("plan_id")
            if isinstance(plan_id, str) and plan_id:
                response["plan_id"] = plan_id
            message = _clean_str(item.get("message"), max_len=255)
            if message:
                response["message"] = message
            return response
        return None

    async def find_payment_by_inv_id(self, user_id: str, inv_id: str) -> JsonDict | None:
        payments = await self.get_payments(user_id)
        for item in payments:
            if str(item.get("request_id", "")) == inv_id:
                return item
        return None

    async def mark_payment_failed(self, user_id: str, payment_id: str, message: str) -> None:
        payments = await self.get_payments(user_id)
        for item in payments:
            if item.get("payment_id") != payment_id:
                continue
            await self.record_payment(
                user_id,
                {
                    **item,
                    "payment_id": payment_id,
                    "status": "failed",
                    "message": message[:255],
                },
            )
            return

    def build_robokassa_return_url(self, user_id: str, payment_id: str, *, success: bool) -> str:
        segment = "success" if success else "fail"
        return (
            f"{self._robokassa_callback_base_url}/return/{segment}?"
            + urlencode({"user_id": user_id, "payment_id": payment_id})
        )

    async def record_payment(self, user_id: str, payment: JsonDict) -> None:
        current = await self.get_payments(user_id)
        payment_id = _clean_str(payment.get("payment_id"), max_len=255)
        status = _clean_str(payment.get("status"), max_len=32)
        if not payment_id or not status:
            raise SynapseError(400, "payment_id and status are required", Codes.INVALID_PARAM)

        existing = next(
            (item for item in current if item.get("payment_id") == payment_id),
            None,
        )

        entry: JsonDict = {
            "payment_id": payment_id,
            "status": status.lower(),
            "provider": _clean_str(payment.get("provider"), max_len=64) or "unknown",
            "created_at": _clean_timestamp_ms(payment.get("created_at")) or _now_ms(),
        }

        amount = _clean_float(payment.get("amount"))
        if amount is not None:
            entry["amount"] = round(amount, 2)
        currency = _clean_str(payment.get("currency"), max_len=16)
        if currency:
            entry["currency"] = currency
        request_id = _clean_str(payment.get("request_id"), max_len=255)
        if request_id:
            entry["request_id"] = request_id
        label = _clean_str(payment.get("label"), max_len=255)
        if label:
            entry["label"] = label
        plan_id = _clean_str(payment.get("plan_id"), max_len=64)
        if plan_id:
            entry["plan_id"] = plan_id
        message = _clean_str(payment.get("message"), max_len=255)
        if message:
            entry["message"] = message

        assistant_fields = payment.get("assistant_fields")
        if isinstance(assistant_fields, dict):
            cleaned_fields = self._clean_assistant_fields(assistant_fields)
            if cleaned_fields:
                entry["assistant_fields"] = cleaned_fields
        elif isinstance(existing, dict):
            previous_fields = existing.get("assistant_fields")
            if isinstance(previous_fields, dict) and previous_fields:
                entry["assistant_fields"] = previous_fields

        current = [item for item in current if item.get("payment_id") != payment_id]
        current.insert(0, entry)
        current = current[:100]

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_PAYMENTS, {"payments": current}
        )

        if entry.get("provider") == "payanyway":
            normalized_status = str(entry.get("status", "")).lower()
            if normalized_status == "pending":
                self._payanyway_pending_users[payment_id] = user_id
            elif normalized_status in {"paid", "failed", "canceled"}:
                self._payanyway_pending_users.pop(payment_id, None)

        normalized_status = str(entry.get("status", "")).lower()
        if normalized_status in {"success", "paid"}:
            await self._safe_record_analytics(
                event_name="plus.payment_paid",
                user_id=user_id,
                source="server",
                properties={
                    "provider": str(entry.get("provider", "unknown")),
                    "payment_id": payment_id,
                },
            )
        elif normalized_status in {"failed", "canceled", "cancelled"}:
            await self._safe_record_analytics(
                event_name="plus.payment_failed",
                user_id=user_id,
                source="server",
                properties={
                    "provider": str(entry.get("provider", "unknown")),
                    "payment_id": payment_id,
                    "status": normalized_status,
                },
            )
        elif normalized_status in {"pending", "created"}:
            await self._safe_record_analytics(
                event_name="plus.payment_created",
                user_id=user_id,
                source="server",
                properties={
                    "provider": str(entry.get("provider", "unknown")),
                    "payment_id": payment_id,
                },
            )

    async def _safe_record_analytics(
        self,
        *,
        event_name: str,
        user_id: str | None = None,
        platform: str | None = None,
        app_version: str | None = None,
        device_id: str | None = None,
        properties: JsonDict | None = None,
        source: str = "server",
        ts: int | None = None,
    ) -> None:
        """Best-effort analytics — must never break Plus payments."""
        try:
            await self._hs.get_setka_analytics_handler().record_event(
                event_name=event_name,
                user_id=user_id,
                platform=platform,
                app_version=app_version,
                device_id=device_id,
                properties=properties,
                source=source,
                ts=ts,
            )
        except Exception:
            logger.warning(
                "Setka analytics skipped for %s (user=%s)",
                event_name,
                user_id,
                exc_info=True,
            )

    async def _record_checkout_started(
        self,
        user_id: str,
        *,
        provider: str,
        plan_id: str,
        payment_id: str,
    ) -> None:
        await self._safe_record_analytics(
            event_name="plus.checkout_started",
            user_id=user_id,
            source="server",
            properties={
                "provider": provider,
                "plan_id": plan_id,
                "payment_id": payment_id,
            },
        )

    async def create_yoomoney_payment_request(
        self,
        user_id: str,
        *,
        amount: float | None = None,
        description: str | None = None,
        plan_id: str | None = None,
    ) -> JsonDict:
        self._assert_acquiring_enabled()
        plan = self._resolve_plan(plan_id)
        selected_plan_id = str(plan.get("id", "setka_plus"))
        selected_plan_name = str(plan.get("name", "Setka Plus"))
        final_amount = round(
            amount
            if amount and amount > 0
            else float(plan.get("price_rub", self._plus_price_rub)),
            2,
        )
        label = f"setka_plus|{user_id}|{selected_plan_id}|{uuid.uuid4().hex}"
        comment = description or f"{selected_plan_name} subscription"
        payment_id = f"setka_plus_{uuid.uuid4().hex}"
        now = _now_ms()

        checkout_url = self._build_quickpay_url(final_amount, label, comment)
        request_id: str | None = None
        status = "created"

        if self._yoomoney_token and self._yoomoney_receiver:
            body = {
                "pattern_id": "p2p",
                "to": self._yoomoney_receiver,
                "amount": f"{final_amount:.2f}",
                "comment": comment,
                "message": "Setka Plus",
                "label": label,
            }
            headers = {
                b"Authorization": [f"Bearer {self._yoomoney_token}".encode("utf-8")],
                b"Content-Type": [b"application/x-www-form-urlencoded"],
            }
            try:
                response = await self._http_client.post_urlencoded_get_json(
                    "https://yoomoney.ru/api/request-payment",
                    body,
                    headers=headers,
                )
                if isinstance(response, dict):
                    request_id = _clean_str(response.get("request_id"), max_len=255)
                    response_status = _clean_str(response.get("status"), max_len=32)
                    if response_status:
                        status = response_status.lower()
            except Exception:
                status = "pending_external"

        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "request_id": request_id or payment_id,
                "provider": "yoomoney",
                "status": "pending",
                "amount": final_amount,
                "currency": "RUB",
                "label": label,
                "plan_id": selected_plan_id,
                "created_at": now,
            },
        )
        await self.set_subscription(
            user_id,
            {
                "status": "pending",
                "tier": selected_plan_id,
                "amount": final_amount,
            },
        )

        await self._record_checkout_started(
            user_id,
            provider="yoomoney",
            plan_id=selected_plan_id,
            payment_id=payment_id,
        )
        return {
            "payment_id": payment_id,
            "request_id": request_id,
            "provider": "yoomoney",
            "status": status,
            "amount": final_amount,
            "currency": "RUB",
            "label": label,
            "plan_id": selected_plan_id,
            "plan_name": selected_plan_name,
            "checkout_url": checkout_url,
            "return_url": self._yoomoney_return_url or None,
        }

    async def process_yoomoney_payment(
        self,
        user_id: str,
        *,
        request_id: str,
        money_source: str = "wallet",
        plan_id: str | None = None,
    ) -> JsonDict:
        if not self._yoomoney_token:
            raise SynapseError(
                500,
                "SETKA_PLUS_YOOMONEY_OAUTH_TOKEN is not configured",
                Codes.UNKNOWN,
            )

        normalized_request_id = _clean_str(request_id, max_len=255)
        if not normalized_request_id:
            raise SynapseError(400, "request_id is required", Codes.MISSING_PARAM)

        body = {
            "request_id": normalized_request_id,
            "money_source": money_source,
        }
        headers = {
            b"Authorization": [f"Bearer {self._yoomoney_token}".encode("utf-8")],
            b"Content-Type": [b"application/x-www-form-urlencoded"],
        }

        response = await self._http_client.post_urlencoded_get_json(
            "https://yoomoney.ru/api/process-payment",
            body,
            headers=headers,
        )

        status = "unknown"
        payment_id = normalized_request_id
        if isinstance(response, dict):
            response_status = _clean_str(response.get("status"), max_len=32)
            if response_status:
                status = response_status.lower()
            response_payment_id = _clean_str(response.get("payment_id"), max_len=255)
            if response_payment_id:
                payment_id = response_payment_id

        selected_plan = self._resolve_plan(plan_id)
        if status == "success":
            subscription = await self.activate_subscription(
                user_id,
                payment_id=payment_id,
                payment_provider="yoomoney",
                amount=float(selected_plan.get("price_rub", self._plus_price_rub)),
                plan_id=str(selected_plan.get("id", "setka_plus")),
                duration_days=int(selected_plan.get("duration_days", self._plus_duration_days)),
            )
        else:
            await self.record_payment(
                user_id,
                {
                    "payment_id": payment_id,
                    "request_id": normalized_request_id,
                    "provider": "yoomoney",
                    "status": status,
                    "amount": float(selected_plan.get("price_rub", self._plus_price_rub)),
                    "currency": "RUB",
                    "plan_id": str(selected_plan.get("id", "setka_plus")),
                    "created_at": _now_ms(),
                },
            )
            subscription = await self.get_subscription(user_id)

        return {
            "status": status,
            "payment_id": payment_id,
            "request_id": normalized_request_id,
            "subscription": subscription,
            "raw": response if isinstance(response, dict) else {},
        }

    async def handle_yoomoney_webhook(self, payload: Mapping[str, str]) -> JsonDict:
        if self._yoomoney_notification_secret:
            provided_hash = payload.get("sha1_hash", "").strip().lower()
            expected_hash = self._calculate_yoomoney_webhook_hash(payload)
            if not provided_hash or provided_hash != expected_hash:
                raise SynapseError(403, "Invalid YooMoney webhook signature", Codes.FORBIDDEN)

        label = payload.get("label", "")
        parts = label.split("|")
        if len(parts) < 3 or parts[0] != "setka_plus":
            raise SynapseError(400, "Unsupported payment label", Codes.INVALID_PARAM)
        user_id = parts[1]
        plan_id = parts[2] if len(parts) > 3 else None

        payment_id = payload.get("operation_id") or payload.get("request_id") or uuid.uuid4().hex
        amount = _clean_float(payload.get("amount")) or self._plus_price_rub
        currency = payload.get("currency") or "RUB"

        subscription = await self.activate_subscription(
            user_id,
            payment_id=payment_id,
            payment_provider="yoomoney_webhook",
            amount=amount,
            plan_id=plan_id,
            currency=currency,
        )
        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "provider": "yoomoney_webhook",
                "status": "success",
                "amount": amount,
                "currency": currency,
                "label": label,
                "plan_id": plan_id or "setka_plus",
                "created_at": _now_ms(),
            },
        )
        return {"ok": True, "user_id": user_id, "subscription": subscription}

    async def create_robokassa_payment_request(
        self,
        user_id: str,
        *,
        amount: float | None = None,
        description: str | None = None,
        plan_id: str | None = None,
    ) -> JsonDict:
        self._assert_acquiring_enabled()
        password1, _ = self._robokassa_passwords(self._robokassa_is_test)
        if not self._robokassa_merchant_login or not password1:
            raise SynapseError(
                500,
                "SETKA_PLUS_ROBOKASSA_PASSWORD1 is not configured",
                Codes.UNKNOWN,
            )

        plan = self._resolve_plan(plan_id)
        selected_plan_id = str(plan.get("id", "setka_plus"))
        selected_plan_name = str(plan.get("name", "Setka Plus"))
        final_amount = round(
            amount
            if amount and amount > 0
            else float(plan.get("price_rub", self._plus_price_rub)),
            2,
        )
        comment = description or f"{selected_plan_name} subscription"
        payment_id = f"setka_plus_{uuid.uuid4().hex}"
        inv_id = random.randint(1, 2147483647)
        now = _now_ms()
        out_sum = f"{final_amount:.2f}"
        shp = {
            "plan": selected_plan_id[:64],
            "uid": self._encode_robokassa_uid(user_id),
            "pid": payment_id[:64],
        }
        signature = self._robokassa_payment_signature(
            self._robokassa_merchant_login,
            out_sum,
            inv_id,
            password1,
            shp,
        )
        callback_base = self._robokassa_callback_base_url

        query: dict[str, str] = {
            "MerchantLogin": self._robokassa_merchant_login,
            "OutSum": out_sum,
            "InvId": str(inv_id),
            "Description": comment[:100],
            "SignatureValue": signature,
            "Culture": "ru",
            "Encoding": "utf-8",
            "Shp_plan": shp["plan"],
            "Shp_uid": shp["uid"],
            "Shp_pid": shp["pid"],
            "ResultURL": f"{callback_base}/result",
            "SuccessURL": f"{callback_base}/return/success",
            "FailURL": f"{callback_base}/return/fail",
        }
        if self._robokassa_is_test:
            query["IsTest"] = "1"

        checkout_url = self._robokassa_payment_url + "?" + urlencode(query)

        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "request_id": str(inv_id),
                "provider": "robokassa",
                "status": "pending",
                "amount": final_amount,
                "currency": "RUB",
                "label": f"robokassa:{inv_id}",
                "plan_id": selected_plan_id,
                "created_at": now,
            },
        )
        await self.set_subscription(
            user_id,
            {
                "status": "pending",
                "tier": selected_plan_id,
                "amount": final_amount,
            },
        )

        await self._record_checkout_started(
            user_id,
            provider="robokassa",
            plan_id=selected_plan_id,
            payment_id=payment_id,
        )
        return {
            "payment_id": payment_id,
            "request_id": str(inv_id),
            "provider": "robokassa",
            "status": "created",
            "amount": final_amount,
            "currency": "RUB",
            "label": f"robokassa:{inv_id}",
            "plan_id": selected_plan_id,
            "plan_name": selected_plan_name,
            "checkout_url": checkout_url,
            "return_url": "https://setka-matrix.ru/",
        }

    async def handle_robokassa_result(self, payload: Mapping[str, str]) -> str:
        out_sum = payload.get("OutSum", "").strip()
        inv_id = payload.get("InvId", "").strip()
        signature = payload.get("SignatureValue", "").strip()
        if not out_sum or not inv_id or not signature:
            raise SynapseError(400, "Missing Robokassa parameters", Codes.MISSING_PARAM)

        shp = self._extract_robokassa_shp(payload)
        uid = shp.get("uid")
        plan_id = shp.get("plan")
        if not uid:
            raise SynapseError(400, "Missing Robokassa user reference", Codes.MISSING_PARAM)

        is_test = payload.get("IsTest", "0").strip() == "1"
        _, password2 = self._robokassa_passwords(is_test)
        if not password2:
            _, password2 = self._robokassa_passwords(False)
        if not password2:
            raise SynapseError(
                500,
                "SETKA_PLUS_ROBOKASSA_PASSWORD2 is not configured",
                Codes.UNKNOWN,
            )

        expected = self._robokassa_result_signature(out_sum, inv_id, password2, shp)
        if signature.upper() != expected.upper():
            raise SynapseError(403, "Invalid Robokassa signature", Codes.FORBIDDEN)

        user_id = self._decode_robokassa_uid(uid)
        amount = _clean_float(out_sum) or self._plus_price_rub
        payment_id = shp.get("pid", "").strip()
        if not payment_id:
            pending = await self.find_payment_by_inv_id(user_id, inv_id)
            if pending:
                payment_id = str(pending.get("payment_id", ""))
        if not payment_id:
            payment_id = f"robokassa_{inv_id}"

        subscription = await self.activate_subscription(
            user_id,
            payment_id=payment_id,
            payment_provider="robokassa",
            amount=amount,
            plan_id=plan_id,
            currency="RUB",
        )
        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "request_id": inv_id,
                "provider": "robokassa",
                "status": "paid",
                "amount": amount,
                "currency": "RUB",
                "label": f"robokassa:{inv_id}",
                "plan_id": plan_id or "setka_plus",
                "created_at": _now_ms(),
            },
        )
        return inv_id

    async def resolve_robokassa_browser_return(
        self, payload: Mapping[str, str], *, success: bool
    ) -> tuple[str, str]:
        """Returns (user_id, payment_id) for redirect to the app return page."""
        inv_id = payload.get("InvId", "").strip()
        shp = self._extract_robokassa_shp(payload)
        uid = shp.get("uid", "").strip()
        payment_id = shp.get("pid", "").strip()

        if success:
            out_sum = payload.get("OutSum", "").strip()
            signature = payload.get("SignatureValue", "").strip()
            if out_sum and inv_id and signature:
                is_test = payload.get("IsTest", "0").strip() == "1"
                password1, _ = self._robokassa_passwords(is_test)
                if not password1:
                    password1, _ = self._robokassa_passwords(False)
                expected = self._robokassa_result_signature(out_sum, inv_id, password1, shp)
                if signature.upper() != expected.upper():
                    success = False

        user_id = self._decode_robokassa_uid(uid) if uid else ""
        if not payment_id and user_id and inv_id:
            pending = await self.find_payment_by_inv_id(user_id, inv_id)
            if pending:
                payment_id = str(pending.get("payment_id", ""))

        if not success and user_id and payment_id:
            await self.mark_payment_failed(user_id, payment_id, "Оплата отменена")

        if not user_id:
            user_id = ""
        if not payment_id:
            payment_id = f"robokassa_{inv_id}" if inv_id else ""
        return user_id, payment_id

    def _robokassa_passwords(self, is_test: bool) -> tuple[str, str]:
        if is_test and self._robokassa_test_password1 and self._robokassa_test_password2:
            return self._robokassa_test_password1, self._robokassa_test_password2
        return self._robokassa_password1, self._robokassa_password2

    @staticmethod
    def _robokassa_md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _robokassa_payment_signature(
        self,
        merchant_login: str,
        out_sum: str,
        inv_id: int,
        password1: str,
        shp: Mapping[str, str],
    ) -> str:
        parts = [merchant_login, out_sum, str(inv_id), password1]
        for key in sorted(shp.keys()):
            parts.append(f"Shp_{key}={shp[key]}")
        return self._robokassa_md5(":".join(parts))

    def _robokassa_result_signature(
        self,
        out_sum: str,
        inv_id: str,
        password2: str,
        shp: Mapping[str, str],
    ) -> str:
        parts = [out_sum, str(inv_id), password2]
        for key in sorted(shp.keys()):
            parts.append(f"Shp_{key}={shp[key]}")
        return self._robokassa_md5(":".join(parts))

    @staticmethod
    def _extract_robokassa_shp(payload: Mapping[str, str]) -> dict[str, str]:
        shp: dict[str, str] = {}
        for key, value in payload.items():
            if key.startswith("Shp_"):
                shp[key[4:]] = value
        return shp

    @staticmethod
    def _encode_robokassa_uid(user_id: str) -> str:
        return base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_robokassa_uid(encoded: str) -> str:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")

    async def create_payanyway_payment_request(
        self,
        user_id: str,
        *,
        amount: float | None = None,
        description: str | None = None,
        plan_id: str | None = None,
    ) -> JsonDict:
        self._assert_acquiring_enabled()
        if not self._payanyway_mnt_id:
            raise SynapseError(
                500,
                "SETKA_PLUS_PAYANYWAY_MNT_ID is not configured",
                Codes.UNKNOWN,
            )
        has_merchant_api = bool(
            self._payanyway_api_username and self._payanyway_api_password
        )
        if not self._payanyway_integrity_code and not has_merchant_api:
            raise SynapseError(
                500,
                "SETKA_PLUS_PAYANYWAY integrity code or Merchant API credentials required",
                Codes.UNKNOWN,
            )

        plan = self._resolve_plan(plan_id)
        selected_plan_id = str(plan.get("id", "setka_plus"))
        selected_plan_name = str(plan.get("name", "Setka Plus"))
        final_amount = round(
            amount
            if amount and amount > 0
            else float(plan.get("price_rub", self._plus_price_rub)),
            2,
        )
        comment = description or f"{selected_plan_name} subscription"
        payment_id = f"setka_plus_{uuid.uuid4().hex}"
        amount_str = f"{final_amount:.2f}"
        test_mode = "1" if self._payanyway_is_test else "0"
        encoded_user_id = self._encode_robokassa_uid(user_id)[:255]

        checkout_url: str | None = None
        payment_method = "assistant_link"
        operation_id: int | None = None

        if not self._payanyway_simpla_self:
            operation_id = await self._payanyway_create_invoice_operation(
                payment_id=payment_id,
                amount=final_amount,
                description=comment,
            )
            if operation_id is not None:
                checkout_url = self._payanyway_operation_checkout_url(operation_id)
                payment_method = "merchant_api"

        assistant_fields: dict[str, str] = {}
        checkout_form_html: str | None = None
        if checkout_url is None:
            assistant_fields = self._payanyway_assistant_fields(
                payment_id=payment_id,
                amount_str=amount_str,
                test_mode=test_mode,
                description=comment,
                plan_id=selected_plan_id,
                encoded_user_id=encoded_user_id,
                user_id=user_id,
            )
            checkout_url = self.build_payanyway_checkout_bridge_url(user_id, payment_id)
            checkout_form_html = self._payanyway_assistant_form_html(assistant_fields)
            payment_method = "assistant_post"

        now = _now_ms()

        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "request_id": payment_id,
                "provider": "payanyway",
                "status": "pending",
                "amount": final_amount,
                "currency": "RUB",
                "label": f"payanyway:{payment_id}",
                "plan_id": selected_plan_id,
                "created_at": now,
                **(
                    {"assistant_fields": assistant_fields}
                    if assistant_fields
                    else {}
                ),
            },
        )
        await self.set_subscription(
            user_id,
            {"status": "pending", "tier": selected_plan_id, "amount": final_amount},
        )

        await self._record_checkout_started(
            user_id,
            provider="payanyway",
            plan_id=selected_plan_id,
            payment_id=payment_id,
        )
        return {
            "payment_id": payment_id,
            "request_id": payment_id,
            "provider": "payanyway",
            "status": "created",
            "amount": final_amount,
            "currency": "RUB",
            "label": f"payanyway:{payment_id}",
            "plan_id": selected_plan_id,
            "plan_name": selected_plan_name,
            "checkout_url": checkout_url,
            "checkout_form_html": checkout_form_html,
            "payment_method": payment_method,
            "operation_id": operation_id,
            "return_url": self.build_payanyway_return_url(
                user_id, payment_id, success=True
            ),
        }

    def build_payanyway_checkout_bridge_url(self, user_id: str, payment_id: str) -> str:
        sig = self._payanyway_checkout_bridge_sig(user_id, payment_id)
        # Use /pay — already mounted on all Setka Synapse deployments (Moneta Pay URL).
        # /checkout is optional; older servers only expose /pay.
        return (
            f"{self._payanyway_callback_base_url}/pay?"
            + urlencode(
                {
                    "setka_checkout": "1",
                    "user_id": user_id,
                    "payment_id": payment_id,
                    "sig": sig,
                }
            )
        )

    def _payanyway_checkout_bridge_sig(self, user_id: str, payment_id: str) -> str:
        return self._payanyway_md5(
            user_id + payment_id + self._payanyway_integrity_code
        )

    async def render_payanyway_checkout_page(
        self, user_id: str, payment_id: str, sig: str
    ) -> str:
        if sig.lower() != self._payanyway_checkout_bridge_sig(user_id, payment_id).lower():
            raise SynapseError(403, "Invalid checkout signature", Codes.FORBIDDEN)

        payments = await self.get_payments(user_id)
        payment = next(
            (item for item in payments if item.get("payment_id") == payment_id),
            None,
        )
        if payment is None:
            raise SynapseError(404, "Payment not found", Codes.NOT_FOUND)

        if str(payment.get("status", "")).lower() not in {"", "pending", "created"}:
            raise SynapseError(409, "Payment is no longer pending", Codes.UNKNOWN)

        amount = _clean_float(payment.get("amount")) or self._plus_price_rub
        amount_str = f"{float(amount):.2f}"
        test_mode = "1" if self._payanyway_is_test else "0"
        plan_id = str(payment.get("plan_id", "setka_plus"))
        plan = self._resolve_plan(plan_id)
        description = str(plan.get("name", "Setka Plus")) + " subscription"
        encoded_user_id = self._encode_robokassa_uid(user_id)[:255]
        # Always rebuild — stored assistant_fields may carry an old signature/URL.
        fields = self._payanyway_assistant_fields(
            payment_id=payment_id,
            amount_str=amount_str,
            test_mode=test_mode,
            description=description,
            plan_id=plan_id,
            encoded_user_id=encoded_user_id,
            user_id=user_id,
        )
        logger.info(
            "PayAnyWay checkout bridge user=%s payment=%s mnt_id=%s amount=%s",
            user_id,
            payment_id,
            self._payanyway_mnt_id,
            amount_str,
        )

        return (
            "<!DOCTYPE html><html><head>"
            '<meta charset="utf-8"><title>Setka Plus</title></head><body>'
            "<p>Переход на оплату PayAnyWay…</p>"
            + self._payanyway_assistant_form_html(fields)
            + "</body></html>"
        )

    async def handle_payanyway_pay_url(self, payload: Mapping[str, str]) -> str:
        if not payload:
            return "SUCCESS"

        expected = self._payanyway_pay_signature(payload)
        if payload.get("MNT_SIGNATURE", "").lower() != expected.lower():
            raise SynapseError(403, "Invalid PayAnyWay signature", Codes.FORBIDDEN)

        payment_id = payload.get("MNT_TRANSACTION_ID", "").strip()
        user_id = self._payanyway_user_id_from_payload(payload)
        if not user_id and payment_id:
            user_id = self._payanyway_pending_users.get(payment_id, "")
        plan_id = payload.get("MNT_CUSTOM1", "").strip() or "setka_plus"
        amount = _clean_float(payload.get("MNT_AMOUNT", "")) or self._plus_price_rub

        if not payment_id or not user_id:
            return "FAIL"

        await self.activate_subscription(
            user_id,
            payment_id=payment_id,
            payment_provider="payanyway",
            amount=amount,
            plan_id=plan_id,
            currency="RUB",
        )
        await self.record_payment(
            user_id,
            {
                "payment_id": payment_id,
                "request_id": payment_id,
                "provider": "payanyway",
                "status": "paid",
                "amount": amount,
                "currency": "RUB",
                "label": f"payanyway:{payment_id}",
                "plan_id": plan_id,
                "created_at": _now_ms(),
            },
        )
        return "SUCCESS"

    async def handle_payanyway_check_url(self, payload: Mapping[str, str]) -> str:
        mnt_id = self._payanyway_mnt_id
        integrity = self._payanyway_integrity_code
        tx_id = payload.get("MNT_TRANSACTION_ID", "").strip()
        if not tx_id:
            return self._payanyway_check_xml(
                mnt_id, "", "500", "Missing transaction id", "0.00", integrity
            )

        if payload.get("MNT_COMMAND", "").upper() != "CHECK":
            return self._payanyway_check_xml(
                mnt_id, tx_id, "500", "Unsupported command", "0.00", integrity
            )

        expected = self._payanyway_check_signature(payload)
        if payload.get("MNT_SIGNATURE", "").lower() != expected.lower():
            return self._payanyway_check_xml(
                mnt_id, tx_id, "500", "Invalid signature", "0.00", integrity
            )

        user_id = self._payanyway_user_id_from_payload(payload)
        if not user_id:
            return self._payanyway_check_xml(
                mnt_id, tx_id, "500", "Unknown user", "0.00", integrity
            )

        status = await self.get_payment_status(user_id, tx_id)
        if status and status.get("status") == "paid":
            amount = f"{float(status.get('amount', self._plus_price_rub)):.2f}"
            return self._payanyway_check_xml(
                mnt_id, tx_id, "200", "Already paid", amount, integrity
            )

        amount_raw = payload.get("MNT_AMOUNT", "").strip()
        if amount_raw:
            amount = f"{float(amount_raw):.2f}"
        else:
            payments = await self.get_payments(user_id)
            pending_amount = self._plus_price_rub
            for item in payments:
                if str(item.get("payment_id", "")) == tx_id:
                    pending_amount = float(item.get("amount", self._plus_price_rub))
                    break
            amount = f"{pending_amount:.2f}"

        return self._payanyway_check_xml(
            mnt_id, tx_id, "402", "Ready for payment", amount, integrity
        )

    def build_payanyway_return_url(self, user_id: str, payment_id: str, *, success: bool) -> str:
        segment = "success" if success else "fail"
        return (
            f"{self._payanyway_callback_base_url}/return/{segment}?"
            + urlencode({"user_id": user_id, "payment_id": payment_id})
        )

    async def resolve_payanyway_browser_return(
        self, payload: Mapping[str, str], *, success: bool
    ) -> tuple[str, str]:
        payment_id = payload.get("MNT_TRANSACTION_ID", "").strip()
        if not payment_id:
            payment_id = payload.get("payment_id", "").strip()
        subscriber = payload.get("MNT_SUBSCRIBER_ID", "").strip()
        user_id = self._payanyway_user_id_from_payload(payload)

        if not success and user_id and payment_id:
            await self.mark_payment_failed(user_id, payment_id, "Оплата отменена")

        return user_id, payment_id

    def _payanyway_assistant_fields(
        self,
        *,
        payment_id: str,
        amount_str: str,
        test_mode: str,
        description: str,
        plan_id: str,
        encoded_user_id: str,
        user_id: str,
    ) -> dict[str, str]:
        # Simpla 2 / SELF PayAnyWay: MNT_TRANSACTION_ID = order id, optional subscriber empty.
        signature = self._payanyway_form_signature(
            transaction_id=payment_id,
            amount=amount_str,
            test_mode=test_mode,
        )
        fields: dict[str, str] = {
            "MNT_ID": self._payanyway_mnt_id,
            "MNT_TRANSACTION_ID": payment_id,
            "MNT_AMOUNT": amount_str,
            "MNT_CURRENCY_CODE": "RUB",
            "MNT_TEST_MODE": test_mode,
            "MNT_DESCRIPTION": description[:500],
            "MNT_SIGNATURE": signature,
        }
        # Simpla Assistant form: no MNT_CUSTOM* (Moneta reserves these fields).
        # Pay URL maps user via MNT_TRANSACTION_ID + in-memory pending index.
        if not self._payanyway_simpla_self:
            fields["MNT_CUSTOM1"] = plan_id[:32]
            fields["MNT_CUSTOM2"] = payment_id[:32]
            fields["MNT_CUSTOM3"] = encoded_user_id
        if self._payanyway_override_urls:
            fields["MNT_SUCCESS_URL"] = self.build_payanyway_return_url(
                user_id, payment_id, success=True
            )
            fields["MNT_FAIL_URL"] = self.build_payanyway_return_url(
                user_id, payment_id, success=False
            )
            fields["MNT_RETURN_URL"] = self.build_payanyway_return_url(
                user_id, payment_id, success=False
            )
        return fields

    def _payanyway_assistant_get_url(self, fields: Mapping[str, str]) -> str:
        return self._payanyway_payment_url + "?" + urlencode(fields, quote_via=quote)

    def _payanyway_assistant_form_html(self, fields: Mapping[str, str]) -> str:
        form_fields = dict(fields)
        if self._payanyway_simpla_self:
            form_fields = {
                key: value
                for key, value in form_fields.items()
                if not str(key).startswith("MNT_CUSTOM")
            }
        inputs = "".join(
            f'<input type="hidden" name="{html.escape(name)}" '
            f'value="{html.escape(value)}">'
            for name, value in form_fields.items()
        )
        action = html.escape(self._payanyway_payment_url, quote=True)
        return (
            f'<form id="payanyway" method="post" action="{action}">'
            f"{inputs}"
            "</form>"
            '<script>document.getElementById("payanyway").submit();</script>'
        )

    def _payanyway_operation_checkout_url(self, operation_id: int) -> str:
        base = self._payanyway_payment_url.split("?", 1)[0]
        if "assistant.widget" in base:
            return f"{base}?operationId={operation_id}"
        return f"{base}?operationId={operation_id}"

    @staticmethod
    def _payanyway_normalize_api_username(raw: str) -> str:
        trimmed = raw.strip()
        if not trimmed:
            return trimmed
        digits = "".join(ch for ch in trimmed if ch.isdigit())
        if trimmed.startswith("+") or digits.startswith("7") or digits.startswith("8"):
            if digits.startswith("8") and len(digits) >= 11:
                digits = "7" + digits[1:]
            if digits.startswith("7"):
                return digits
        return trimmed

    async def _payanyway_create_invoice_operation(
        self,
        *,
        payment_id: str,
        amount: float,
        description: str,
    ) -> int | None:
        username = self._payanyway_normalize_api_username(self._payanyway_api_username)
        password = self._payanyway_api_password
        if not username or not password:
            return None

        amount_str = f"{amount:.2f}"
        safe_description = xml_escape(description[:500])
        safe_client_tx = xml_escape(payment_id[:255])
        payer = xml_escape(self._payanyway_api_payer)
        payee = xml_escape(self._payanyway_mnt_id)
        safe_username = xml_escape(username)
        safe_password = xml_escape(password)

        envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:mes="http://www.moneta.ru/schemas/messages.xsd" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
  <soapenv:Header>
    <wsse:Security soapenv:mustUnderstand="1">
      <wsse:UsernameToken>
        <wsse:Username>{safe_username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{safe_password}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body>
    <mes:InvoiceRequest mes:version="VERSION_2">
      <mes:payer>{payer}</mes:payer>
      <mes:payee>{payee}</mes:payee>
      <mes:amount>{amount_str}</mes:amount>
      <mes:clientTransaction>{safe_client_tx}</mes:clientTransaction>
      <mes:description>{safe_description}</mes:description>
    </mes:InvoiceRequest>
  </soapenv:Body>
</soapenv:Envelope>"""

        service_url = self._payanyway_api_service_url
        if self._payanyway_is_test and "demo.moneta.ru" not in service_url:
            service_url = "https://demo.moneta.ru/services"

        headers = Headers(
            {
                b"Content-Type": [b"text/xml; charset=utf-8"],
                b"SOAPAction": [b'""'],
            }
        )
        try:
            from synapse.http.client import readBody
            from synapse.util.async_helpers import make_deferred_yieldable

            response = await self._http_client.request(
                "POST",
                service_url,
                data=envelope.encode("utf-8"),
                headers=headers,
            )
            body = await make_deferred_yieldable(readBody(response))
            if response.code >= 400:
                logger.warning(
                    "PayAnyWay InvoiceRequest HTTP %s for user %s: %s",
                    response.code,
                    username,
                    body[:1000].decode("utf-8", errors="replace"),
                )
                return None
            operation_id = self._payanyway_parse_invoice_transaction(body)
            if operation_id is None:
                logger.warning(
                    "PayAnyWay InvoiceRequest: no transaction id in response: %s",
                    body[:1000].decode("utf-8", errors="replace"),
                )
            return operation_id
        except Exception as error:
            logger.warning("PayAnyWay InvoiceRequest error: %s", error)
            return None

    @staticmethod
    def _payanyway_parse_invoice_transaction(body: bytes) -> int | None:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "transaction" and element.text:
                try:
                    return int(element.text.strip())
                except ValueError:
                    return None
        return None

    @staticmethod
    def _normalize_payanyway_assistant_url(raw: str) -> str:
        """Simpla POST target is www.payanyway.ru/assistant.htm — not moneta.ru."""
        trimmed = raw.strip()
        if not trimmed:
            return "https://www.payanyway.ru/assistant.htm"
        lower = trimmed.lower().rstrip("/")
        if lower in {
            "https://www.payanyway.ru",
            "http://www.payanyway.ru",
            "https://payanyway.ru",
            "http://payanyway.ru",
            "https://www.moneta.ru",
            "http://www.moneta.ru",
            "https://moneta.ru",
            "http://moneta.ru",
        }:
            return "https://www.payanyway.ru/assistant.htm"
        if "moneta.ru/assistant" in lower and "payanyway.ru" not in lower:
            return "https://www.payanyway.ru/assistant.htm"
        if "payanyway.ru" in lower and "assistant" not in lower:
            return trimmed.rstrip("/") + "/assistant.htm"
        return trimmed

    @staticmethod
    def _payanyway_md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    def _payanyway_form_signature(
        self,
        *,
        transaction_id: str,
        amount: str,
        test_mode: str,
        subscriber_id: str = "",
        currency: str = "RUB",
    ) -> str:
        return self._payanyway_md5(
            self._payanyway_mnt_id
            + transaction_id
            + amount
            + currency
            + subscriber_id
            + test_mode
            + self._payanyway_integrity_code
        )

    @staticmethod
    def _clean_assistant_fields(raw: Mapping[str, object]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            field_name = _clean_str(key, max_len=64)
            field_value = _clean_str(value, max_len=2048) if isinstance(value, str) else None
            if field_name and field_value is not None:
                cleaned[field_name] = field_value
        return cleaned

    def _payanyway_user_id_from_payload(self, payload: Mapping[str, str]) -> str:
        subscriber = payload.get("MNT_SUBSCRIBER_ID", "").strip()
        if subscriber:
            return self._decode_robokassa_uid(subscriber)
        custom = payload.get("MNT_CUSTOM3", "").strip()
        if custom:
            return self._decode_robokassa_uid(custom)
        return ""

    def _payanyway_pay_signature(self, payload: Mapping[str, str]) -> str:
        amount_raw = payload.get("MNT_AMOUNT", "").strip()
        amount_part = f"{float(amount_raw or 0):.2f}"
        parts = [
            payload.get("MNT_ID", ""),
            payload.get("MNT_TRANSACTION_ID", ""),
            payload.get("MNT_OPERATION_ID", ""),
            amount_part,
            payload.get("MNT_CURRENCY_CODE", ""),
            payload.get("MNT_SUBSCRIBER_ID", ""),
            payload.get("MNT_TEST_MODE", "0"),
            self._payanyway_integrity_code,
        ]
        return self._payanyway_md5("".join(parts))

    def _payanyway_check_signature(self, payload: Mapping[str, str]) -> str:
        amount_raw = payload.get("MNT_AMOUNT", "").strip()
        amount_part = f"{float(amount_raw):.2f}" if amount_raw else ""
        parts = [
            payload.get("MNT_COMMAND", ""),
            payload.get("MNT_ID", ""),
            payload.get("MNT_TRANSACTION_ID", ""),
            payload.get("MNT_OPERATION_ID", ""),
            amount_part,
            payload.get("MNT_CURRENCY_CODE", ""),
            payload.get("MNT_SUBSCRIBER_ID", ""),
            payload.get("MNT_TEST_MODE", "0"),
            self._payanyway_integrity_code,
        ]
        return self._payanyway_md5("".join(parts))

    @staticmethod
    def _payanyway_check_xml(
        mnt_id: str,
        tx_id: str,
        result_code: str,
        description: str,
        amount: str,
        integrity: str,
    ) -> str:
        signature = SetkaPlusHandler._payanyway_md5(result_code + mnt_id + tx_id + integrity)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<MNT_RESPONSE>"
            f"<MNT_ID>{mnt_id}</MNT_ID>"
            f"<MNT_TRANSACTION_ID>{tx_id}</MNT_TRANSACTION_ID>"
            f"<MNT_RESULT_CODE>{result_code}</MNT_RESULT_CODE>"
            f"<MNT_DESCRIPTION>{description}</MNT_DESCRIPTION>"
            f"<MNT_AMOUNT>{amount}</MNT_AMOUNT>"
            f"<MNT_SIGNATURE>{signature}</MNT_SIGNATURE>"
            "</MNT_RESPONSE>"
        )

    def _calculate_yoomoney_webhook_hash(self, payload: Mapping[str, str]) -> str:
        pieces = [
            payload.get("notification_type", ""),
            payload.get("operation_id", ""),
            payload.get("amount", ""),
            payload.get("currency", ""),
            payload.get("datetime", ""),
            payload.get("sender", ""),
            payload.get("codepro", ""),
            self._yoomoney_notification_secret,
            payload.get("label", ""),
        ]
        return hashlib.sha1("&".join(pieces).encode("utf-8")).hexdigest()

    def _build_quickpay_url(self, amount: float, label: str, description: str) -> str:
        query: dict[str, str] = {
            "receiver": self._yoomoney_receiver,
            "quickpay-form": "shop",
            "targets": description,
            "paymentType": "AC",
            "sum": f"{amount:.2f}",
            "label": label,
        }
        if self._yoomoney_return_url:
            query["successURL"] = self._yoomoney_return_url
        return "https://yoomoney.ru/quickpay/confirm.xml?" + urlencode(query)

    def _normalize_subscription(self, raw: object) -> JsonDict:
        now = _now_ms()
        default_plan = self._resolve_plan(None)
        if not isinstance(raw, dict):
            return {
                "tier": str(default_plan.get("id", "setka_plus")),
                "plan_name": str(default_plan.get("name", "Setka Plus")),
                "status": "inactive",
                "expires_at": 0,
                "started_at": 0,
                "updated_at": now,
                "is_active": False,
                "price_rub": float(default_plan.get("price_rub", self._plus_price_rub)),
                "duration_days": int(default_plan.get("duration_days", self._plus_duration_days)),
            }

        tier = _clean_str(raw.get("tier"), max_len=64) or str(default_plan.get("id", "setka_plus"))
        status = (_clean_str(raw.get("status"), max_len=32) or "inactive").lower()
        if status not in _SUBSCRIPTION_STATUS:
            status = "inactive"
        started_at = _clean_timestamp_ms(raw.get("started_at"), allow_zero=True) or 0
        expires_at = _clean_timestamp_ms(raw.get("expires_at"), allow_zero=True) or 0
        updated_at = _clean_timestamp_ms(raw.get("updated_at")) or now
        last_payment_id = _clean_str(raw.get("last_payment_id"), max_len=255)
        payment_provider = _clean_str(raw.get("payment_provider"), max_len=64)
        amount = _clean_float(raw.get("amount"))
        currency = _clean_str(raw.get("currency"), max_len=16) or "RUB"
        plan_name = _clean_str(raw.get("plan_name"), max_len=120)

        matching_plan = self._find_plan_by_id(tier)
        resolved_plan = matching_plan or default_plan
        duration_days = _clean_int(raw.get("duration_days"))
        if duration_days is None or duration_days <= 0:
            duration_days = int(resolved_plan.get("duration_days", self._plus_duration_days))
        price_rub = _clean_float(raw.get("price_rub"))
        if price_rub is None:
            price_rub = float(resolved_plan.get("price_rub", self._plus_price_rub))
        if not plan_name:
            plan_name = str(resolved_plan.get("name", "Setka Plus"))

        is_active = expires_at > now
        if status == "active" and not is_active:
            status = "expired"
        elif status in {"inactive", "expired", "canceled"} and is_active:
            status = "active"

        normalized: JsonDict = {
            "tier": tier,
            "status": status,
            "started_at": started_at,
            "expires_at": expires_at,
            "updated_at": updated_at,
            "is_active": is_active,
            "price_rub": round(price_rub, 2),
            "duration_days": duration_days,
            "plan_name": plan_name,
        }
        if last_payment_id:
            normalized["last_payment_id"] = last_payment_id
        if payment_provider:
            normalized["payment_provider"] = payment_provider
        if amount is not None:
            normalized["amount"] = round(amount, 2)
        if currency:
            normalized["currency"] = currency
        return normalized

    def _normalize_sticker_packs(self, raw: object) -> list[JsonDict]:
        if not isinstance(raw, list):
            return []

        normalized: list[JsonDict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            pack_id = _clean_str(item.get("id"), max_len=128)
            name = _clean_str(item.get("name"), max_len=120)
            if not pack_id or not name:
                continue
            raw_kind = _clean_str(item.get("kind"), max_len=24)
            pack_kind = raw_kind.lower() if raw_kind and raw_kind.lower() in _PACK_KIND else "sticker"
            stickers = self._normalize_stickers(item.get("stickers"), pack_kind=pack_kind)
            pack: JsonDict = {
                "id": pack_id,
                "name": name,
                "kind": pack_kind,
                "stickers": stickers,
                "created_at": _clean_timestamp_ms(item.get("created_at")) or _now_ms(),
                "updated_at": _clean_timestamp_ms(item.get("updated_at")) or _now_ms(),
            }
            normalized.append(pack)

        normalized.sort(key=lambda pack: str(pack.get("name", "")).lower())
        return normalized[:100]

    def _normalize_stickers(self, raw: object, *, pack_kind: str = "sticker") -> list[JsonDict]:
        if not isinstance(raw, list):
            return []
        normalized: list[JsonDict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue

            sticker_id = _clean_str(item.get("id"), max_len=128)
            if not sticker_id:
                sticker_id = uuid.uuid4().hex
            name = _clean_str(item.get("name"), max_len=120) or "Sticker"

            mxc_url = _clean_str(item.get("mxc_url"), max_len=2048)
            if not mxc_url:
                mxc_url = _clean_str(item.get("url"), max_len=2048)
            if not mxc_url or not mxc_url.startswith("mxc://"):
                continue

            mime_type = _clean_str(item.get("mime_type"), max_len=128)
            width = _clean_int(item.get("width"))
            height = _clean_int(item.get("height"))
            size = _clean_int(item.get("size"))

            if pack_kind == "emoji":
                width = 50
                height = 50

            sticker: JsonDict = {
                "id": sticker_id,
                "name": name,
                "mxc_url": mxc_url,
            }
            if mime_type:
                sticker["mime_type"] = mime_type
            if width is not None and width > 0:
                sticker["width"] = width
            if height is not None and height > 0:
                sticker["height"] = height
            if size is not None and size > 0:
                sticker["size"] = size

            normalized.append(sticker)

        return normalized[:512]

    def _default_plans(self) -> list[JsonDict]:
        base = round(self._plus_price_rub, 2)
        return [
            {
                "id": "setka_plus_month",
                "name": "Setka Plus 30 days",
                "price_rub": base,
                "duration_days": 30,
                "features": [
                    "Custom stickers and custom emoji packs",
                    "Status emoji near nickname",
                    "Priority media limits",
                ],
                "active": True,
                "is_default": True,
                "sort_order": 10,
            },
            {
                "id": "setka_plus_quarter",
                "name": "Setka Plus 90 days",
                "price_rub": round(base * 2.7, 2),
                "duration_days": 90,
                "features": [
                    "All monthly features",
                    "Discounted quarterly billing",
                    "Early access to Plus UI updates",
                ],
                "active": True,
                "is_default": False,
                "sort_order": 20,
            },
            {
                "id": "setka_plus_year",
                "name": "Setka Plus 365 days",
                "price_rub": round(base * 9.5, 2),
                "duration_days": 365,
                "features": [
                    "All Plus features",
                    "Best yearly price",
                    "Priority support channel",
                ],
                "active": True,
                "is_default": False,
                "sort_order": 30,
            },
        ]

    def _normalize_plans(self, raw: object) -> list[JsonDict]:
        source = raw if isinstance(raw, list) else self._default_plans()
        normalized: list[JsonDict] = []
        known_ids: set[str] = set()
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            plan_id = _clean_str(item.get("id"), max_len=64)
            name = _clean_str(item.get("name"), max_len=120)
            price_rub = _clean_float(item.get("price_rub"))
            duration_days = _clean_int(item.get("duration_days"))
            if not plan_id or not name or price_rub is None or duration_days is None or duration_days <= 0:
                continue
            if plan_id in known_ids:
                continue
            known_ids.add(plan_id)
            features: list[str] = []
            raw_features = item.get("features")
            if isinstance(raw_features, list):
                for feature in raw_features:
                    cleaned = _clean_str(feature, max_len=160)
                    if cleaned:
                        features.append(cleaned)
            plan: JsonDict = {
                "id": plan_id,
                "name": name,
                "price_rub": round(price_rub, 2),
                "duration_days": duration_days,
                "features": features[:16],
                "active": _clean_bool(item.get("active"), default=True),
                "is_default": _clean_bool(item.get("is_default"), default=False),
                "sort_order": _clean_int(item.get("sort_order")) or ((index + 1) * 10),
            }
            normalized.append(plan)

        if not normalized:
            normalized = [dict(plan) for plan in self._default_plans()]

        active_plans = [plan for plan in normalized if _clean_bool(plan.get("active"), default=False)]
        if not active_plans:
            normalized[0]["active"] = True
            active_plans = [normalized[0]]

        has_default = any(_clean_bool(plan.get("is_default"), default=False) for plan in active_plans)
        if not has_default:
            active_plans[0]["is_default"] = True

        normalized.sort(
            key=lambda plan: (
                int(plan.get("sort_order", 0)),
                str(plan.get("name", "")).lower(),
            )
        )
        return normalized[:32]

    def _persist_plans_file(self, plans: list[JsonDict]) -> None:
        directory = os.path.dirname(self._plans_file)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                return
        try:
            with open(self._plans_file, "w", encoding="utf-8") as stream:
                json.dump({"plans": plans}, stream, ensure_ascii=False, indent=2)
        except OSError:
            # If persistence fails, keep plans in memory at least for this process lifecycle.
            pass

    def _load_plans(self) -> list[JsonDict]:
        if self._plans_cache is not None:
            return [dict(plan) for plan in self._plans_cache]

        parsed: object = None
        try:
            with open(self._plans_file, "r", encoding="utf-8") as stream:
                parsed = json.load(stream)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            raw_plans = parsed.get("plans")
        else:
            raw_plans = parsed
        plans = self._normalize_plans(raw_plans)
        self._plans_cache = [dict(plan) for plan in plans]

        # Backfill file with defaults or normalized content to keep future reads deterministic.
        self._persist_plans_file(plans)
        return [dict(plan) for plan in plans]

    def _find_plan_by_id(self, plan_id: str | None) -> JsonDict | None:
        normalized_id = _clean_str(plan_id, max_len=64)
        if not normalized_id:
            return None
        for plan in self._load_plans():
            if plan.get("id") == normalized_id:
                return dict(plan)
        return None

    def _resolve_plan(self, plan_id: str | None) -> JsonDict:
        plan = self._find_plan_by_id(plan_id)
        if plan and _clean_bool(plan.get("active"), default=True):
            return plan

        plans = self._load_plans()
        default_active = next(
            (
                candidate
                for candidate in plans
                if _clean_bool(candidate.get("active"), default=False)
                and _clean_bool(candidate.get("is_default"), default=False)
            ),
            None,
        )
        if default_active:
            return dict(default_active)

        first_active = next(
            (candidate for candidate in plans if _clean_bool(candidate.get("active"), default=False)),
            None,
        )
        if first_active:
            return dict(first_active)

        return dict(plans[0]) if plans else dict(self._default_plans()[0])
