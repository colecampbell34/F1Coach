from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any

from f1coach.models import (
    CarStatusSnapshot,
    CarTelemetrySnapshot,
    LapSnapshot,
    MotionExSnapshot,
    MotionSnapshot,
    SessionInfo,
    TelemetryMessage,
)


@dataclass(frozen=True, slots=True)
class LapSample:
    lap_time_ms: int
    lap_distance_m: float
    normalized_distance: float
    speed_kmh: int
    throttle: float
    brake: float
    steer: float
    gear: int
    engine_rpm: int
    ers_percent: float | None
    fuel_kg: float | None
    lateral_g: float | None
    longitudinal_g: float | None
    avg_slip_ratio: float | None
    avg_slip_angle: float | None
    world_position: tuple[float, float, float] | None


@dataclass(slots=True)
class CompletedLap:
    lap_num: int
    lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    invalid: bool
    samples: list[LapSample] = field(default_factory=list)

    @property
    def sector3_time_ms(self) -> int:
        sector3 = self.lap_time_ms - self.sector1_time_ms - self.sector2_time_ms
        return max(0, sector3)


@dataclass(slots=True)
class ReferenceProfile:
    name: str
    source: str
    lap_time_ms: int
    sector1_time_ms: int
    sector2_time_ms: int
    samples: list[LapSample] = field(default_factory=list)
    lap_count: int = 1
    synthetic: bool = False
    assist_profile: dict[str, str] = field(default_factory=dict)

    @property
    def sector3_time_ms(self) -> int:
        return max(0, self.lap_time_ms - self.sector1_time_ms - self.sector2_time_ms)


@dataclass(frozen=True, slots=True)
class SegmentInsight:
    area: str
    start_pct: float
    end_pct: float
    time_delta_ms: int
    speed_delta_kmh: float
    severity: str
    category: str
    detail: str


