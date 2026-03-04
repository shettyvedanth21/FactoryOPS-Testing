"""Performance trend materialization and query service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo
import logging

import httpx
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device import Device, DevicePerformanceTrend
from app.services.health_config import HealthConfigService
from app.services.shift import ShiftService

logger = logging.getLogger(__name__)


RANGE_TO_DELTA = {
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


class PerformanceTrendService:
    """Service for building and querying materialized trend snapshots."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._shift_service = ShiftService(session)
        self._health_service = HealthConfigService(session)
        self._tz = ZoneInfo(settings.PERFORMANCE_TRENDS_TIMEZONE)

    def _bucket_bounds_utc(self, now_utc: datetime) -> tuple[datetime, datetime]:
        interval = max(1, settings.PERFORMANCE_TRENDS_INTERVAL_MINUTES)
        now_local = now_utc.astimezone(self._tz)
        minute_floor = (now_local.minute // interval) * interval
        bucket_end_local = now_local.replace(minute=minute_floor, second=0, microsecond=0)
        bucket_start_local = bucket_end_local - timedelta(minutes=interval)
        return (
            bucket_start_local.astimezone(timezone.utc),
            bucket_end_local.astimezone(timezone.utc),
        )

    async def _fetch_bucket_telemetry_mean(
        self,
        device_id: str,
        bucket_start_utc: datetime,
        bucket_end_utc: datetime,
    ) -> tuple[dict[str, float], int]:
        params = {
            "start_time": bucket_start_utc.isoformat(),
            "end_time": bucket_end_utc.isoformat(),
            "aggregate": "mean",
            "interval": f"{max(1, settings.PERFORMANCE_TRENDS_INTERVAL_MINUTES)}m",
            "limit": "1",
        }
        url = f"{settings.DATA_SERVICE_BASE_URL}/api/v1/data/telemetry/{device_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

        items = payload.get("data", {}).get("items", []) if isinstance(payload, dict) else []
        if not items:
            return {}, 0

        latest = items[-1]
        numeric_values: dict[str, float] = {}
        for key, value in latest.items():
            if key in {"timestamp", "device_id", "schema_version", "enrichment_status", "table"}:
                continue
            if isinstance(value, (int, float)):
                numeric_values[key] = float(value)

        return numeric_values, len(items)

    async def _get_uptime_components(self, device_id: str) -> tuple[Optional[float], int, int, int, str]:
        uptime = await self._shift_service.calculate_uptime(device_id)
        active_shifts = [s for s in await self._shift_service.get_shifts_by_device(device_id) if s.is_active]
        break_minutes = sum(s.maintenance_break_minutes for s in active_shifts)
        return (
            uptime.get("uptime_percentage"),
            uptime.get("total_planned_minutes", 0),
            uptime.get("total_effective_minutes", 0),
            break_minutes,
            uptime.get("message", ""),
        )

    async def materialize_latest_bucket(self) -> dict[str, int]:
        """Compute and upsert trend snapshot for all devices for latest time bucket."""
        now_utc = datetime.now(timezone.utc)
        bucket_start_utc, bucket_end_utc = self._bucket_bounds_utc(now_utc)

        device_rows = await self._session.execute(
            select(Device.device_id).where(Device.deleted_at.is_(None))
        )
        device_ids = [d[0] for d in device_rows.all()]

        created = 0
        updated = 0
        failed = 0

        for device_id in device_ids:
            result = await self._materialize_device_bucket(device_id, bucket_start_utc, bucket_end_utc)
            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            else:
                failed += 1

        retention_cutoff = now_utc - timedelta(days=max(1, settings.PERFORMANCE_TRENDS_RETENTION_DAYS))
        await self._session.execute(
            delete(DevicePerformanceTrend)
            .where(DevicePerformanceTrend.bucket_start_utc < retention_cutoff)
            .execution_options(synchronize_session=False)
        )

        await self._session.commit()
        return {"devices_total": len(device_ids), "created": created, "updated": updated, "failed": failed}

    async def _materialize_device_bucket(
        self,
        device_id: str,
        bucket_start_utc: datetime,
        bucket_end_utc: datetime,
    ) -> str:
        try:
            telemetry_values, points_used = await self._fetch_bucket_telemetry_mean(
                device_id,
                bucket_start_utc,
                bucket_end_utc,
            )

            health_result = await self._health_service.calculate_health_score(
                device_id=device_id,
                telemetry_values=telemetry_values,
                machine_state="RUNNING",
            )
            uptime_percentage, planned, effective, break_minutes, uptime_message = await self._get_uptime_components(
                device_id
            )

            health_score = health_result.get("health_score")
            message_parts = []
            if health_result.get("message"):
                message_parts.append(health_result["message"])
            if uptime_message:
                message_parts.append(uptime_message)

            existing = await self._session.execute(
                select(DevicePerformanceTrend).where(
                    and_(
                        DevicePerformanceTrend.device_id == device_id,
                        DevicePerformanceTrend.bucket_start_utc == bucket_start_utc,
                    )
                )
            )
            row = existing.scalar_one_or_none()

            if row:
                row.bucket_end_utc = bucket_end_utc
                row.bucket_timezone = settings.PERFORMANCE_TRENDS_TIMEZONE
                row.interval_minutes = settings.PERFORMANCE_TRENDS_INTERVAL_MINUTES
                row.health_score = health_score
                row.uptime_percentage = uptime_percentage
                row.planned_minutes = planned
                row.effective_minutes = effective
                row.break_minutes = break_minutes
                row.points_used = points_used
                row.is_valid = bool(health_score is not None or uptime_percentage is not None)
                row.message = " | ".join(message_parts) if message_parts else None
                return "updated"

            self._session.add(
                DevicePerformanceTrend(
                    device_id=device_id,
                    bucket_start_utc=bucket_start_utc,
                    bucket_end_utc=bucket_end_utc,
                    bucket_timezone=settings.PERFORMANCE_TRENDS_TIMEZONE,
                    interval_minutes=settings.PERFORMANCE_TRENDS_INTERVAL_MINUTES,
                    health_score=health_score,
                    uptime_percentage=uptime_percentage,
                    planned_minutes=planned,
                    effective_minutes=effective,
                    break_minutes=break_minutes,
                    points_used=points_used,
                    is_valid=bool(health_score is not None or uptime_percentage is not None),
                    message=" | ".join(message_parts) if message_parts else None,
                )
            )
            return "created"
        except Exception as exc:
            logger.error(
                "Failed to materialize performance trend bucket",
                extra={"device_id": device_id, "error": str(exc)},
            )
            return "failed"

    async def get_trends(
        self,
        device_id: str,
        metric: str,
        range_key: str,
    ) -> dict[str, Any]:
        range_delta = RANGE_TO_DELTA.get(range_key, RANGE_TO_DELTA["24h"])
        now_utc = datetime.now(timezone.utc)
        start_utc = now_utc - range_delta

        rows = (
            await self._session.execute(
                select(DevicePerformanceTrend)
                .where(
                    and_(
                        DevicePerformanceTrend.device_id == device_id,
                        DevicePerformanceTrend.bucket_start_utc >= start_utc,
                    )
                )
                .order_by(DevicePerformanceTrend.bucket_start_utc.asc())
            )
        ).scalars().all()

        if not rows:
            now_utc = datetime.now(timezone.utc)
            bucket_start_utc, bucket_end_utc = self._bucket_bounds_utc(now_utc)
            result = await self._materialize_device_bucket(device_id, bucket_start_utc, bucket_end_utc)
            if result in {"created", "updated"}:
                await self._session.commit()
                rows = (
                    await self._session.execute(
                        select(DevicePerformanceTrend)
                        .where(
                            and_(
                                DevicePerformanceTrend.device_id == device_id,
                                DevicePerformanceTrend.bucket_start_utc >= start_utc,
                            )
                        )
                        .order_by(DevicePerformanceTrend.bucket_start_utc.asc())
                    )
                ).scalars().all()

        total_points = len(rows)
        max_points = max(50, settings.PERFORMANCE_TRENDS_MAX_POINTS)
        stride = max(1, total_points // max_points) if total_points > max_points else 1
        sampled_rows = rows[::stride]

        points = []
        for row in sampled_rows:
            ts_local = row.bucket_start_utc.astimezone(self._tz)
            points.append(
                {
                    "timestamp": ts_local.isoformat(),
                    "health_score": row.health_score,
                    "uptime_percentage": row.uptime_percentage,
                    "planned_minutes": row.planned_minutes,
                    "effective_minutes": row.effective_minutes,
                    "break_minutes": row.break_minutes,
                }
            )

        last_message = sampled_rows[-1].message if sampled_rows else "No trend data found for selected range."
        return {
            "device_id": device_id,
            "metric": metric,
            "range": range_key,
            "interval_minutes": settings.PERFORMANCE_TRENDS_INTERVAL_MINUTES,
            "timezone": settings.PERFORMANCE_TRENDS_TIMEZONE,
            "points": points,
            "total_points": total_points,
            "sampled_points": len(points),
            "message": last_message,
        }
