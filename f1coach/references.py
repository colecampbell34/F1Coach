from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from f1coach.coach import LapSample, ReferenceProfile


def load_reference(path: str | Path) -> ReferenceProfile:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = [_sample_from_dict(item) for item in raw.get("samples", [])]
    if not samples:
        raise ValueError("Reference file does not contain any samples.")

    return ReferenceProfile(
        name=str(raw.get("name") or "Imported reference"),
        source=str(raw.get("source") or "imported-json"),
        lap_time_ms=int(raw.get("lapTimeMs") or raw.get("lap_time_ms") or samples[-1].lap_time_ms),
        sector1_time_ms=int(raw.get("sector1Ms") or raw.get("sector1_time_ms") or 0),
        sector2_time_ms=int(raw.get("sector2Ms") or raw.get("sector2_time_ms") or 0),
        samples=samples,
        lap_count=int(raw.get("lapCount") or raw.get("lap_count") or 1),
        synthetic=bool(raw.get("synthetic", False)),
        assist_profile=dict(raw.get("assistProfile") or raw.get("assist_profile") or {}),
    )


def reference_to_json(reference: ReferenceProfile) -> str:
    payload = {
        "name": reference.name,
        "source": reference.source,
        "lapTimeMs": reference.lap_time_ms,
        "sector1Ms": reference.sector1_time_ms,
        "sector2Ms": reference.sector2_time_ms,
        "lapCount": reference.lap_count,
        "synthetic": reference.synthetic,
        "assistProfile": reference.assist_profile,
        "samples": [_sample_to_dict(sample) for sample in reference.samples],
    }
    return json.dumps(payload, indent=2)


def _sample_from_dict(raw: dict[str, Any]) -> LapSample:
    return LapSample(
        lap_time_ms=int(_get(raw, "lapTimeMs", "lap_time_ms", default=0)),
        lap_distance_m=float(_get(raw, "lapDistanceM", "lap_distance_m", default=0.0)),
        normalized_distance=float(_get(raw, "normalizedDistance", "normalized_distance", default=0.0)),
        speed_kmh=int(_get(raw, "speedKmh", "speed_kmh", default=0)),
        throttle=float(_get(raw, "throttle", default=0.0)),
        brake=float(_get(raw, "brake", default=0.0)),
        steer=float(_get(raw, "steer", default=0.0)),
        gear=int(_get(raw, "gear", default=0)),
        engine_rpm=int(_get(raw, "engineRpm", "engine_rpm", default=0)),
        ers_percent=_optional_float(_get(raw, "ersPercent", "ers_percent", default=None)),
        fuel_kg=_optional_float(_get(raw, "fuelKg", "fuel_kg", default=None)),
        lateral_g=_optional_float(_get(raw, "lateralG", "lateral_g", default=None)),
        longitudinal_g=_optional_float(_get(raw, "longitudinalG", "longitudinal_g", default=None)),
        avg_slip_ratio=_optional_float(_get(raw, "avgSlipRatio", "avg_slip_ratio", default=None)),
        avg_slip_angle=_optional_float(_get(raw, "avgSlipAngle", "avg_slip_angle", default=None)),
        world_position=_position(_get(raw, "worldPosition", "world_position", default=None)),
    )


def _sample_to_dict(sample: LapSample) -> dict[str, Any]:
    return {
        "lapTimeMs": sample.lap_time_ms,
        "lapDistanceM": sample.lap_distance_m,
        "normalizedDistance": sample.normalized_distance,
        "speedKmh": sample.speed_kmh,
        "throttle": sample.throttle,
        "brake": sample.brake,
        "steer": sample.steer,
        "gear": sample.gear,
        "engineRpm": sample.engine_rpm,
        "ersPercent": sample.ers_percent,
        "fuelKg": sample.fuel_kg,
        "lateralG": sample.lateral_g,
        "longitudinalG": sample.longitudinal_g,
        "avgSlipRatio": sample.avg_slip_ratio,
        "avgSlipAngle": sample.avg_slip_angle,
        "worldPosition": sample.world_position,
    }


def _get(raw: dict[str, Any], *keys: str, default: Any) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return default


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _position(value: Any) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if len(value) != 3:
        return None
    return (float(value[0]), float(value[1]), float(value[2]))
