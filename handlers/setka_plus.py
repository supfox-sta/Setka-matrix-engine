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

import hashlib
import json
import os
import tempfile
import time
import uuid
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlencode

from synapse.api.constants import AccountDataTypes
from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


_SUBSCRIPTION_STATUS = {"inactive", "pending", "active", "expired", "canceled"}
_PACK_KIND = {"sticker", "emoji"}


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

        configured_plans_file = os.environ.get("SETKA_PLUS_PLANS_FILE", "").strip()
        self._plans_file = configured_plans_file or os.path.join(
            tempfile.gettempdir(),
            "setka_plus_plans.json",
        )
        self._plans_cache: list[JsonDict] | None = None

    async def get_subscription(self, user_id: str) -> JsonDict:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_SUBSCRIPTION
        )
        return self._normalize_subscription(raw)

    async def set_subscription(self, user_id: str, updates: JsonDict) -> JsonDict:
        existing = await self.get_subscription(user_id)
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
        return self._normalize_subscription(payload)

    async def get_sticker_packs(self, user_id: str) -> list[JsonDict]:
        raw = await self._store.get_global_account_data_by_type_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_STICKER_PACKS
        )
        if not isinstance(raw, dict):
            return []
        return self._normalize_sticker_packs(raw.get("packs"))

    async def upsert_sticker_pack(
        self, user_id: str, pack_id: str, content: JsonDict
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

    async def delete_sticker_pack(self, user_id: str, pack_id: str) -> None:
        packs = await self.get_sticker_packs(user_id)
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
            normalized.append(normalized_item)
        normalized.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
        return normalized[:100]

    async def record_payment(self, user_id: str, payment: JsonDict) -> None:
        current = await self.get_payments(user_id)
        payment_id = _clean_str(payment.get("payment_id"), max_len=255)
        status = _clean_str(payment.get("status"), max_len=32)
        if not payment_id or not status:
            raise SynapseError(400, "payment_id and status are required", Codes.INVALID_PARAM)

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

        current = [item for item in current if item.get("payment_id") != payment_id]
        current.insert(0, entry)
        current = current[:100]

        await self._account_data_handler.add_account_data_for_user(
            user_id, AccountDataTypes.SETKA_PLUS_PAYMENTS, {"payments": current}
        )

    async def create_yoomoney_payment_request(
        self,
        user_id: str,
        *,
        amount: float | None = None,
        description: str | None = None,
        plan_id: str | None = None,
    ) -> JsonDict:
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