class LapCoach:
    def __init__(self, sample_buckets: int = 240, external_reference: ReferenceProfile | None = None) -> None:
        self.sample_buckets = sample_buckets
        self.track_length_m: int | None = None
        self.track_id: int | None = None
        self.latest_telemetry: CarTelemetrySnapshot | None = None
        self.latest_status: CarStatusSnapshot | None = None
        self.latest_motion: MotionSnapshot | None = None
        self.latest_motion_ex: MotionExSnapshot | None = None
        self.active_lap_num: int | None = None
        self.active_invalid = False
        self.active_sector1_time_ms = 0
        self.active_sector2_time_ms = 0
        self.active_samples: list[LapSample] = []
        self.best_lap: CompletedLap | None = None
        self.clean_laps: list[CompletedLap] = []
        self.ideal_reference: ReferenceProfile | None = None
        self.external_reference = external_reference
        self.completed_laps: list[CompletedLap] = []
        self.latest_insights: list[SegmentInsight] = []
        self.notices: list[str] = []
        self.last_hint_at = 0.0

    def update(self, message: TelemetryMessage) -> list[str]:
        if isinstance(message, SessionInfo):
            notices = self._update_session(message)
            self._record_notices(notices)
            return notices
        if isinstance(message, CarTelemetrySnapshot):
            self.latest_telemetry = message
            return []
        if isinstance(message, CarStatusSnapshot):
            self.latest_status = message
            return []
        if isinstance(message, MotionSnapshot):
            self.latest_motion = message
            return []
        if isinstance(message, MotionExSnapshot):
            self.latest_motion_ex = message
            return []
        if isinstance(message, LapSnapshot):
            notices = self._update_lap(message)
            self._record_notices(notices)
            return notices
        return []

    def _update_session(self, message: SessionInfo) -> list[str]:
        notices: list[str] = []
        if message.track_length_m > 0 and message.track_length_m != self.track_length_m:
            self.track_length_m = message.track_length_m
            self.track_id = message.track_id
            notices.append(
                f"Session detected: track {message.track_id}, {message.track_length_m} m, {message.total_laps} laps."
            )
        return notices

    def _update_lap(self, lap: LapSnapshot) -> list[str]:
        notices: list[str] = []
        if lap.driver_status not in {1, 4}:
            return notices

        if self.active_lap_num is None:
            self.active_lap_num = lap.current_lap_num

        if lap.current_lap_num != self.active_lap_num:
            completed = self._complete_lap(lap)
            if completed is not None:
                notices.extend(self._summarize_completed_lap(completed))
                self.completed_laps.append(completed)
                self.completed_laps = self.completed_laps[-20:]
            self.active_lap_num = lap.current_lap_num
            self.active_invalid = False
            self.active_sector1_time_ms = 0
            self.active_sector2_time_ms = 0
            self.active_samples = []

        self.active_invalid = self.active_invalid or lap.current_lap_invalid
        if lap.sector1_time_ms > 0:
            self.active_sector1_time_ms = lap.sector1_time_ms
        if lap.sector2_time_ms > 0:
            self.active_sector2_time_ms = lap.sector2_time_ms
        sample = self._sample_from_lap(lap)
        if sample is not None:
            self.active_samples.append(sample)
            hint = self._live_hint(sample)
            if hint is not None:
                notices.append(hint)
        return notices

    def _complete_lap(self, new_lap_snapshot: LapSnapshot) -> CompletedLap | None:
        if self.active_lap_num is None or new_lap_snapshot.last_lap_time_ms <= 0:
            return None
        return CompletedLap(
            lap_num=self.active_lap_num,
            lap_time_ms=new_lap_snapshot.last_lap_time_ms,
            sector1_time_ms=self.active_sector1_time_ms,
            sector2_time_ms=self.active_sector2_time_ms,
            invalid=self.active_invalid,
            samples=self._dedupe_samples(self.active_samples),
        )

    def _sample_from_lap(self, lap: LapSnapshot) -> LapSample | None:
        telemetry = self.latest_telemetry
        if telemetry is None or lap.current_lap_time_ms <= 0:
            return None

        track_length_m = self.track_length_m
        if track_length_m is None or track_length_m <= 0:
            if lap.lap_distance_m <= 0:
                return None
            normalized_distance = 0.0
        else:
            normalized_distance = max(0.0, min(0.999, lap.lap_distance_m / track_length_m))

        ers_percent: float | None = None
        fuel_kg: float | None = None
        if self.latest_status is not None:
            ers_percent = max(0.0, min(100.0, self.latest_status.ers_store_energy_j / 4_000_000 * 100))
            fuel_kg = self.latest_status.fuel_in_tank_kg

        avg_slip_ratio: float | None = None
        avg_slip_angle: float | None = None
        if self.latest_motion_ex is not None:
            avg_slip_ratio = sum(abs(value) for value in self.latest_motion_ex.wheel_slip_ratio) / 4
            avg_slip_angle = sum(abs(value) for value in self.latest_motion_ex.wheel_slip_angle) / 4

        lateral_g: float | None = None
        longitudinal_g: float | None = None
        if self.latest_motion is not None:
            lateral_g = self.latest_motion.g_force_lateral
            longitudinal_g = self.latest_motion.g_force_longitudinal
            world_position = self.latest_motion.world_position
        else:
            world_position = None

        return LapSample(
            lap_time_ms=lap.current_lap_time_ms,
            lap_distance_m=lap.lap_distance_m,
            normalized_distance=normalized_distance,
            speed_kmh=telemetry.speed_kmh,
            throttle=telemetry.throttle,
            brake=telemetry.brake,
            steer=telemetry.steer,
            gear=telemetry.gear,
            engine_rpm=telemetry.engine_rpm,
            ers_percent=ers_percent,
            fuel_kg=fuel_kg,
            lateral_g=lateral_g,
            longitudinal_g=longitudinal_g,
            avg_slip_ratio=avg_slip_ratio,
            avg_slip_angle=avg_slip_angle,
            world_position=world_position,
        )

    def _live_hint(self, sample: LapSample) -> str | None:
        now = monotonic()
        reference = self.reference_profile()
        if reference is None or now - self.last_hint_at < 1.2:
            return None
        reference_sample = self._reference_sample(reference.samples, sample.normalized_distance)
        reference_time_ms = self._time_at(reference.samples, sample.normalized_distance)
        if reference_sample is None or reference_time_ms is None:
            return None

        speed_delta = sample.speed_kmh - reference_sample.speed_kmh
        throttle_delta = sample.throttle - reference_sample.throttle
        brake_delta = sample.brake - reference_sample.brake
        elapsed_delta_ms = sample.lap_time_ms - reference_time_ms
        area = self._area_name(sample.normalized_distance)
        speed_loss = abs(speed_delta)

        hint: str | None = None
        if elapsed_delta_ms > 120 and speed_delta <= -8 and sample.brake > 0.20 and brake_delta > 0.12:
            hint = (
                f"{area}: +{elapsed_delta_ms / 1000:.2f}s vs {reference.name}. "
                f"You are {speed_loss:.0f} km/h down and braking {brake_delta * 100:.0f} pts more. "
                "Work on a shorter trail-brake phase and get the car released earlier."
            )
        elif elapsed_delta_ms > 120 and speed_delta <= -8 and throttle_delta <= -0.20 and sample.brake < 0.10:
            hint = (
                f"{area}: +{elapsed_delta_ms / 1000:.2f}s vs {reference.name}. "
                f"Throttle is {abs(throttle_delta) * 100:.0f} pts later and speed is {speed_loss:.0f} km/h down. "
                "Prioritize entry rotation so you can commit to power sooner."
            )
        elif elapsed_delta_ms > 120 and speed_delta <= -8 and abs(sample.steer) > abs(reference_sample.steer) + 0.18:
            hint = (
                f"{area}: +{elapsed_delta_ms / 1000:.2f}s vs {reference.name}. "
                f"Steering input is higher while speed is {speed_loss:.0f} km/h lower. "
                "You are likely asking too much front grip mid-corner."
            )
        elif sample.avg_slip_ratio is not None and sample.avg_slip_ratio > 0.28 and sample.throttle > 0.65:
            hint = (
                f"{area}: traction loss on exit. Slip ratio is {sample.avg_slip_ratio:.2f}; "
                "unwind steering before full throttle or feed torque in more progressively."
            )

        if hint is not None:
            self.last_hint_at = now
        return hint

    def _summarize_completed_lap(self, lap: CompletedLap) -> list[str]:
        lap_time = self._format_ms(lap.lap_time_ms)
        if lap.invalid:
            return [f"Lap {lap.lap_num}: {lap_time} invalid, ignored for reference."]

        if self.best_lap is None:
            self.best_lap = lap
            self.clean_laps.append(lap)
            self._rebuild_ideal_reference()
            self.latest_insights = self.analyze_lap(lap)
            return [f"Lap {lap.lap_num}: {lap_time} clean. Set as first personal best."]

        previous_best = self.best_lap
        delta_ms = lap.lap_time_ms - previous_best.lap_time_ms
        self.clean_laps.append(lap)
        self.clean_laps = self.clean_laps[-30:]
        if delta_ms < 0:
            self.best_lap = lap
            self._rebuild_ideal_reference()
            self.latest_insights = self.analyze_lap(lap)
            return [f"Lap {lap.lap_num}: {lap_time} clean, new personal best by {self._format_delta(-delta_ms)}."]

        self._rebuild_ideal_reference()
        self.latest_insights = self.analyze_lap(lap)
        reference = self.reference_profile()
        messages = [
            f"Lap {lap.lap_num}: {lap_time} clean, {self._format_delta(delta_ms)} slower than personal best.",
            self._sector_summary(lap, previous_best),
        ]
        if reference is not None:
            reference_delta_ms = lap.lap_time_ms - reference.lap_time_ms
            messages.append(f"Reference target: {reference.name}, {self._signed_delta(reference_delta_ms)}.")
        if self.latest_insights:
            messages.append("Main losses: " + "; ".join(insight.detail for insight in self.latest_insights[:3]))
        return messages

    def _sector_summary(self, lap: CompletedLap, reference: CompletedLap) -> str:
        deltas = (
            lap.sector1_time_ms - reference.sector1_time_ms,
            lap.sector2_time_ms - reference.sector2_time_ms,
            lap.sector3_time_ms - reference.sector3_time_ms,
        )
        formatted = ", ".join(f"S{index + 1} {self._signed_delta(delta)}" for index, delta in enumerate(deltas))
        return f"Sector delta: {formatted}."

    def analyze_lap(self, lap: CompletedLap, reference: ReferenceProfile | None = None) -> list[SegmentInsight]:
        reference = reference or self.reference_profile()
        if reference is None or not lap.samples or not reference.samples:
            return []

        insights: list[SegmentInsight] = []
        segment_count = 30
        for index in range(segment_count):
            start = index / segment_count
            end = (index + 1) / segment_count
            lap_start = self._time_at(lap.samples, start)
            lap_end = self._time_at(lap.samples, end)
            ref_start = self._time_at(reference.samples, start)
            ref_end = self._time_at(reference.samples, end)
            if None in {lap_start, lap_end, ref_start, ref_end}:
                continue
            delta_ms = int((lap_end - lap_start) - (ref_end - ref_start))
            if delta_ms < 60:
                continue
            lap_segment = self._samples_between(lap.samples, start, end)
            ref_segment = self._samples_between(reference.samples, start, end)
            if not lap_segment or not ref_segment:
                continue
            insights.append(self._classify_segment(start, end, delta_ms, lap_segment, ref_segment))

        return sorted(insights, key=lambda insight: insight.time_delta_ms, reverse=True)[:8]

    def reference_profile(self) -> ReferenceProfile | None:
        if self.external_reference is not None:
            return self.external_reference
        if self.ideal_reference is not None:
            return self.ideal_reference
        if self.best_lap is not None:
            return self._profile_from_lap(self.best_lap, "Personal best", "personal-best")
        return None

    def _rebuild_ideal_reference(self) -> None:
        clean_laps = [lap for lap in self.clean_laps if not lap.invalid and len(lap.samples) >= 20]
        if not clean_laps:
            self.ideal_reference = None
            return

        by_bucket: dict[int, LapSample] = {}
        for lap in clean_laps:
            for sample in lap.samples:
                bucket = self._bucket(sample.normalized_distance)
                current = by_bucket.get(bucket)
                if current is None or sample.lap_time_ms < current.lap_time_ms:
                    by_bucket[bucket] = sample

        samples = [by_bucket[bucket] for bucket in sorted(by_bucket)]
        if len(samples) < 20:
            self.ideal_reference = None
            return

        adjusted: list[LapSample] = []
        previous_time_ms = 0
        for sample in samples:
            lap_time_ms = max(sample.lap_time_ms, previous_time_ms + 1)
            previous_time_ms = lap_time_ms
            adjusted.append(
                LapSample(
                    lap_time_ms=lap_time_ms,
                    lap_distance_m=sample.lap_distance_m,
                    normalized_distance=sample.normalized_distance,
                    speed_kmh=sample.speed_kmh,
                    throttle=sample.throttle,
                    brake=sample.brake,
                    steer=sample.steer,
                    gear=sample.gear,
                    engine_rpm=sample.engine_rpm,
                    ers_percent=sample.ers_percent,
                    fuel_kg=sample.fuel_kg,
                    lateral_g=sample.lateral_g,
                    longitudinal_g=sample.longitudinal_g,
                    avg_slip_ratio=sample.avg_slip_ratio,
                    avg_slip_angle=sample.avg_slip_angle,
                    world_position=sample.world_position,
                )
            )

        best = min(clean_laps, key=lambda lap: lap.lap_time_ms)
        lap_time_ms = self._ideal_lap_time_ms(clean_laps) or best.lap_time_ms
        self.ideal_reference = ReferenceProfile(
            name="Ideal lap",
            source="best-microsectors",
            lap_time_ms=lap_time_ms,
            sector1_time_ms=best.sector1_time_ms,
            sector2_time_ms=best.sector2_time_ms,
            samples=adjusted,
            lap_count=len(clean_laps),
            synthetic=True,
        )

    def _ideal_lap_time_ms(self, laps: list[CompletedLap]) -> int | None:
        segment_count = 30
        total = 0
        for index in range(segment_count):
            start = index / segment_count
            end = (index + 1) / segment_count
            segment_times: list[int] = []
            for lap in laps:
                start_ms = self._time_at(lap.samples, start)
                end_ms = self._time_at(lap.samples, end)
                if start_ms is not None and end_ms is not None and end_ms > start_ms:
                    segment_times.append(end_ms - start_ms)
            if not segment_times:
                return None
            total += min(segment_times)
        return total

    def _classify_segment(
        self,
        start: float,
        end: float,
        delta_ms: int,
        lap_segment: list[LapSample],
        ref_segment: list[LapSample],
    ) -> SegmentInsight:
        avg_speed_delta = self._avg(sample.speed_kmh for sample in lap_segment) - self._avg(
            sample.speed_kmh for sample in ref_segment
        )
        brake_delta = self._avg(sample.brake for sample in lap_segment) - self._avg(sample.brake for sample in ref_segment)
        throttle_delta = self._avg(sample.throttle for sample in lap_segment) - self._avg(
            sample.throttle for sample in ref_segment
        )
        min_speed_delta = min(sample.speed_kmh for sample in lap_segment) - min(sample.speed_kmh for sample in ref_segment)
        steer_delta = self._avg(abs(sample.steer) for sample in lap_segment) - self._avg(
            abs(sample.steer) for sample in ref_segment
        )
        slip_values = [sample.avg_slip_ratio for sample in lap_segment if sample.avg_slip_ratio is not None]
        avg_slip = self._avg(slip_values) if slip_values else 0.0

        severity = "high" if delta_ms >= 250 else "medium" if delta_ms >= 120 else "low"
        area = self._area_range(start, end)
        speed_loss = abs(avg_speed_delta)

        if brake_delta > 0.10 and avg_speed_delta < -5:
            category = "braking"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s under braking, "
                f"{speed_loss:.0f} km/h slower with {brake_delta * 100:.0f} pts more brake."
            )
        elif throttle_delta < -0.16 and avg_speed_delta < -5:
            category = "throttle"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s on throttle pickup, "
                f"power is {abs(throttle_delta) * 100:.0f} pts later and speed is {speed_loss:.0f} km/h down."
            )
        elif min_speed_delta < -7:
            category = "minimum-speed"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s at minimum speed, "
                f"apex speed is {abs(min_speed_delta):.0f} km/h below reference."
            )
        elif steer_delta > 0.16 and avg_speed_delta < -4:
            category = "steering"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s to steering scrub, "
                f"{steer_delta * 100:.0f} pts more steering while slower."
            )
        elif avg_slip > 0.26 and throttle_delta > -0.05:
            category = "traction"
            detail = f"{area}: lost {delta_ms / 1000:.2f}s on exit traction, average slip ratio {avg_slip:.2f}."
        else:
            category = "pace"
            detail = f"{area}: lost {delta_ms / 1000:.2f}s, average speed {speed_loss:.0f} km/h below reference."

        return SegmentInsight(
            area=area,
            start_pct=start * 100,
            end_pct=end * 100,
            time_delta_ms=delta_ms,
            speed_delta_kmh=avg_speed_delta,
            severity=severity,
            category=category,
            detail=detail,
        )

    def _reference_sample(self, samples: list[LapSample], normalized_distance: float) -> LapSample | None:
        ref_by_bucket = self._bucket_samples(samples)
        bucket = self._bucket(normalized_distance)
        for radius in range(0, 4):
            for candidate in (bucket - radius, bucket + radius):
                ref = ref_by_bucket.get(candidate)
                if ref is not None:
                    return ref
        return None

    def _bucket_samples(self, samples: list[LapSample]) -> dict[int, LapSample]:
        by_bucket: dict[int, LapSample] = {}
        for sample in samples:
            by_bucket[self._bucket(sample.normalized_distance)] = sample
        return by_bucket

    def _dedupe_samples(self, samples: list[LapSample]) -> list[LapSample]:
        return list(self._bucket_samples(samples).values())

    def _time_at(self, samples: list[LapSample], normalized_distance: float) -> int | None:
        if not samples:
            return None
        ordered = sorted(samples, key=lambda sample: sample.normalized_distance)
        if normalized_distance <= ordered[0].normalized_distance:
            return ordered[0].lap_time_ms
        for previous, current in zip(ordered, ordered[1:]):
            if previous.normalized_distance <= normalized_distance <= current.normalized_distance:
                span = current.normalized_distance - previous.normalized_distance
                if span <= 0:
                    return current.lap_time_ms
                ratio = (normalized_distance - previous.normalized_distance) / span
                return int(previous.lap_time_ms + ratio * (current.lap_time_ms - previous.lap_time_ms))
        return ordered[-1].lap_time_ms

    def _samples_between(self, samples: list[LapSample], start: float, end: float) -> list[LapSample]:
        return [sample for sample in samples if start <= sample.normalized_distance <= end]

    def _bucket(self, normalized_distance: float) -> int:
        clamped = max(0.0, min(0.999, normalized_distance))
        return int(clamped * self.sample_buckets)

    def _profile_from_lap(self, lap: CompletedLap, name: str, source: str) -> ReferenceProfile:
        return ReferenceProfile(
            name=name,
            source=source,
            lap_time_ms=lap.lap_time_ms,
            sector1_time_ms=lap.sector1_time_ms,
            sector2_time_ms=lap.sector2_time_ms,
            samples=lap.samples,
        )

    def snapshot(self) -> dict[str, Any]:
        reference = self.reference_profile()
        current_sample = self.active_samples[-1] if self.active_samples else None
        best_lap = self.best_lap
        return {
            "session": {
                "trackId": self.track_id,
                "trackLengthM": self.track_length_m,
            },
            "current": {
                "lapNum": self.active_lap_num,
                "invalid": self.active_invalid,
                "sample": self._sample_to_dict(current_sample) if current_sample else None,
                "samples": [self._sample_to_dict(sample) for sample in self.active_samples[-500:]],
            },
            "bestLap": self._lap_summary(best_lap) if best_lap else None,
            "reference": self._reference_to_dict(reference) if reference else None,
            "completedLaps": [self._lap_summary(lap) for lap in self.completed_laps[-10:]],
            "insights": [asdict(insight) for insight in self.latest_insights],
            "notices": self.notices[-12:],
        }

    def _record_notices(self, notices: list[str]) -> None:
        if notices:
            self.notices.extend(notices)
            self.notices = self.notices[-50:]

    def _lap_summary(self, lap: CompletedLap) -> dict[str, Any]:
        return {
            "lapNum": lap.lap_num,
            "lapTimeMs": lap.lap_time_ms,
            "lapTime": self._format_ms(lap.lap_time_ms),
            "sector1Ms": lap.sector1_time_ms,
            "sector2Ms": lap.sector2_time_ms,
            "sector3Ms": lap.sector3_time_ms,
            "invalid": lap.invalid,
        }

    def _reference_to_dict(self, reference: ReferenceProfile) -> dict[str, Any]:
        return {
            "name": reference.name,
            "source": reference.source,
            "lapTimeMs": reference.lap_time_ms,
            "lapTime": self._format_ms(reference.lap_time_ms),
            "sector1Ms": reference.sector1_time_ms,
            "sector2Ms": reference.sector2_time_ms,
            "sector3Ms": reference.sector3_time_ms,
            "lapCount": reference.lap_count,
            "synthetic": reference.synthetic,
            "assistProfile": reference.assist_profile,
            "samples": [self._sample_to_dict(sample) for sample in reference.samples[-700:]],
        }

    @staticmethod
    def _sample_to_dict(sample: LapSample | None) -> dict[str, Any] | None:
        if sample is None:
            return None
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

    @staticmethod
    def _avg(values: Any) -> float:
        collected = list(values)
        return sum(collected) / len(collected) if collected else 0.0

    @staticmethod
    def _area_name(normalized_distance: float) -> str:
        pct = normalized_distance * 100
        return f"Track zone {int(pct // 3.333) + 1} ({pct:.1f}% lap)"

    @staticmethod
    def _area_range(start: float, end: float) -> str:
        return f"Zone {int(start * 30) + 1} ({start * 100:.0f}-{end * 100:.0f}% lap)"

    @staticmethod
    def _format_ms(milliseconds: int) -> str:
        minutes, remainder = divmod(milliseconds, 60_000)
        seconds, ms = divmod(remainder, 1000)
        return f"{minutes}:{seconds:02d}.{ms:03d}"

    @staticmethod
    def _format_delta(milliseconds: int) -> str:
        return f"{milliseconds / 1000:.3f}s"

    @classmethod
    def _signed_delta(cls, milliseconds: int) -> str:
        sign = "+" if milliseconds >= 0 else "-"
        return f"{sign}{cls._format_delta(abs(milliseconds))}"
