"""Service layer for home dashboard aggregates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
from sqlalchemy import Float, and_, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device import Device, DevicePerformanceTrend, DeviceShift, RuntimeStatus
from app.services.health_config import HealthConfigService


class DashboardService:
    """Aggregate system-level and per-device metrics for the home dashboard."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _get_alerts_summary(self) -> Dict[str, int]:
        """Fetch alert summary from rule-engine service."""
        url = f"{settings.RULE_ENGINE_SERVICE_BASE_URL}/api/v1/alerts/events/summary"
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                return {
                    "active_alerts": int(data.get("active_alerts", 0)),
                    "alerts_triggered": int(data.get("alerts_triggered", 0)),
                    "alerts_cleared": int(data.get("alerts_cleared", 0)),
                    "rules_created": int(data.get("rules_created", 0)),
                }
        except Exception:
            return {
                "active_alerts": 0,
                "alerts_triggered": 0,
                "alerts_cleared": 0,
                "rules_created": 0,
            }

    async def _fetch_latest_telemetry_values(
        self,
        client: httpx.AsyncClient,
        device_id: str,
    ) -> dict[str, float]:
        """Fetch latest telemetry point and return numeric fields only."""
        try:
            url = f"{settings.DATA_SERVICE_BASE_URL}/api/v1/data/telemetry/{device_id}"
            response = await client.get(url, params={"limit": "1"})
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data", {}).get("items", []) if isinstance(payload, dict) else []
            if not items:
                return {}
            latest = items[0]
            numeric_values: dict[str, float] = {}
            for key, value in latest.items():
                if key in {"timestamp", "device_id", "schema_version", "enrichment_status", "table"}:
                    continue
                if isinstance(value, (int, float)):
                    numeric_values[key] = float(value)
            return numeric_values
        except Exception:
            return {}

    async def get_dashboard_summary(self) -> Dict[str, Any]:
        """Get full dashboard summary payload."""
        latest_health_bucket_subq = (
            select(
                DevicePerformanceTrend.device_id.label("device_id"),
                func.max(DevicePerformanceTrend.bucket_start_utc).label("latest_health_bucket"),
            )
            .where(DevicePerformanceTrend.health_score.is_not(None))
            .group_by(DevicePerformanceTrend.device_id)
            .subquery()
        )

        latest_trend_subq = (
            select(
                DevicePerformanceTrend.device_id.label("device_id"),
                DevicePerformanceTrend.health_score.label("health_score"),
            )
            .join(
                latest_health_bucket_subq,
                and_(
                    DevicePerformanceTrend.device_id == latest_health_bucket_subq.c.device_id,
                    DevicePerformanceTrend.bucket_start_utc == latest_health_bucket_subq.c.latest_health_bucket,
                ),
            )
            .subquery()
        )

        start_sec = func.time_to_sec(DeviceShift.shift_start)
        end_sec = func.time_to_sec(DeviceShift.shift_end)
        planned_minutes_expr = (
            case(
                (end_sec <= start_sec, (end_sec + literal(86400)) - start_sec),
                else_=end_sec - start_sec,
            )
            / literal(60.0)
        )
        effective_minutes_expr = planned_minutes_expr - cast(DeviceShift.maintenance_break_minutes, Float)

        shift_agg_subq = (
            select(
                DeviceShift.device_id.label("device_id"),
                func.sum(planned_minutes_expr).label("planned_minutes"),
                func.sum(effective_minutes_expr).label("effective_minutes"),
            )
            .where(DeviceShift.is_active.is_(True))
            .group_by(DeviceShift.device_id)
            .subquery()
        )

        health_config_subq = (
            select(
                DeviceShift.device_id.label("device_id"),
            )
            .where(literal(False))
            .subquery()
        )
        # Keep this query independent from ORM relationship loading for performance.
        from app.models.device import ParameterHealthConfig
        health_config_subq = (
            select(
                ParameterHealthConfig.device_id.label("device_id"),
                func.count(ParameterHealthConfig.id).label("active_health_config_count"),
            )
            .where(ParameterHealthConfig.is_active.is_(True))
            .group_by(ParameterHealthConfig.device_id)
            .subquery()
        )

        device_rows = await self._session.execute(
            select(
                Device.device_id,
                Device.device_name,
                Device.device_type,
                Device.location,
                Device.last_seen_timestamp,
                latest_trend_subq.c.health_score,
                shift_agg_subq.c.planned_minutes,
                shift_agg_subq.c.effective_minutes,
                health_config_subq.c.active_health_config_count,
            )
            .outerjoin(latest_trend_subq, latest_trend_subq.c.device_id == Device.device_id)
            .outerjoin(shift_agg_subq, shift_agg_subq.c.device_id == Device.device_id)
            .outerjoin(health_config_subq, health_config_subq.c.device_id == Device.device_id)
            .where(Device.deleted_at.is_(None))
            .order_by(Device.device_name.asc())
        )

        devices: List[Dict[str, Any]] = []
        health_values: List[float] = []
        uptime_values: List[float] = []
        running_count = 0
        uptime_configured_count = 0
        fallback_health_candidates: List[Dict[str, Any]] = []

        for row in device_rows.all():
            runtime_status = RuntimeStatus.STOPPED.value
            if row.last_seen_timestamp is not None:
                # Reuse model logic for status consistency.
                d = Device(
                    device_id=row.device_id,
                    device_name=row.device_name,
                    device_type=row.device_type,
                )
                d.last_seen_timestamp = row.last_seen_timestamp
                runtime_status = d.get_runtime_status()

            if runtime_status == RuntimeStatus.RUNNING.value:
                running_count += 1

            health_score = float(row.health_score) if row.health_score is not None else None
            if health_score is not None:
                health_values.append(health_score)

            uptime_percentage = None
            planned_minutes = float(row.planned_minutes or 0)
            effective_minutes = float(row.effective_minutes or 0)
            if planned_minutes > 0:
                uptime_configured_count += 1
                uptime_percentage = round((effective_minutes / planned_minutes) * 100.0, 2)
                uptime_values.append(uptime_percentage)

            device_item = {
                "device_id": row.device_id,
                "device_name": row.device_name,
                "device_type": row.device_type,
                "runtime_status": runtime_status,
                "location": row.location,
                "last_seen_timestamp": row.last_seen_timestamp,
                "health_score": round(health_score, 2) if health_score is not None else None,
                "uptime_percentage": uptime_percentage,
            }
            devices.append(device_item)
            if health_score is None and int(row.active_health_config_count or 0) > 0:
                fallback_health_candidates.append(device_item)

        if fallback_health_candidates:
            health_service = HealthConfigService(self._session)
            async with httpx.AsyncClient(timeout=3.0) as client:
                for device_item in fallback_health_candidates:
                    telemetry_values = await self._fetch_latest_telemetry_values(client, device_item["device_id"])
                    if not telemetry_values:
                        continue
                    try:
                        result = await health_service.calculate_health_score(
                            device_id=device_item["device_id"],
                            telemetry_values=telemetry_values,
                            machine_state="RUNNING",
                        )
                    except Exception:
                        continue
                    fallback_health = result.get("health_score")
                    if fallback_health is None:
                        continue
                    fallback_health = round(float(fallback_health), 2)
                    device_item["health_score"] = fallback_health
                    health_values.append(fallback_health)

        total_devices = len(devices)
        stopped_count = max(total_devices - running_count, 0)
        devices_with_health_data = len(health_values)
        devices_missing_uptime_config = max(total_devices - uptime_configured_count, 0)

        alerts = await self._get_alerts_summary()

        return {
            "success": True,
            "generated_at": datetime.now(timezone.utc),
            "summary": {
                "total_devices": total_devices,
                "running_devices": running_count,
                "stopped_devices": stopped_count,
                "devices_with_health_data": devices_with_health_data,
                "devices_with_uptime_configured": uptime_configured_count,
                "devices_missing_uptime_config": devices_missing_uptime_config,
                "system_health": round(sum(health_values) / devices_with_health_data, 2)
                if devices_with_health_data > 0
                else None,
                "average_efficiency": round(sum(uptime_values) / len(uptime_values), 2)
                if uptime_values
                else None,
            },
            "alerts": alerts,
            "devices": devices,
        }
