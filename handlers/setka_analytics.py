# This file is licensed under the Affero General Public License (AGPL) version 3.
# Copyright (C) 2026 Setka Matrix

"""Setka business analytics — event log + investor KPI aggregation."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

_MS_DAY = 86_400_000
_MS_HOUR = 3_600_000
_ONLINE_WINDOW_MS = 15 * 60 * 1000

# Canonical event names (client + server)
ALLOWED_EVENTS = frozenset(
    {
        "session.active",
        "user.registered",
        "login.verified",
        "login.fallback.emoji",
        "login.fallback.recovery_key",
        "login.skey.silent_success",
        "login.skey.silent_failed",
        "skey.enabled",
        "skey.disabled",
        "plus.checkout_started",
        "plus.payment_created",
        "plus.payment_paid",
        "plus.payment_failed",
        "plus.subscription_activated",
        "plus.subscription_expired",
        "channel.published",
        "channel.joined",
        "channel.discovery_viewed",
        "screen.viewed",
        "feature.used",
    }
)


def _clean_str(value: object, *, max_len: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_len]


class SetkaAnalyticsHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()

    async def record_event(
        self,
        *,
        event_name: str,
        user_id: str | None = None,
        platform: str | None = None,
        app_version: str | None = None,
        device_id: str | None = None,
        properties: JsonDict | None = None,
        source: str = "client",
        ts: int | None = None,
    ) -> None:
        name = _clean_str(event_name, max_len=64)
        if not name or name not in ALLOWED_EVENTS:
            logger.debug("Setka analytics: ignored event %r", event_name)
            return

        event_ts = ts if ts is not None else self._clock.time_msec()
        props = properties if isinstance(properties, dict) else {}

        def _txn(txn: Any) -> None:
            from synapse.storage.engines import PostgresEngine

            if isinstance(self._store.database_engine, PostgresEngine):
                from psycopg2.extras import Json

                props_value: object = Json(props)
            else:
                props_value = json.dumps(props)

            txn.execute(
                """
                INSERT INTO setka_analytics_events
                    (event_name, user_id, platform, app_version, device_id, ts, properties, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    user_id,
                    _clean_str(platform, max_len=32),
                    _clean_str(app_version, max_len=64),
                    _clean_str(device_id, max_len=128),
                    event_ts,
                    props_value,
                    _clean_str(source, max_len=16) or "client",
                ),
            )

        try:
            await self._store.db_pool.runInteraction("setka_analytics_insert", _txn)
        except Exception:
            logger.warning("Setka analytics: failed to record %s", name, exc_info=True)

    async def record_events_batch(
        self,
        user_id: str | None,
        device_id: str | None,
        platform: str | None,
        app_version: str | None,
        events: list[JsonDict],
    ) -> int:
        recorded = 0
        for item in events[:50]:
            if not isinstance(item, dict):
                continue
            name = item.get("event_name") or item.get("event")
            if not isinstance(name, str):
                continue
            props = item.get("properties")
            if not isinstance(props, dict):
                props = {}
            ts = item.get("ts")
            event_ts = int(ts) if isinstance(ts, (int, float)) else None
            await self.record_event(
                event_name=name,
                user_id=user_id,
                platform=platform or _clean_str(item.get("platform"), max_len=32),
                app_version=app_version or _clean_str(item.get("app_version"), max_len=64),
                device_id=device_id,
                properties=props,
                source="client",
                ts=event_ts,
            )
            recorded += 1
        return recorded

    async def get_overview(self, *, days: int = 30) -> JsonDict:
        days = max(1, min(int(days), 365))
        now_ms = self._clock.time_msec()
        since_ms = now_ms - days * _MS_DAY
        dau_since = now_ms - _MS_DAY
        wau_since = now_ms - 7 * _MS_DAY

        live = self._empty_live_metrics()
        online = self._empty_online_metrics()
        analytics_ready = False
        total_events = 0
        event_metrics = self._empty_event_metrics()

        try:
            live = await self._store.db_pool.runInteraction(
                "setka_analytics_live",
                lambda txn: SetkaAnalyticsHandler._live_metrics_txn(txn, since_ms, now_ms),
            )
        except Exception:
            logger.warning("Setka analytics live metrics failed", exc_info=True)

        try:
            online = await self._store.db_pool.runInteraction(
                "setka_analytics_online",
                lambda txn: SetkaAnalyticsHandler._online_metrics_txn(txn, since_ms, now_ms),
            )
        except Exception:
            logger.warning("Setka analytics online metrics failed", exc_info=True)

        try:
            analytics_ready, total_events = await self._store.db_pool.runInteraction(
                "setka_analytics_events_stats",
                SetkaAnalyticsHandler._events_table_stats_txn,
            )
        except Exception:
            logger.warning("Setka analytics events table check failed", exc_info=True)

        if analytics_ready:
            try:
                event_metrics = await self._store.db_pool.runInteraction(
                    "setka_analytics_events_overview",
                    lambda txn: SetkaAnalyticsHandler._event_metrics_txn(
                        txn,
                        since_ms=since_ms,
                        dau_since=dau_since,
                        wau_since=wau_since,
                        days=days,
                    ),
                )
            except Exception:
                logger.warning("Setka analytics event metrics failed", exc_info=True)

        metrics = {
            **event_metrics,
            "plus_activations_30d": max(
                event_metrics["plus_activations_30d"], live["plus_activations_live_period"]
            ),
            "plus_payments_paid_30d": max(
                event_metrics["plus_payments_paid_30d"], live["plus_payments_live_period"]
            ),
            "plus_active_now": live["plus_active_now"],
            "skey_enabled_30d": max(
                event_metrics["skey_enabled_30d"], live["skey_enabled_now"]
            ),
            "skey_enabled_now": live["skey_enabled_now"],
            "skey_silent_success_30d": (
                event_metrics["skey_silent_success_30d"]
                + event_metrics.get("skey_fallback_events", 0)
            ),
            **online,
            "analytics_ready": analytics_ready,
            "analytics_events_total": total_events,
        }
        metrics.pop("skey_fallback_events", None)

        total_users = 0
        total_users_all = 0
        try:
            counts = await self._store.db_pool.runInteraction(
                "setka_analytics_user_count",
                SetkaAnalyticsHandler._count_users_txn,
            )
            total_users = int(counts["active"])
            total_users_all = int(counts["all"])
        except Exception:
            pass

        return {
            "generated_at_ms": now_ms,
            "period_days": days,
            "total_users": total_users,
            "total_users_all": total_users_all,
            **metrics,
        }

    @staticmethod
    def _empty_live_metrics() -> JsonDict:
        return {
            "plus_active_now": 0,
            "skey_enabled_now": 0,
            "plus_payments_live_period": 0,
            "plus_activations_live_period": 0,
        }

    @staticmethod
    def _empty_online_metrics() -> JsonDict:
        return {
            "online_now": 0,
            "peak_online_period": 0,
            "peak_online_today": 0,
            "daily_peak_online": [],
        }

    @staticmethod
    def _empty_event_metrics() -> JsonDict:
        return {
            "dau": 0,
            "wau": 0,
            "mau": 0,
            "platform_split_30d": {},
            "registrations_30d": 0,
            "plus_activations_30d": 0,
            "plus_payments_paid_30d": 0,
            "skey_enabled_30d": 0,
            "channels_published_30d": 0,
            "login_verified_30d": 0,
            "skey_silent_success_30d": 0,
            "skey_fallback_events": 0,
            "daily_active_users": [],
        }

    @staticmethod
    def _event_metrics_txn(
        txn: Any,
        *,
        since_ms: int,
        dau_since: int,
        wau_since: int,
        days: int,
    ) -> JsonDict:
        def distinct_users(since: int) -> int:
            txn.execute(
                """
                SELECT COUNT(DISTINCT user_id) FROM setka_analytics_events
                WHERE user_id IS NOT NULL AND ts >= ?
                """,
                (since,),
            )
            row = txn.fetchone()
            return int(row[0]) if row else 0

        def event_count(name: str, since: int) -> int:
            txn.execute(
                """
                SELECT COUNT(*) FROM setka_analytics_events
                WHERE event_name = ? AND ts >= ?
                """,
                (name, since),
            )
            row = txn.fetchone()
            return int(row[0]) if row else 0

        def platform_split(since: int) -> JsonDict:
            txn.execute(
                """
                SELECT COALESCE(platform, 'unknown') AS p, COUNT(DISTINCT user_id)
                FROM setka_analytics_events
                WHERE user_id IS NOT NULL AND ts >= ?
                GROUP BY p
                """,
                (since,),
            )
            return {str(row[0]): int(row[1]) for row in txn.fetchall()}

        def daily_series(since: int) -> list[JsonDict]:
            txn.execute(
                """
                SELECT (ts / ?) AS day_bucket, COUNT(DISTINCT user_id)
                FROM setka_analytics_events
                WHERE user_id IS NOT NULL AND ts >= ? AND event_name = 'session.active'
                GROUP BY day_bucket
                ORDER BY day_bucket
                """,
                (_MS_DAY, since),
            )
            return [
                {"day_ms": int(row[0]) * _MS_DAY, "active_users": int(row[1])}
                for row in txn.fetchall()
            ]

        def hourly_series(since: int) -> list[JsonDict]:
            txn.execute(
                """
                SELECT (last_seen / ?) AS hour_bucket, COUNT(DISTINCT user_id)
                FROM user_ips
                WHERE last_seen >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket
                """,
                (_MS_HOUR, since),
            )
            return [
                {"hour_ms": int(row[0]) * _MS_HOUR, "online_users": int(row[1])}
                for row in txn.fetchall()
            ]

        hourly_since = since_ms if days == 1 else dau_since
        return {
            "dau": distinct_users(dau_since),
            "wau": distinct_users(wau_since),
            "mau": distinct_users(since_ms),
            "platform_split_30d": platform_split(since_ms),
            "registrations_30d": event_count("user.registered", since_ms),
            "plus_activations_30d": event_count("plus.subscription_activated", since_ms),
            "plus_payments_paid_30d": event_count("plus.payment_paid", since_ms),
            "skey_enabled_30d": event_count("skey.enabled", since_ms),
            "channels_published_30d": event_count("channel.published", since_ms),
            "login_verified_30d": event_count("login.verified", since_ms),
            "skey_silent_success_30d": event_count("login.skey.silent_success", since_ms),
            "skey_fallback_events": event_count("login.fallback.recovery_key", since_ms),
            "daily_active_users": daily_series(since_ms),
            "hourly_online_users": hourly_series(hourly_since),
        }

    @staticmethod
    def _count_users_txn(txn: Any) -> dict[str, int]:
        txn.execute("SELECT COUNT(*) FROM users WHERE deactivated = 0")
        active_row = txn.fetchone()
        active = int(active_row[0]) if active_row else 0
        txn.execute("SELECT COUNT(*) FROM users")
        all_row = txn.fetchone()
        all_users = int(all_row[0]) if all_row else 0
        return {"active": active, "all": all_users}

    @staticmethod
    def _events_table_stats_txn(txn: Any) -> tuple[bool, int]:
        try:
            txn.execute("SELECT COUNT(*) FROM setka_analytics_events")
            row = txn.fetchone()
            return True, int(row[0]) if row else 0
        except Exception:
            return False, 0

    @staticmethod
    def _online_metrics_txn(txn: Any, since_ms: int, now_ms: int) -> JsonDict:
        online_now = 0
        peak_period = 0
        peak_today = 0
        daily_peak: dict[int, int] = {}

        try:
            txn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM user_ips WHERE last_seen >= ?",
                (now_ms - _ONLINE_WINDOW_MS,),
            )
            row = txn.fetchone()
            online_now = int(row[0]) if row else 0

            txn.execute(
                """
                SELECT (last_seen / ?) AS hour_bucket, COUNT(DISTINCT user_id) AS cnt
                FROM user_ips
                WHERE last_seen >= ?
                GROUP BY hour_bucket
                """,
                (_MS_HOUR, since_ms),
            )
            today_bucket = now_ms // _MS_DAY
            for hour_bucket, cnt in txn.fetchall():
                cnt_int = int(cnt)
                peak_period = max(peak_period, cnt_int)
                day_bucket = int(hour_bucket) // 24
                day_ms = day_bucket * _MS_DAY
                daily_peak[day_ms] = max(daily_peak.get(day_ms, 0), cnt_int)
                if day_bucket == today_bucket:
                    peak_today = max(peak_today, cnt_int)
        except Exception:
            logger.debug("Setka analytics online metrics unavailable", exc_info=True)

        return {
            "online_now": online_now,
            "peak_online_period": peak_period,
            "peak_online_today": peak_today,
            "daily_peak_online": [
                {"day_ms": day_ms, "peak_online": peak}
                for day_ms, peak in sorted(daily_peak.items())
            ],
        }

    @staticmethod
    def _live_metrics_txn(txn: Any, since_ms: int, now_ms: int) -> JsonDict:
        from synapse.api.constants import AccountDataTypes

        plus_active = 0
        skey_enabled = 0
        plus_payments = 0
        plus_activations = 0

        try:
            txn.execute(
                """
                SELECT account_data_type, content FROM account_data
                WHERE account_data_type IN (?, ?, ?)
                  AND content IS NOT NULL AND content != '{}'
                """,
                (
                    AccountDataTypes.SETKA_PLUS_SUBSCRIPTION,
                    AccountDataTypes.SETKA_PLUS_PAYMENTS,
                    AccountDataTypes.SETKA_LOGIN,
                ),
            )
            for account_data_type, content_raw in txn.fetchall():
                try:
                    if isinstance(content_raw, (bytes, memoryview)):
                        content_raw = content_raw.decode("utf-8")
                    content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                except Exception:
                    continue
                if not isinstance(content, dict):
                    continue

                if account_data_type == AccountDataTypes.SETKA_PLUS_SUBSCRIPTION:
                    status = str(content.get("status", "")).lower()
                    expires_at = int(content.get("expires_at") or 0)
                    is_active_flag = content.get("is_active") is True
                    plus_is_live = expires_at > now_ms and (
                        is_active_flag or status == "active" or status == ""
                    )
                    if plus_is_live and status not in {"inactive", "cancelled", "expired"}:
                        plus_active += 1
                    started_at = int(content.get("started_at") or 0)
                    updated_at = int(content.get("updated_at") or 0)
                    if plus_is_live and max(started_at, updated_at) >= since_ms:
                        plus_activations += 1
                elif account_data_type == AccountDataTypes.SETKA_LOGIN:
                    backup = content.get("recovery_backup")
                    if isinstance(backup, dict) and backup:
                        skey_enabled += 1
                elif account_data_type == AccountDataTypes.SETKA_PLUS_PAYMENTS:
                    payments = content.get("payments")
                    if isinstance(payments, list):
                        for payment in payments:
                            if not isinstance(payment, dict):
                                continue
                            status = str(payment.get("status", "")).lower()
                            if status not in {"success", "paid"}:
                                continue
                            created = int(
                                payment.get("created_at") or payment.get("paid_at") or 0
                            )
                            if created >= since_ms:
                                plus_payments += 1
        except Exception:
            logger.debug("Setka analytics live metrics unavailable", exc_info=True)

        return {
            "plus_active_now": plus_active,
            "skey_enabled_now": skey_enabled,
            "plus_payments_live_period": plus_payments,
            "plus_activations_live_period": plus_activations,
        }

    async def list_online_users(self) -> JsonDict:
        """Return users currently online (same window as overview online_now)."""
        now_ms = self._clock.time_msec()
        window_start = now_ms - _ONLINE_WINDOW_MS

        def _txn(txn: Any) -> list[JsonDict]:
            txn.execute(
                """
                SELECT user_id, MAX(last_seen) AS last_seen_ms
                FROM user_ips
                WHERE last_seen >= ?
                GROUP BY user_id
                ORDER BY last_seen_ms DESC
                """,
                (window_start,),
            )
            return [
                {"user_id": str(row[0]), "last_seen_ms": int(row[1])}
                for row in txn.fetchall()
            ]

        try:
            users = await self._store.db_pool.runInteraction(
                "setka_analytics_online_users",
                _txn,
            )
        except Exception:
            logger.warning("Setka analytics online users list failed", exc_info=True)
            users = []

        return {"users": users}

    async def get_retention(self, *, cohort_days: int = 30) -> JsonDict:
        """Simple D1/D7/D30 retention from session.active events."""
        cohort_days = max(7, min(int(cohort_days), 90))
        now_ms = self._clock.time_msec()
        cohort_start = now_ms - cohort_days * _MS_DAY

        def _txn(txn: Any) -> JsonDict:
            txn.execute(
                """
                WITH first_seen AS (
                    SELECT user_id, MIN(ts) AS first_ts
                    FROM setka_analytics_events
                    WHERE user_id IS NOT NULL AND event_name IN ('session.active', 'user.registered')
                    GROUP BY user_id
                ),
                cohort AS (
                    SELECT user_id, first_ts FROM first_seen
                    WHERE first_ts >= ? AND first_ts < ?
                )
                SELECT
                    COUNT(*) AS cohort_size,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM setka_analytics_events e
                            WHERE e.user_id = cohort.user_id
                              AND e.event_name = 'session.active'
                              AND e.ts >= cohort.first_ts + ?
                              AND e.ts < cohort.first_ts + ? + ?
                        )
                    ) AS d1,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM setka_analytics_events e
                            WHERE e.user_id = cohort.user_id
                              AND e.event_name = 'session.active'
                              AND e.ts >= cohort.first_ts + ?
                              AND e.ts < cohort.first_ts + ? + ?
                        )
                    ) AS d7,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM setka_analytics_events e
                            WHERE e.user_id = cohort.user_id
                              AND e.event_name = 'session.active'
                              AND e.ts >= cohort.first_ts + ?
                              AND e.ts < cohort.first_ts + ? + ?
                        )
                    ) AS d30
                FROM cohort
                """,
                (
                    cohort_start,
                    now_ms - 30 * _MS_DAY,
                    0,
                    0,
                    _MS_DAY,
                    6 * _MS_DAY,
                    6 * _MS_DAY,
                    _MS_DAY,
                    29 * _MS_DAY,
                    29 * _MS_DAY,
                    _MS_DAY,
                ),
            )
            row = txn.fetchone()
            if not row:
                return {"cohort_size": 0, "d1_pct": 0, "d7_pct": 0, "d30_pct": 0}
            size = int(row[0]) or 0
            if size == 0:
                return {"cohort_size": 0, "d1_pct": 0, "d7_pct": 0, "d30_pct": 0}
            return {
                "cohort_size": size,
                "d1_pct": round(100.0 * int(row[1]) / size, 1),
                "d7_pct": round(100.0 * int(row[2]) / size, 1),
                "d30_pct": round(100.0 * int(row[3]) / size, 1),
            }

        try:
            data = await self._store.db_pool.runInteraction(
                "setka_analytics_retention", _txn
            )
        except Exception:
            logger.warning("Setka analytics retention failed", exc_info=True)
            data = {"cohort_size": 0, "d1_pct": 0, "d7_pct": 0, "d30_pct": 0}

        return {"period_days": cohort_days, **data}
