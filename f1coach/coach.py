from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
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
from f1coach.track_metadata import track_name_for_id


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
    ers_deploy_mode: int | None
    ers_deployed_this_lap_j: float | None
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
    game_invalid: bool = False
    fuel_kg: float | None = None
    fuel_remaining_laps: float | None = None
    actual_tyre_compound: int | None = None
    visual_tyre_compound: int | None = None
    race_position: int | None = None
    pit_statuses: tuple[int, ...] = ()
    safety_car_statuses: tuple[int, ...] = ()
    fia_flag_statuses: tuple[int, ...] = ()

    @property
    def sector3_time_ms(self) -> int:
        sector3 = self.lap_time_ms - self.sector1_time_ms - self.sector2_time_ms
        return max(0, sector3)

    @property
    def pit_lap(self) -> bool:
        return any(status > 0 for status in self.pit_statuses)

    @property
    def safety_car_lap(self) -> bool:
        return any(status > 0 for status in self.safety_car_statuses)

    @property
    def non_green_flag_lap(self) -> bool:
        return any(status not in {0, 1} for status in self.fia_flag_statuses)


@dataclass(slots=True)
class TheoreticalSegment:
    index: int
    start_pct: float
    end_pct: float
    source_lap_num: int
    segment_time_ms: int


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
    segments: list[TheoreticalSegment] = field(default_factory=list)

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
    evidence: str
    recommendation: str
    setup_hint: str | None = None
    reference_source: str | None = None


@dataclass(frozen=True, slots=True)
class DynamicSegmentContext:
    label: str
    corner_index: int | None
    phase: str
    start_pct: float
    end_pct: float
    avg_speed_kmh: float
    min_speed_kmh: int
    avg_brake: float
    avg_throttle: float
    avg_steer: float
    speed_change_kmh: int
    elevation_change_m: float | None
    avg_slip_ratio: float | None
    ers_used_kj: float | None
    straight_after: bool


class LapCoach:
    ACTIVE_DRIVER_STATUSES = {1, 2, 3, 4}
    RESULT_STATUS_FINISHED = 3

    def __init__(
        self,
        sample_buckets: int = 240,
        external_reference: ReferenceProfile | None = None,
        driving_goal: str = "qualifying",
        corner_metadata: Any | None = None,
    ) -> None:
        self.sample_buckets = sample_buckets
        self.dashboard_sample_limit = max(1200, sample_buckets * 6)
        self.driving_goal = self._normalize_driving_goal(driving_goal)
        self.corner_metadata = corner_metadata
        self.track_length_m: int | None = None
        self.track_id: int | None = None
        self.session_type: int | None = None
        self.total_laps: int | None = None
        self.session_time_left_s: int | None = None
        self.session_expired = False
        self.session_finalized = False
        self.safety_car_status = 0
        self.latest_result_status: int | None = None
        self.race_finished = False
        self.race_finished_lap_num: int | None = None
        self.race_start_position: int | None = None
        self.latest_telemetry: CarTelemetrySnapshot | None = None
        self.latest_status: CarStatusSnapshot | None = None
        self.latest_motion: MotionSnapshot | None = None
        self.latest_motion_ex: MotionExSnapshot | None = None
        self.latest_lap: LapSnapshot | None = None
        self.active_lap_num: int | None = None
        self.completed_lap_sequence = 0
        self.active_invalid = False
        self.active_game_invalid = False
        self.active_lap_frames = 0
        self.active_invalid_frames = 0
        self.active_invalid_streak = 0
        self.active_max_invalid_streak = 0
        self.active_latest_invalid = False
        self.active_pit_statuses: set[int] = set()
        self.active_safety_car_statuses: set[int] = set()
        self.active_fia_flag_statuses: set[int] = set()
        self.active_sector1_time_ms = 0
        self.active_sector2_time_ms = 0
        self.active_samples: list[LapSample] = []
        self.best_lap: CompletedLap | None = None
        self.clean_laps: list[CompletedLap] = []
        self.external_reference = external_reference
        self.completed_laps: list[CompletedLap] = []
        self.latest_insights: list[SegmentInsight] = []
        self.latest_setup_suggestions: list[str] = []
        self.notices: list[str] = []
        self.last_hint_at = 0.0

    def set_driving_goal(self, goal: str) -> list[str]:
        normalized = self._normalize_driving_goal(goal)
        if normalized == self.driving_goal:
            return []
        self.driving_goal = normalized
        label = "qualifying laps" if normalized == "qualifying" else "race pace"
        return [f"Coaching goal set to {label}."]

    def start_new_session(
        self,
        reason: str = "New session started.",
        track_id: int | None = None,
        track_length_m: int | None = None,
        session_type: int | None = None,
        total_laps: int | None = None,
    ) -> list[str]:
        self.track_length_m = track_length_m
        self.track_id = track_id
        self.session_type = session_type
        self.total_laps = total_laps
        self.session_time_left_s = None
        self.session_expired = False
        self.session_finalized = False
        self.safety_car_status = 0
        self.latest_result_status = None
        self.race_finished = False
        self.race_finished_lap_num = None
        self.race_start_position = None
        self.latest_telemetry = None
        self.latest_status = None
        self.latest_motion = None
        self.latest_motion_ex = None
        self.latest_lap = None
        self.active_lap_num = None
        self.completed_lap_sequence = 0
        self._reset_active_lap_state()
        self.active_sector1_time_ms = 0
        self.active_sector2_time_ms = 0
        self.active_samples = []
        self.best_lap = None
        self.clean_laps = []
        self.completed_laps = []
        self.latest_insights = []
        self.latest_setup_suggestions = []
        self.last_hint_at = 0.0
        self._begin_corner_metadata_load()
        return [reason]

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
        self.session_type = message.session_type
        self.safety_car_status = message.safety_car_status
        self.session_time_left_s = message.session_time_left_s
        if message.session_time_left_s == 0:
            self.session_expired = True
        elif message.session_time_left_s is not None and message.session_time_left_s > 0:
            self.session_expired = False
            self.session_finalized = False
        incoming_total_laps = message.total_laps if message.total_laps > 0 else None
        track_changed = (
            self.track_length_m is not None
            and message.track_length_m > 0
            and (message.track_length_m != self.track_length_m or message.track_id != self.track_id)
        )
        if track_changed:
            track_label = self._track_label(message.track_id)
            notices.extend(
                self.start_new_session(
                    reason=f"New track detected: reset session for {track_label}.",
                    track_id=message.track_id,
                    track_length_m=message.track_length_m,
                    session_type=message.session_type,
                    total_laps=incoming_total_laps,
                )
            )
        if incoming_total_laps is not None:
            self.total_laps = incoming_total_laps
        if message.track_length_m > 0 and message.track_length_m != self.track_length_m:
            self.track_length_m = message.track_length_m
            self.track_id = message.track_id
            track_label = self._track_label(message.track_id)
            notices.append(
                f"Session detected: {track_label}, {message.track_length_m} m, {message.total_laps} laps."
            )
        elif track_changed:
            track_label = self._track_label(message.track_id)
            notices.append(
                f"Session detected: {track_label}, {message.track_length_m} m, {message.total_laps} laps."
            )
        self._begin_corner_metadata_load()
        return notices

    def _update_lap(self, lap: LapSnapshot) -> list[str]:
        notices: list[str] = []
        finished_result_packet = self._is_race_finished_packet(lap)
        race_finished_packet = finished_result_packet and self.driving_goal == "race"
        race_distance_finished_packet = self._is_race_distance_finished_packet(lap)
        race_continuation_packet = self._is_race_inactive_lap_packet(lap)
        qualifying_finished_packet = self._is_qualifying_final_lap_packet(lap)
        qualifying_continuation_packet = self._is_qualifying_inactive_lap_packet(lap)
        session_finished_packet = self._is_session_finished_packet(lap)
        if (
            lap.driver_status not in self.ACTIVE_DRIVER_STATUSES
            and not race_finished_packet
            and not race_distance_finished_packet
            and not race_continuation_packet
            and not qualifying_finished_packet
            and not qualifying_continuation_packet
            and not session_finished_packet
        ):
            return notices
        self.latest_lap = lap
        self.latest_result_status = lap.result_status
        self._record_race_start_position(lap)

        if self.active_lap_num is None:
            self.active_lap_num = lap.current_lap_num

        if lap.current_lap_num != self.active_lap_num:
            completed = self._complete_lap(lap)
            if completed is not None:
                notices.extend(self._record_completed_lap(completed))
            self.active_lap_num = lap.current_lap_num
            self._reset_active_lap_state()
            self.active_sector1_time_ms = 0
            self.active_sector2_time_ms = 0
            self.active_samples = []

        self._record_invalid_flag(lap.current_lap_invalid)
        self._record_lap_conditions(lap)
        self.active_invalid = self._active_lap_invalid()
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
        if race_finished_packet or race_distance_finished_packet:
            notices.extend(self._finalize_race_if_needed(lap))
        elif qualifying_finished_packet or session_finished_packet:
            notices.extend(self._finalize_timed_session_if_needed(lap))
        return notices

    def _complete_lap(self, new_lap_snapshot: LapSnapshot) -> CompletedLap | None:
        if self.active_lap_num is None or new_lap_snapshot.last_lap_time_ms <= 0:
            return None
        return self._build_completed_lap(new_lap_snapshot.last_lap_time_ms, new_lap_snapshot)

    def _build_completed_lap(self, lap_time_ms: int, lap_snapshot: LapSnapshot) -> CompletedLap:
        game_invalid = self._active_lap_game_invalid()
        self.completed_lap_sequence += 1
        return CompletedLap(
            lap_num=self.completed_lap_sequence,
            lap_time_ms=lap_time_ms,
            sector1_time_ms=self.active_sector1_time_ms,
            sector2_time_ms=self.active_sector2_time_ms,
            invalid=self._active_lap_invalid(),
            samples=self._preserve_lap_samples(self.active_samples),
            game_invalid=game_invalid,
            fuel_kg=self.latest_status.fuel_in_tank_kg if self.latest_status is not None else None,
            fuel_remaining_laps=self.latest_status.fuel_remaining_laps if self.latest_status is not None else None,
            actual_tyre_compound=self.latest_status.actual_tyre_compound if self.latest_status is not None else None,
            visual_tyre_compound=self.latest_status.visual_tyre_compound if self.latest_status is not None else None,
            race_position=lap_snapshot.car_position if lap_snapshot.car_position > 0 else None,
            pit_statuses=tuple(sorted(self.active_pit_statuses)),
            safety_car_statuses=tuple(sorted(self.active_safety_car_statuses)),
            fia_flag_statuses=tuple(sorted(self.active_fia_flag_statuses)),
        )

    def _record_completed_lap(self, completed: CompletedLap) -> list[str]:
        notices = self._summarize_completed_lap(completed)
        self.completed_laps.append(completed)
        self.completed_laps = self.completed_laps[-120:]
        return notices

    def _is_race_finished_packet(self, lap: LapSnapshot) -> bool:
        return lap.result_status == self.RESULT_STATUS_FINISHED

    def _is_qualifying_final_lap_packet(self, lap: LapSnapshot) -> bool:
        if self.session_finalized or self.driving_goal != "qualifying" or self.active_lap_num is None:
            return False
        if self._is_race_finished_packet(lap):
            return True
        if lap.current_lap_num != self.active_lap_num and self._active_lap_has_timing_evidence(lap):
            return self.session_expired or lap.driver_status not in self.ACTIVE_DRIVER_STATUSES
        if lap.driver_status in self.ACTIVE_DRIVER_STATUSES:
            return False
        return self._has_finished_lap_time(lap)

    def _is_qualifying_inactive_lap_packet(self, lap: LapSnapshot) -> bool:
        if self.session_finalized or self.driving_goal != "qualifying" or self.active_lap_num is None:
            return False
        if lap.driver_status in self.ACTIVE_DRIVER_STATUSES:
            return False
        if lap.current_lap_num != self.active_lap_num:
            return False
        return lap.current_lap_time_ms > 0 or lap.lap_distance_m > 0

    def _active_lap_has_timing_evidence(self, lap: LapSnapshot) -> bool:
        latest_sample_ms = max((sample.lap_time_ms for sample in self.active_samples), default=0)
        return lap.last_lap_time_ms > 0 or lap.current_lap_time_ms >= 10_000 or latest_sample_ms >= 10_000

    def _has_finished_lap_time(self, lap: LapSnapshot) -> bool:
        if lap.last_lap_time_ms > 0:
            if not self.completed_laps or self.completed_laps[-1].lap_time_ms != lap.last_lap_time_ms:
                return True
        if self._lap_timer_or_distance_reset_after_active_lap(lap):
            return True
        if lap.current_lap_time_ms >= 10_000 and self._lap_distance_near_finish(lap):
            return True
        return False

    def _lap_timer_or_distance_reset_after_active_lap(self, lap: LapSnapshot) -> bool:
        latest_sample_ms = max((sample.lap_time_ms for sample in self.active_samples), default=0)
        if latest_sample_ms < 10_000:
            return False
        if lap.current_lap_num != self.active_lap_num:
            return True
        if 0 <= lap.current_lap_time_ms <= 2_000 and latest_sample_ms - lap.current_lap_time_ms >= 5_000:
            return True
        if self.track_length_m is None or self.track_length_m <= 0:
            return False
        latest_distance = max((sample.lap_distance_m for sample in self.active_samples), default=0.0)
        return latest_distance >= self.track_length_m - 250.0 and 0 <= lap.lap_distance_m <= 250.0

    def _lap_distance_near_finish(self, lap: LapSnapshot) -> bool:
        if self.track_length_m is None or self.track_length_m <= 0:
            latest_distance = max((sample.lap_distance_m for sample in self.active_samples), default=0.0)
            return max(lap.lap_distance_m, latest_distance) > 0 and lap.current_lap_time_ms >= 30_000
        finish_threshold = max(0.0, self.track_length_m - 90.0)
        latest_distance = max((sample.lap_distance_m for sample in self.active_samples), default=0.0)
        return max(lap.lap_distance_m, latest_distance) >= finish_threshold

    def _is_race_distance_finished_packet(self, lap: LapSnapshot) -> bool:
        if self.race_finished or self.driving_goal != "race" or self.total_laps is None:
            return False
        if self.active_lap_num is None or len(self.completed_laps) >= self.total_laps:
            return False
        if not self._active_race_lap_is_final_expected():
            return False
        if lap.current_lap_num != self.active_lap_num and self._active_lap_has_timing_evidence(lap):
            return True
        if lap.driver_status not in self.ACTIVE_DRIVER_STATUSES:
            return self._has_finished_race_lap_time(lap)
        return False

    def _is_race_inactive_lap_packet(self, lap: LapSnapshot) -> bool:
        if self.race_finished or self.driving_goal != "race" or self.total_laps is None:
            return False
        if self.active_lap_num is None or len(self.completed_laps) >= self.total_laps:
            return False
        if lap.driver_status in self.ACTIVE_DRIVER_STATUSES:
            return False
        if lap.current_lap_num < self.active_lap_num:
            return False
        final_window = (
            self.active_lap_num >= max(1, self.total_laps - 1)
            or lap.current_lap_num >= self.total_laps
            or len(self.completed_laps) + 1 >= self.total_laps
        )
        if not final_window:
            return False
        return lap.current_lap_time_ms > 0 or lap.last_lap_time_ms > 0 or lap.lap_distance_m > 0

    def _active_race_lap_is_final_expected(self) -> bool:
        if self.total_laps is None or self.active_lap_num is None:
            return False
        if len(self.completed_laps) >= self.total_laps:
            return False
        return self.active_lap_num >= self.total_laps or len(self.completed_laps) + 1 >= self.total_laps

    def _has_finished_race_lap_time(self, lap: LapSnapshot) -> bool:
        if lap.last_lap_time_ms > 0:
            if not self.completed_laps or self.completed_laps[-1].lap_time_ms != lap.last_lap_time_ms:
                return True
        if self._race_lap_timer_or_distance_reset_after_active_lap(lap):
            return True
        if lap.current_lap_time_ms >= 10_000 and self._lap_distance_near_finish(lap):
            return True
        return False

    def _race_lap_timer_or_distance_reset_after_active_lap(self, lap: LapSnapshot) -> bool:
        latest_sample_ms = max((sample.lap_time_ms for sample in self.active_samples), default=0)
        if latest_sample_ms < 10_000:
            return False
        if lap.current_lap_num != self.active_lap_num:
            return True
        timer_reset = 0 <= lap.current_lap_time_ms <= 2_000 and latest_sample_ms - lap.current_lap_time_ms >= 5_000
        if self.track_length_m is None or self.track_length_m <= 0:
            return timer_reset and latest_sample_ms >= 30_000
        latest_distance = max((sample.lap_distance_m for sample in self.active_samples), default=0.0)
        near_finish = latest_distance >= self.track_length_m - 250.0
        distance_reset = 0 <= lap.lap_distance_m <= 250.0
        return near_finish and (timer_reset or distance_reset)

    def _record_race_start_position(self, lap: LapSnapshot) -> None:
        if self.race_start_position is not None or self.driving_goal != "race":
            return
        if lap.car_position <= 0:
            return
        if self.total_laps is not None and lap.current_lap_num > max(1, self.total_laps):
            return
        self.race_start_position = lap.car_position

    def _is_session_finished_packet(self, lap: LapSnapshot) -> bool:
        if self.session_finalized or not self.session_expired:
            return False
        if self._is_race_finished_packet(lap):
            return True
        if lap.driver_status not in self.ACTIVE_DRIVER_STATUSES:
            return self.active_lap_num is not None
        return (
            self.active_lap_num is not None
            and lap.current_lap_num != self.active_lap_num
            and lap.last_lap_time_ms > 0
        )

    def _finalize_timed_session_if_needed(self, lap: LapSnapshot) -> list[str]:
        if self.session_finalized:
            return []
        if self.active_lap_num is None:
            self.session_finalized = True
            return self._timed_session_finished_notice()

        final_lap_time_ms = self._finished_lap_time_ms(lap)
        if final_lap_time_ms is None:
            if self.completed_laps and lap.last_lap_time_ms == self.completed_laps[-1].lap_time_ms:
                self.session_finalized = True
                return self._timed_session_finished_notice()
            return []

        completed = self._build_completed_lap(final_lap_time_ms, lap)
        notices = self._record_completed_lap(completed)
        self.active_lap_num = None
        self._reset_active_lap_state()
        self.active_sector1_time_ms = 0
        self.active_sector2_time_ms = 0
        self.active_samples = []
        self.session_finalized = True
        notices.extend(self._timed_session_finished_notice())
        return notices

    def _finalize_race_if_needed(self, lap: LapSnapshot) -> list[str]:
        if self.race_finished:
            return []
        if not self._should_finalize_active_race_lap(lap):
            if self.total_laps is not None and len(self.completed_laps) < self.total_laps and self.active_lap_num is not None:
                return []
            self.race_finished = True
            self.race_finished_lap_num = self.completed_laps[-1].lap_num if self.completed_laps else None
            return self._race_finished_notice()

        final_lap_time_ms = self._finished_lap_time_ms(lap)
        if final_lap_time_ms is None:
            return []

        completed = self._build_completed_lap(final_lap_time_ms, lap)
        notices = self._record_completed_lap(completed)
        self.race_finished = True
        self.race_finished_lap_num = completed.lap_num
        self.active_lap_num = None
        self._reset_active_lap_state()
        self.active_sector1_time_ms = 0
        self.active_sector2_time_ms = 0
        self.active_samples = []
        notices.extend(self._race_finished_notice())
        return notices

    def _should_finalize_active_race_lap(self, lap: LapSnapshot) -> bool:
        if self.active_lap_num is None:
            return False
        if self.total_laps is not None:
            if len(self.completed_laps) >= self.total_laps:
                return False
            if self.active_lap_num > self.total_laps and lap.current_lap_num > self.total_laps:
                return False
        return self._finished_lap_time_ms(lap) is not None

    def _finished_lap_time_ms(self, lap: LapSnapshot) -> int | None:
        latest_sample_ms = max((sample.lap_time_ms for sample in self.active_samples), default=0)
        if lap.current_lap_time_ms >= 10_000:
            return lap.current_lap_time_ms
        one_lap_remaining = self.total_laps is not None and len(self.completed_laps) < self.total_laps
        duplicate_last_lap_time = (
            lap.last_lap_time_ms > 0
            and bool(self.completed_laps)
            and self.completed_laps[-1].lap_time_ms == lap.last_lap_time_ms
        )
        if lap.last_lap_time_ms > 0 and one_lap_remaining and not duplicate_last_lap_time:
            return lap.last_lap_time_ms
        if lap.last_lap_time_ms > 0 and not duplicate_last_lap_time:
            return lap.last_lap_time_ms
        if latest_sample_ms >= 10_000:
            return latest_sample_ms
        return None

    def _race_finished_notice(self) -> list[str]:
        lap_count = len(self.completed_laps)
        expected = f"/{self.total_laps}" if self.total_laps is not None else ""
        return [f"Race complete: {lap_count}{expected} laps captured. Race Overview finalized."]

    def _timed_session_finished_notice(self) -> list[str]:
        lap_count = len(self.completed_laps)
        return [f"Timed session complete: {lap_count} laps captured."]

    def _reset_active_lap_state(self) -> None:
        self.active_invalid = False
        self.active_game_invalid = False
        self.active_lap_frames = 0
        self.active_invalid_frames = 0
        self.active_invalid_streak = 0
        self.active_max_invalid_streak = 0
        self.active_latest_invalid = False
        self.active_pit_statuses = set()
        self.active_safety_car_statuses = set()
        self.active_fia_flag_statuses = set()

    def _record_invalid_flag(self, invalid: bool) -> None:
        self.active_lap_frames += 1
        self.active_latest_invalid = invalid
        if invalid:
            self.active_invalid_frames += 1
            self.active_invalid_streak += 1
            self.active_max_invalid_streak = max(self.active_max_invalid_streak, self.active_invalid_streak)
        else:
            self.active_invalid_streak = 0
        self.active_game_invalid = self._active_lap_game_invalid()

    def _active_lap_game_invalid(self) -> bool:
        if self.active_lap_frames == 0:
            return False
        invalid_ratio = self.active_invalid_frames / self.active_lap_frames
        return self.active_latest_invalid or self.active_max_invalid_streak >= 5 or invalid_ratio >= 0.20

    def _active_lap_invalid(self) -> bool:
        if not self._active_lap_game_invalid():
            return False
        if self._is_practice_session():
            return False
        return True

    def _record_lap_conditions(self, lap: LapSnapshot) -> None:
        if lap.pit_status > 0:
            self.active_pit_statuses.add(lap.pit_status)
        if self.safety_car_status > 0:
            self.active_safety_car_statuses.add(self.safety_car_status)
        if self.latest_status is not None and self.latest_status.vehicle_fia_flags not in {0, 1}:
            self.active_fia_flag_statuses.add(self.latest_status.vehicle_fia_flags)

    def _is_practice_session(self) -> bool:
        return self.session_type in {1, 2, 3, 4}

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
        ers_deploy_mode: int | None = None
        ers_deployed_this_lap_j: float | None = None
        fuel_kg: float | None = None
        if self.latest_status is not None:
            ers_percent = max(0.0, min(100.0, self.latest_status.ers_store_energy_j / 4_000_000 * 100))
            ers_deploy_mode = self.latest_status.ers_deploy_mode
            ers_deployed_this_lap_j = self.latest_status.ers_deployed_this_lap_j
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
            ers_deploy_mode=ers_deploy_mode,
            ers_deployed_this_lap_j=ers_deployed_this_lap_j,
            fuel_kg=fuel_kg,
            lateral_g=lateral_g,
            longitudinal_g=longitudinal_g,
            avg_slip_ratio=avg_slip_ratio,
            avg_slip_angle=avg_slip_angle,
            world_position=world_position,
        )

    def _live_hint(self, sample: LapSample) -> str | None:
        now = monotonic()
        reference = self._analysis_reference_profile()
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
            dynamic_hint = self._live_dynamic_hint(sample, hint)
            if dynamic_hint:
                hint = f"{hint} {dynamic_hint}"
            self.last_hint_at = now
        return hint

    def _summarize_completed_lap(self, lap: CompletedLap) -> list[str]:
        lap_time = self._format_ms(lap.lap_time_ms)
        if lap.invalid:
            return [f"Lap {lap.lap_num}: {lap_time} invalid, ignored for reference."]
        invalid_note = " (practice-program invalid flag ignored)" if lap.game_invalid else ""

        if self.best_lap is None:
            self.best_lap = lap
            self.clean_laps.append(lap)
            analysis_reference = self._analysis_reference_profile()
            self.latest_insights = self.analyze_lap(lap, analysis_reference)
            self.latest_setup_suggestions = self._setup_suggestions(lap, analysis_reference)
            return [f"Lap {lap.lap_num}: {lap_time} clean{invalid_note}. Set as first personal best."]

        previous_best = self.best_lap
        delta_ms = lap.lap_time_ms - previous_best.lap_time_ms
        self.clean_laps.append(lap)
        self.clean_laps = self.clean_laps[-30:]
        if delta_ms < 0:
            self.best_lap = lap
            analysis_reference = self._analysis_reference_profile()
            self.latest_insights = self.analyze_lap(lap, analysis_reference)
            self.latest_setup_suggestions = self._setup_suggestions(lap, analysis_reference)
            return [
                f"Lap {lap.lap_num}: {lap_time} clean{invalid_note}, new personal best by {self._format_delta(-delta_ms)}."
            ]

        reference = self.reference_profile()
        analysis_reference = self._analysis_reference_profile()
        self.latest_insights = self.analyze_lap(lap, analysis_reference)
        self.latest_setup_suggestions = self._setup_suggestions(lap, analysis_reference)
        messages = [
            f"Lap {lap.lap_num}: {lap_time} clean{invalid_note}, {self._format_delta(delta_ms)} slower than personal best.",
            self._sector_summary(lap, previous_best),
        ]
        if reference is not None:
            reference_delta_ms = lap.lap_time_ms - reference.lap_time_ms
            messages.append(f"Reference target: {reference.name}, {self._signed_delta(reference_delta_ms)}.")
        if self.latest_insights:
            messages.append("Focus: " + "; ".join(self._compact_tip(insight) for insight in self.latest_insights[:2]))
        return messages

    def _sector_summary(self, lap: CompletedLap, reference: CompletedLap) -> str:
        deltas = (
            lap.sector1_time_ms - reference.sector1_time_ms,
            lap.sector2_time_ms - reference.sector2_time_ms,
            lap.sector3_time_ms - reference.sector3_time_ms,
        )
        formatted = ", ".join(f"S{index + 1} {self._signed_delta(delta)}" for index, delta in enumerate(deltas))
        return f"Sector delta: {formatted}."

    @staticmethod
    def _compact_tip(insight: SegmentInsight) -> str:
        category = insight.category
        area = insight.area.split(" (", 1)[0]
        if category == "braking":
            return f"{area}: {insight.recommendation}"
        if category == "throttle":
            return f"{area}: {insight.recommendation}"
        if category == "minimum-speed":
            return f"{area}: {insight.recommendation}"
        if category == "steering":
            return f"{area}: {insight.recommendation}"
        if category == "traction":
            return f"{area}: {insight.recommendation}"
        if category == "ERS deployment":
            return f"{area}: {insight.recommendation}"
        if category == "ERS waste":
            return f"{area}: {insight.recommendation}"
        return f"{area}: {insight.recommendation}"

    def analyze_lap(self, lap: CompletedLap, reference: ReferenceProfile | None = None) -> list[SegmentInsight]:
        reference = reference or self.reference_profile()
        if reference is None or not lap.samples:
            return []
        if not reference.samples:
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
            if delta_ms < 40:
                continue
            lap_segment = self._samples_between(lap.samples, start, end)
            ref_segment = self._samples_between(reference.samples, start, end)
            if not lap_segment or not ref_segment:
                continue
            insights.append(
                self._classify_segment(
                    start,
                    end,
                    delta_ms,
                    lap_segment,
                    ref_segment,
                    self._reference_source_for(reference, start, end),
                    reference.samples,
                )
            )

        ranked = sorted(insights, key=lambda insight: insight.time_delta_ms, reverse=True)
        selected = ranked[:3]
        ers_insight = next(
            (insight for insight in ranked if insight.category in {"ERS deployment", "ERS waste"}),
            None,
        )
        if ers_insight is not None and ers_insight not in selected:
            selected = [*selected[:2], ers_insight]
        return selected

    def reference_profile(self) -> ReferenceProfile | None:
        sector_theoretical = self._sector_theoretical_reference()
        if sector_theoretical is not None:
            return sector_theoretical
        if self.external_reference is not None:
            return self.external_reference
        if self.best_lap is not None:
            return self._profile_from_lap(self.best_lap, "Personal best", "personal-best")
        return None

    def _analysis_reference_profile(self) -> ReferenceProfile | None:
        if self.external_reference is not None and self.external_reference.samples:
            return self.external_reference
        if self.best_lap is not None:
            return self._profile_from_lap(self.best_lap, "Personal best", "personal-best")
        return None

    def _sector_theoretical_reference(self) -> ReferenceProfile | None:
        if not self.clean_laps:
            return None
        sector1_lap = min(
            (lap for lap in self.clean_laps if lap.sector1_time_ms > 0),
            key=lambda lap: lap.sector1_time_ms,
            default=None,
        )
        sector2_lap = min(
            (lap for lap in self.clean_laps if lap.sector2_time_ms > 0),
            key=lambda lap: lap.sector2_time_ms,
            default=None,
        )
        sector3_lap = min(
            (lap for lap in self.clean_laps if lap.sector3_time_ms > 0),
            key=lambda lap: lap.sector3_time_ms,
            default=None,
        )
        if sector1_lap is None or sector2_lap is None or sector3_lap is None:
            return None
        sector1 = sector1_lap.sector1_time_ms
        sector2 = sector2_lap.sector2_time_ms
        sector3 = sector3_lap.sector3_time_ms
        samples = self._stitch_theoretical_samples(sector1_lap, sector2_lap, sector3_lap)
        sector_boundaries = self._canonical_sector_boundaries([sector1_lap, sector2_lap, sector3_lap])
        sector1_end = sector_boundaries[0] * 100 if sector_boundaries is not None else 33.333
        sector2_end = sector_boundaries[1] * 100 if sector_boundaries is not None else 66.667
        return ReferenceProfile(
            name="Theoretical best",
            source="sector-theoretical",
            lap_time_ms=sector1 + sector2 + sector3,
            sector1_time_ms=sector1,
            sector2_time_ms=sector2,
            samples=samples,
            lap_count=len(self.clean_laps),
            synthetic=True,
            segments=[
                TheoreticalSegment(1, 0.0, sector1_end, sector1_lap.lap_num, sector1),
                TheoreticalSegment(2, sector1_end, sector2_end, sector2_lap.lap_num, sector2),
                TheoreticalSegment(3, sector2_end, 100.0, sector3_lap.lap_num, sector3),
            ],
        )

    def _stitch_theoretical_samples(
        self,
        sector1_lap: CompletedLap,
        sector2_lap: CompletedLap,
        sector3_lap: CompletedLap,
    ) -> list[LapSample]:
        if not sector1_lap.samples or not sector2_lap.samples or not sector3_lap.samples:
            return []
        if sector1_lap is sector2_lap and sector2_lap is sector3_lap:
            return sorted(sector1_lap.samples, key=lambda sample: sample.normalized_distance)

        sector1_boundaries = self._sector_boundary_distances(sector1_lap)
        sector2_boundaries = self._sector_boundary_distances(sector2_lap)
        sector3_boundaries = self._sector_boundary_distances(sector3_lap)
        canonical_boundaries = self._canonical_sector_boundaries([sector1_lap, sector2_lap, sector3_lap])
        if (
            sector1_boundaries is None
            or sector2_boundaries is None
            or sector3_boundaries is None
            or canonical_boundaries is None
        ):
            return []

        target_sector1_end, target_sector2_end = canonical_boundaries

        stitched: list[LapSample] = []
        stitched.extend(
            self._sector_samples_for_reference(
                sector1_lap,
                source_start_distance=0.0,
                source_end_distance=sector1_boundaries[0],
                target_start_distance=0.0,
                target_end_distance=target_sector1_end,
                source_start_time_ms=0,
                target_offset_ms=0,
                sector_time_ms=sector1_lap.sector1_time_ms,
            )
        )
        stitched.extend(
            self._sector_samples_for_reference(
                sector2_lap,
                source_start_distance=sector2_boundaries[0],
                source_end_distance=sector2_boundaries[1],
                target_start_distance=target_sector1_end,
                target_end_distance=target_sector2_end,
                source_start_time_ms=sector2_lap.sector1_time_ms,
                target_offset_ms=sector1_lap.sector1_time_ms,
                sector_time_ms=sector2_lap.sector2_time_ms,
            )
        )
        stitched.extend(
            self._sector_samples_for_reference(
                sector3_lap,
                source_start_distance=sector3_boundaries[1],
                source_end_distance=sector3_boundaries[2],
                target_start_distance=target_sector2_end,
                target_end_distance=0.999,
                source_start_time_ms=sector3_lap.sector1_time_ms + sector3_lap.sector2_time_ms,
                target_offset_ms=sector1_lap.sector1_time_ms + sector2_lap.sector2_time_ms,
                sector_time_ms=sector3_lap.sector3_time_ms,
            )
        )
        if not stitched:
            return []

        by_distance: dict[float, LapSample] = {}
        for sample in sorted(stitched, key=lambda item: (item.normalized_distance, item.lap_time_ms)):
            by_distance[round(sample.normalized_distance, 6)] = sample
        ordered: list[LapSample] = []
        for sample in sorted(by_distance.values(), key=lambda item: item.normalized_distance):
            if ordered and sample.lap_time_ms < ordered[-1].lap_time_ms:
                continue
            ordered.append(sample)
        final_time = sector1_lap.sector1_time_ms + sector2_lap.sector2_time_ms + sector3_lap.sector3_time_ms
        if ordered and ordered[-1].normalized_distance < 0.998:
            ordered.append(replace(ordered[-1], normalized_distance=0.999, lap_time_ms=final_time))
        return ordered

    def _sector_samples_for_reference(
        self,
        lap: CompletedLap,
        source_start_distance: float,
        source_end_distance: float,
        target_start_distance: float,
        target_end_distance: float,
        source_start_time_ms: int,
        target_offset_ms: int,
        sector_time_ms: int,
    ) -> list[LapSample]:
        source_span = source_end_distance - source_start_distance
        target_span = target_end_distance - target_start_distance
        if source_span <= 0 or target_span <= 0:
            return []

        result: list[LapSample] = []
        start_sample = self._nearest_sample(lap.samples, source_start_distance)
        end_sample = self._nearest_sample(lap.samples, source_end_distance)
        if start_sample is not None:
            result.append(
                replace(
                    start_sample,
                    lap_time_ms=target_offset_ms,
                    lap_distance_m=self._lap_distance_for(target_start_distance, start_sample),
                    normalized_distance=target_start_distance,
                )
            )
        for sample in sorted(lap.samples, key=lambda item: item.normalized_distance):
            if source_start_distance < sample.normalized_distance < source_end_distance:
                ratio = (sample.normalized_distance - source_start_distance) / source_span
                target_distance = target_start_distance + ratio * target_span
                adjusted_time = max(0, target_offset_ms + sample.lap_time_ms - source_start_time_ms)
                result.append(
                    replace(
                        sample,
                        lap_time_ms=adjusted_time,
                        lap_distance_m=self._lap_distance_for(target_distance, sample),
                        normalized_distance=max(0.0, min(0.999, target_distance)),
                    )
                )
        if end_sample is not None:
            result.append(
                replace(
                    end_sample,
                    lap_time_ms=target_offset_ms + sector_time_ms,
                    lap_distance_m=self._lap_distance_for(target_end_distance, end_sample),
                    normalized_distance=target_end_distance,
                )
            )
        return result

    def _sector_boundary_distances(self, lap: CompletedLap) -> tuple[float, float, float] | None:
        if not lap.samples:
            return None
        sector1_end = self._distance_at_lap_time(lap.samples, lap.sector1_time_ms)
        sector2_end = self._distance_at_lap_time(lap.samples, lap.sector1_time_ms + lap.sector2_time_ms)
        if sector1_end is None or sector2_end is None:
            return None
        lap_end = max(sample.normalized_distance for sample in lap.samples)
        if sector2_end <= sector1_end or lap_end <= sector2_end:
            return None
        return (
            max(0.0, min(0.999, sector1_end)),
            max(0.0, min(0.999, sector2_end)),
            max(0.0, min(0.999, lap_end)),
        )

    def _canonical_sector_boundaries(self, laps: list[CompletedLap]) -> tuple[float, float] | None:
        boundaries = [self._sector_boundary_distances(lap) for lap in laps]
        if any(boundary is None for boundary in boundaries):
            return None
        resolved = [boundary for boundary in boundaries if boundary is not None]
        sector1_end = self._median(boundary[0] for boundary in resolved)
        sector2_end = self._median(boundary[1] for boundary in resolved)
        if sector1_end <= 0 or sector2_end <= sector1_end or sector2_end >= 0.999:
            return None
        return sector1_end, sector2_end

    @staticmethod
    def _nearest_sample(samples: list[LapSample], normalized_distance: float) -> LapSample | None:
        if not samples:
            return None
        return min(samples, key=lambda sample: abs(sample.normalized_distance - normalized_distance))

    def _lap_distance_for(self, normalized_distance: float, fallback: LapSample) -> float:
        if self.track_length_m is not None:
            return normalized_distance * self.track_length_m
        return fallback.lap_distance_m

    def _classify_segment(
        self,
        start: float,
        end: float,
        delta_ms: int,
        lap_segment: list[LapSample],
        ref_segment: list[LapSample],
        reference_source: str | None = None,
        reference_samples: list[LapSample] | None = None,
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
        ers_used_lap = self._ers_used(lap_segment)
        ers_used_ref = self._ers_used(ref_segment)
        ers_delta_kj = None
        if ers_used_lap is not None and ers_used_ref is not None:
            ers_delta_kj = (ers_used_lap - ers_used_ref) / 1000
        exit_phase = self._is_exit_segment(lap_segment)
        entry_phase = self._is_entry_segment(lap_segment)
        avg_throttle = self._avg(sample.throttle for sample in lap_segment)
        avg_brake = self._avg(sample.brake for sample in lap_segment)
        avg_steer = self._avg(abs(sample.steer) for sample in lap_segment)

        severity = "high" if delta_ms >= 250 else "medium" if delta_ms >= 120 else "low"
        context = self._dynamic_segment_context(start, end, lap_segment, ref_segment, reference_samples)
        area = context.label
        speed_loss = abs(avg_speed_delta)
        evidence_parts = [
            f"{delta_ms / 1000:.2f}s lost",
            f"{avg_speed_delta:.0f} km/h avg speed delta",
            f"brake {brake_delta * 100:+.0f} pts",
            f"throttle {throttle_delta * 100:+.0f} pts",
        ]
        if ers_delta_kj is not None:
            evidence_parts.append(f"ERS {ers_delta_kj:+.0f} kJ")
        evidence = ", ".join(evidence_parts)

        full_throttle_ers_zone = avg_throttle > 0.80 and avg_brake < 0.08 and avg_slip < 0.24
        if (
            ers_delta_kj is not None
            and ers_delta_kj < -8
            and avg_speed_delta < -2
            and (exit_phase or full_throttle_ers_zone)
        ):
            category = "ERS deployment"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s with {abs(ers_delta_kj):.0f} kJ less ERS deployed."
            )
            recommendation = (
                "Use more deploy as the car is straightening and traction is stable; avoid saving battery through the "
                "first half of the following straight unless you are protecting charge for a longer DRS run."
            )
            setup_hint = None
        elif ers_delta_kj is not None and ers_delta_kj > 25 and (avg_brake > 0.12 or avg_throttle < 0.50):
            category = "ERS waste"
            detail = f"{area}: ERS use is front-loaded into a braking or partial-throttle phase."
            recommendation = (
                "Move deployment later, after rotation and before the longest acceleration zone. You are spending charge "
                "where the tyres or brakes are limiting acceleration."
            )
            setup_hint = None
        elif brake_delta > 0.10 and avg_speed_delta < -5:
            category = "braking"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s under braking, "
                f"{speed_loss:.0f} km/h slower with {brake_delta * 100:.0f} pts more brake."
            )
            recommendation = (
                "Brake to the same peak if needed, but bleed pressure earlier and target a cleaner release at turn-in. "
                "If lockups appear in this zone, move brake bias rearward one click or reduce brake pressure."
            )
            setup_hint = "Repeated front locking here points to too much forward brake bias; rear instability points the other way."
        elif throttle_delta < -0.16 and avg_speed_delta < -5:
            category = "throttle"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s on throttle pickup, "
                f"power is {abs(throttle_delta) * 100:.0f} pts later and speed is {speed_loss:.0f} km/h down."
            )
            recommendation = (
                "Open steering earlier and start with maintenance throttle before full commitment. If traction control is off, "
                "treat the first 20-40% throttle as the rotation stabilizer."
            )
            setup_hint = "If this repeats with high slip, try a lower on-throttle differential or slightly softer rear roll stiffness."
        elif min_speed_delta < -7:
            category = "minimum-speed"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s at minimum speed, "
                f"apex speed is {abs(min_speed_delta):.0f} km/h below reference."
            )
            recommendation = (
                "Carry a touch less brake into the final third of entry and let the car roll more speed at apex. "
                "Do not fix this by adding earlier throttle if the car is still under-rotated."
            )
            setup_hint = "Persistent low minimum speed with stable rear can justify more front wing or a slightly more open off-throttle diff."
        elif steer_delta > 0.16 and avg_speed_delta < -4:
            category = "steering"
            detail = (
                f"{area}: lost {delta_ms / 1000:.2f}s to steering scrub, "
                f"{steer_delta * 100:.0f} pts more steering while slower."
            )
            recommendation = (
                "Use a shallower entry or delay initial lock so the peak steering angle arrives closer to apex. "
                "The current trace suggests under-rotation followed by extra steering demand."
            )
            setup_hint = "If this appears in several medium/high-speed corners, consider more front wing or less front tyre pressure."
        elif avg_slip > 0.26 and throttle_delta > -0.05:
            category = "traction"
            detail = f"{area}: lost {delta_ms / 1000:.2f}s on exit traction, average slip ratio {avg_slip:.2f}."
            recommendation = (
                "Squeeze throttle against steering unwind rather than pedal position alone. Full throttle should arrive when "
                "steering lock is already falling, not while it is still increasing."
            )
            setup_hint = "Repeated exit slip suggests lowering on-throttle differential, softening rear suspension/ARB, or adding rear wing."
        elif entry_phase and brake_delta < -0.08 and min_speed_delta < -5:
            category = "under-braking"
            detail = f"{area}: entry speed is being shed late; minimum speed ends {abs(min_speed_delta):.0f} km/h below reference."
            recommendation = (
                "Brake a fraction earlier with a cleaner initial hit, then release before apex. The trace looks like entry commitment "
                "is arriving late and forcing a compromised mid-corner."
            )
            setup_hint = None
        else:
            category = "pace"
            detail = f"{area}: lost {delta_ms / 1000:.2f}s, average speed {speed_loss:.0f} km/h below reference."
            recommendation = "Clean up this section before setup changes."
            setup_hint = None

        recommendation = self._concise_recommendation(
            category,
            context,
            avg_speed_delta,
            brake_delta,
            throttle_delta,
            min_speed_delta,
            steer_delta,
            ers_delta_kj,
            avg_slip,
        )
        dynamic_setup_hint = self._dynamic_setup_hint(category, context)
        if setup_hint is None:
            setup_hint = dynamic_setup_hint
        elif dynamic_setup_hint is not None and category in {"braking", "traction", "steering"}:
            setup_hint = f"{setup_hint} {dynamic_setup_hint}"

        return SegmentInsight(
            area=area,
            start_pct=start * 100,
            end_pct=end * 100,
            time_delta_ms=delta_ms,
            speed_delta_kmh=avg_speed_delta,
            severity=severity,
            category=category,
            detail=detail,
            evidence=evidence,
            recommendation=recommendation,
            setup_hint=setup_hint,
            reference_source=reference_source,
        )

    def _concise_recommendation(
        self,
        category: str,
        context: DynamicSegmentContext,
        avg_speed_delta: float,
        brake_delta: float,
        throttle_delta: float,
        min_speed_delta: float,
        steer_delta: float,
        ers_delta_kj: float | None,
        avg_slip: float,
    ) -> str:
        target = self._tip_target(context)
        speed_loss = abs(avg_speed_delta)
        min_loss = abs(min_speed_delta)

        if category == "braking":
            if context.phase == "entry":
                return f"Brake 5-10 m earlier into {target}, then release sooner for exit."
            return f"Shorten the brake release at {target}; you are {speed_loss:.0f} km/h down."
        if category == "under-braking":
            return f"Brake earlier into {target}; the late slowdown is killing minimum speed."
        if category == "throttle":
            return f"Rotate earlier at {target} so throttle starts sooner on exit."
        if category == "minimum-speed":
            return f"Release brake earlier into {target}; protect {min_loss:.0f} km/h more apex speed."
        if category == "steering":
            if steer_delta > 0.22:
                return f"Use one calmer steering input at {target}; extra lock is scrubbing speed."
            return f"Open the wheel earlier at {target} before asking for throttle."
        if category == "traction":
            if avg_slip > 0.32:
                return f"Short-shift at {target} and wait for steering unwind before full throttle."
            return f"Delay full throttle at {target} until the wheel is opening."
        if category == "ERS deployment":
            straight = self._ers_target(context)
            used = f" {abs(ers_delta_kj):.0f} kJ" if ers_delta_kj is not None else ""
            return f"Use more ERS on {straight}; you left{used} on the table here."
        if category == "ERS waste":
            return f"Save ERS through {target}; spend it after the next clean exit."

        if context.phase == "entry":
            return f"Brake earlier and cleaner into {target}; the loss starts before apex."
        if context.phase == "exit":
            return f"Prioritize exit at {target}; open steering before chasing throttle."
        if context.phase == "mid-corner":
            return f"Reduce mid-corner scrub at {target}; enter calmer and use less extra lock."
        if context.phase == "straight":
            return f"Use full throttle and ERS earlier on {target}; this is straight-line loss."
        return f"Clean up {target}; the loss is spread across brake, throttle, and speed."

    @staticmethod
    def _tip_target(context: DynamicSegmentContext) -> str:
        label = context.label.split(" (", 1)[0]
        for suffix in (" entry", " exit", " mid-corner", " transition"):
            if label.endswith(suffix):
                return label[: -len(suffix)]
        return label

    def _ers_target(self, context: DynamicSegmentContext) -> str:
        target = self._tip_target(context)
        if target.startswith("Straight after "):
            return target[0].lower() + target[1:]
        if context.straight_after:
            return f"the straight after {target}"
        return target

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
        return self._ordered_lap_samples(list(self._bucket_samples(samples).values()))

    def _preserve_lap_samples(self, samples: list[LapSample]) -> list[LapSample]:
        ordered = self._ordered_lap_samples(samples)
        if len(ordered) <= self.dashboard_sample_limit:
            return ordered

        buckets: dict[int, list[LapSample]] = {}
        for sample in ordered:
            buckets.setdefault(self._bucket(sample.normalized_distance), []).append(sample)

        selected: dict[int, LapSample] = {}

        def keep(sample: LapSample | None) -> None:
            if sample is not None:
                selected[id(sample)] = sample

        for bucket in sorted(buckets):
            group = buckets[bucket]
            keep(group[0])
            keep(group[-1])
            keep(max(group, key=lambda sample: sample.brake))
            keep(max(group, key=lambda sample: sample.throttle))
            keep(min(group, key=lambda sample: sample.speed_kmh))
            keep(max(group, key=lambda sample: abs(sample.steer)))

        return self._ordered_lap_samples(list(selected.values()))

    def _samples_for_dashboard(self, samples: list[LapSample]) -> list[LapSample]:
        return self._preserve_lap_samples(samples)

    @staticmethod
    def _ordered_lap_samples(samples: list[LapSample]) -> list[LapSample]:
        return sorted(samples, key=lambda sample: (sample.normalized_distance, sample.lap_time_ms))

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

    def _distance_at_lap_time(self, samples: list[LapSample], lap_time_ms: int) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples, key=lambda sample: sample.lap_time_ms)
        if lap_time_ms <= ordered[0].lap_time_ms:
            return ordered[0].normalized_distance
        for previous, current in zip(ordered, ordered[1:]):
            if previous.lap_time_ms <= lap_time_ms <= current.lap_time_ms:
                span = current.lap_time_ms - previous.lap_time_ms
                if span <= 0:
                    return current.normalized_distance
                ratio = (lap_time_ms - previous.lap_time_ms) / span
                return previous.normalized_distance + ratio * (current.normalized_distance - previous.normalized_distance)
        return ordered[-1].normalized_distance

    def _samples_between(self, samples: list[LapSample], start: float, end: float) -> list[LapSample]:
        return [sample for sample in samples if start <= sample.normalized_distance <= end]

    def _setup_suggestions(self, lap: CompletedLap, reference: ReferenceProfile | None) -> list[str]:
        if reference is None or not lap.samples or not reference.samples:
            return []
        insights = self.latest_insights
        categories = {insight.category: 0 for insight in insights}
        for insight in insights:
            categories[insight.category] = categories.get(insight.category, 0) + 1

        lap_slip = self._avg(
            sample.avg_slip_ratio for sample in lap.samples if sample.avg_slip_ratio is not None and sample.throttle > 0.55
        )
        lap_steer = self._avg(abs(sample.steer) for sample in lap.samples if sample.speed_kmh > 120)
        ref_steer = self._avg(abs(sample.steer) for sample in reference.samples if sample.speed_kmh > 120)
        overview = self._lap_overview(lap)
        overlap = float(overview.get("brakeThrottleOverlapPct") or 0.0)
        steering_throttle = float(overview.get("steeringThrottlePct") or 0.0)

        suggestions: list[str] = []
        if categories.get("traction", 0) >= 2 or lap_slip > 0.25:
            suggestions.append("Setup: repeated exit slip. Try lower on-throttle diff or one click more rear wing.")
        if categories.get("steering", 0) >= 2 or lap_steer > ref_steer + 0.12:
            suggestions.append("Setup: high steering demand. Try more front wing or a more open off-throttle diff.")
        if categories.get("braking", 0) >= 2:
            suggestions.append("Setup: repeated braking loss. If fronts lock, move bias rearward one click.")
        if categories.get("ERS deployment", 0) >= 2:
            suggestions.append("Energy: deploy earlier on exits that lead onto long full-throttle sections.")
        suggestions.extend(self._dynamic_setup_suggestions(insights, lap, reference))
        if not suggestions:
            if lap_slip > 0.20:
                suggestions.append("Setup watch: rear slip is elevated, but confirm with another comparable lap before changing diff or rear wing.")
            if lap_steer > ref_steer + 0.08:
                suggestions.append("Setup watch: steering demand is higher than the reference; first confirm line and entry speed before adding front support.")
            if overlap >= 6.0:
                suggestions.append("Balance signal: pedal overlap is high enough to mask setup feel; clean brake release before changing bias.")
            if steering_throttle >= 12.0:
                suggestions.append("Traction signal: high throttle with steering lock is showing up; fix exit shape before softening the rear.")
        if not suggestions:
            suggestions.append("Setup: no clear setup change from this lap. Keep the baseline stable and use Focus Stack for driving inputs.")
        if self.driving_goal == "race" and suggestions:
            suggestions.append("Race pace: choose the fix that stays stable over tyre life.")
        elif self.driving_goal == "qualifying" and suggestions:
            suggestions.append("Qualifying: prioritize the biggest exit loss before the next straight.")
        return suggestions[:3]

    def _reference_source_for(self, reference: ReferenceProfile, start: float, end: float) -> str | None:
        if not reference.segments:
            return None
        start_pct = start * 100
        end_pct = end * 100
        overlapping = [
            segment
            for segment in reference.segments
            if segment.start_pct < end_pct and segment.end_pct > start_pct
        ]
        if not overlapping:
            return None
        source_laps = sorted({segment.source_lap_num for segment in overlapping})
        if len(source_laps) == 1:
            return f"reference segment from lap {source_laps[0]}"
        return "reference segments from laps " + ", ".join(str(lap_num) for lap_num in source_laps[:3])

    def _ers_used(self, samples: list[LapSample]) -> float | None:
        values = [sample.ers_deployed_this_lap_j for sample in samples if sample.ers_deployed_this_lap_j is not None]
        if len(values) < 2:
            return None
        used = values[-1] - values[0]
        return used if used >= 0 else None

    def _is_exit_segment(self, samples: list[LapSample]) -> bool:
        if len(samples) < 2:
            return False
        start_speed = samples[0].speed_kmh
        end_speed = samples[-1].speed_kmh
        avg_throttle = self._avg(sample.throttle for sample in samples)
        avg_brake = self._avg(sample.brake for sample in samples)
        return end_speed - start_speed > 18 and avg_throttle > 0.55 and avg_brake < 0.12

    def _is_entry_segment(self, samples: list[LapSample]) -> bool:
        if len(samples) < 2:
            return False
        start_speed = samples[0].speed_kmh
        end_speed = samples[-1].speed_kmh
        avg_brake = self._avg(sample.brake for sample in samples)
        return start_speed - end_speed > 18 and avg_brake > 0.12

    @staticmethod
    def _clone_sample(sample: LapSample, lap_time_ms: int) -> LapSample:
        return LapSample(
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
            ers_deploy_mode=sample.ers_deploy_mode,
            ers_deployed_this_lap_j=sample.ers_deployed_this_lap_j,
            fuel_kg=sample.fuel_kg,
            lateral_g=sample.lateral_g,
            longitudinal_g=sample.longitudinal_g,
            avg_slip_ratio=sample.avg_slip_ratio,
            avg_slip_angle=sample.avg_slip_angle,
            world_position=sample.world_position,
        )

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
        analysis_reference = self._analysis_reference_profile()
        current_sample = self._live_sample() or (self.active_samples[-1] if self.active_samples else None)
        current_samples = self._dedupe_samples(self._current_samples_with_live(current_sample))
        best_lap = self.best_lap
        map_source, map_samples = self._static_map_samples(reference)
        return {
            "session": {
                "trackId": self.track_id,
                "trackName": track_name_for_id(self.track_id),
                "trackLengthM": self.track_length_m,
                "sessionType": self.session_type,
                "totalLaps": self.total_laps,
                "sessionTimeLeftS": self.session_time_left_s,
                "sessionExpired": self.session_expired,
                "raceStartPosition": self.race_start_position,
                "safetyCarStatus": self.safety_car_status,
                "resultStatus": self.latest_result_status,
                "raceFinished": self.race_finished,
                "raceFinishedLapNum": self.race_finished_lap_num,
            },
            "cornerMetadata": self._corner_metadata_status(),
            "drivingGoal": self.driving_goal,
            "current": {
                "lapNum": self.active_lap_num,
                "invalid": self.active_invalid,
                "gameInvalid": self.active_game_invalid,
                "telemetry": self._telemetry_to_dict(self.latest_telemetry),
                "sample": self._sample_to_dict(current_sample) if current_sample else None,
                "samples": [self._sample_to_dict(sample) for sample in current_samples[-500:]],
            },
            "trackMap": {
                "source": map_source,
                "samples": [self._sample_to_dict(sample) for sample in self._samples_for_dashboard(map_samples)],
            },
            "bestLap": self._lap_summary(best_lap, reference, analysis_reference=analysis_reference) if best_lap else None,
            "reference": self._reference_to_dict(reference) if reference else None,
            "completedLaps": [
                self._lap_summary(
                    lap,
                    reference,
                    include_samples=True,
                    analysis_reference=analysis_reference,
                )
                for lap in self.completed_laps[-20:]
            ],
            "raceReview": self._race_review(reference),
            "insights": [asdict(insight) for insight in self.latest_insights],
            "setupInsights": self.latest_setup_suggestions,
            "notices": self.notices[-12:],
        }

    def _record_notices(self, notices: list[str]) -> None:
        if notices:
            self.notices.extend(notices)
            self.notices = self.notices[-50:]

    def _corner_metadata_status(self) -> dict[str, Any]:
        metadata_fn = getattr(self.corner_metadata, "metadata", None)
        error_fn = getattr(self.corner_metadata, "error", None)
        metadata = metadata_fn(self.track_id) if metadata_fn is not None else None
        error = error_fn(self.track_id) if error_fn is not None else None
        return {
            "available": metadata is not None,
            "source": "FastF1" if metadata is not None else None,
            "eventName": getattr(metadata, "event_name", None),
            "cornerCount": len(getattr(metadata, "corners", ()) or ()),
            "error": error,
        }

    def _lap_summary(
        self,
        lap: CompletedLap,
        reference: ReferenceProfile | None = None,
        include_samples: bool = False,
        analysis_reference: ReferenceProfile | None = None,
        include_insights: bool = True,
    ) -> dict[str, Any]:
        if reference is not None and reference.source == "sector-theoretical":
            insight_reference = reference
        else:
            insight_reference = analysis_reference or (reference if reference is not None and reference.samples else None)
        summary: dict[str, Any] = {
            "lapNum": lap.lap_num,
            "lapTimeMs": lap.lap_time_ms,
            "lapTime": self._format_ms(lap.lap_time_ms),
            "sector1Ms": lap.sector1_time_ms,
            "sector2Ms": lap.sector2_time_ms,
            "sector3Ms": lap.sector3_time_ms,
            "deltaToReferenceMs": self._lap_reference_delta(lap, reference),
            "ersUsedKj": self._lap_ers_used_kj(lap),
            "fuelKg": round(lap.fuel_kg, 2) if lap.fuel_kg is not None else None,
            "fuelRemainingLaps": round(lap.fuel_remaining_laps, 2) if lap.fuel_remaining_laps is not None else None,
            "actualTyreCompound": lap.actual_tyre_compound,
            "visualTyreCompound": lap.visual_tyre_compound,
            "tyreCompound": self._tyre_label(lap.visual_tyre_compound, lap.actual_tyre_compound),
            "position": lap.race_position,
            "pitLap": lap.pit_lap,
            "pitStatuses": list(lap.pit_statuses),
            "safetyCarLap": lap.safety_car_lap,
            "safetyCarStatuses": list(lap.safety_car_statuses),
            "nonGreenFlagLap": lap.non_green_flag_lap,
            "fiaFlagStatuses": list(lap.fia_flag_statuses),
            "overview": self._lap_overview(lap),
            "overviewNotes": self._lap_overview_notes(lap),
            "invalid": lap.invalid,
            "gameInvalid": lap.game_invalid,
            "insights": [
                asdict(insight) for insight in self.analyze_lap(lap, insight_reference)
            ]
            if include_insights
            else [],
        }
        if include_samples:
            summary["samples"] = [self._sample_to_dict(sample) for sample in self._samples_for_dashboard(lap.samples)]
        return summary

    def _race_review(self, reference: ReferenceProfile | None) -> dict[str, Any]:
        laps = self.completed_laps[-120:]
        if not laps:
            return {
                "status": "waiting",
                "summary": {
                    "totalLaps": 0,
                    "expectedLaps": self.total_laps,
                    "lapsRemaining": self.total_laps,
                    "raceComplete": self.race_finished,
                    "startPosition": self.race_start_position,
                    "finishPosition": None,
                    "netPositionDelta": None,
                    "cleanLaps": 0,
                    "invalidLaps": 0,
                    "raceTime": "--",
                    "bestLap": None,
                    "averageLapTime": "--",
                    "consistency": "--",
                    "trend": "--",
                },
                "powerRanking": {
                    "score": None,
                    "label": "No rating",
                    "confidence": "Waiting for laps",
                    "explanation": "Complete laps to build a race power ranking.",
                },
                "factors": [],
                "phaseBreakdown": [],
                "sectorTrend": [],
                "standoutLaps": [],
                "riskRegister": [],
                "recommendations": [],
                "trends": self._empty_race_trends(),
                "funStats": [],
                "lapTable": [],
            }

        rows = self._race_lap_rows(laps, reference)
        position_summary = self._race_position_summary(rows)
        clean_laps = [lap for lap in laps if not lap.invalid]
        scored_lap_nums = {row["lapNum"] for row in rows if row.get("rankingEligible")}
        scored_laps = [lap for lap in laps if lap.lap_num in scored_lap_nums]
        scored_times = [lap.lap_time_ms for lap in scored_laps]
        best_lap = min(scored_laps or clean_laps, key=lambda lap: lap.lap_time_ms, default=None)
        total_ms = sum(lap.lap_time_ms for lap in laps)
        avg_lap_ms = round(self._avg(scored_times)) if scored_times else None
        stdev_ms = round(self._stddev(scored_times)) if len(scored_times) >= 2 else None
        volatility_ms = self._lap_time_volatility(scored_laps)
        longest_scored_streak = self._longest_scored_streak(rows)
        valid_pct = round(len(clean_laps) / len(laps) * 100, 1) if laps else 0.0
        scored_pct = round(len(scored_laps) / len(laps) * 100, 1) if laps else 0.0
        avg_delta_to_best = None
        if best_lap is not None and scored_laps:
            avg_delta_to_best = round(self._avg(lap.lap_time_ms - best_lap.lap_time_ms for lap in scored_laps))
        avg_delta_to_reference = None
        if reference is not None and scored_laps:
            avg_delta_to_reference = round(self._avg(lap.lap_time_ms - reference.lap_time_ms for lap in scored_laps))

        phases = self._race_phase_breakdown(scored_laps, reference)
        opening_avg_ms = phases[0]["averageLapTimeMs"] if phases else None
        closing_avg_ms = phases[-1]["averageLapTimeMs"] if len(phases) >= 2 else None
        trend_ms = (
            int(closing_avg_ms - opening_avg_ms)
            if opening_avg_ms is not None and closing_avg_ms is not None
            else None
        )
        factors = self._race_power_factors(
            laps,
            rows,
            scored_laps,
            reference,
            avg_delta_to_best,
            stdev_ms,
            volatility_ms,
            trend_ms,
            longest_scored_streak,
            position_summary,
        )
        score = self._weighted_power_score(factors)
        label = self._power_label(score)
        confidence = self._power_confidence(len(scored_laps))
        risk_register = self._race_risk_register(rows, scored_laps, stdev_ms, volatility_ms, trend_ms)
        recommendations = self._race_recommendations(factors, risk_register, phases, reference)
        trends = self._race_trends(rows)
        fun_stats = self._race_fun_stats(rows, phases, longest_scored_streak, position_summary)

        return {
            "status": "complete" if self.race_finished else "ready" if scored_laps else "needs-representative-lap",
            "summary": {
                "totalLaps": len(laps),
                "expectedLaps": self.total_laps,
                "lapsRemaining": max(0, self.total_laps - len(laps)) if self.total_laps is not None else None,
                "raceComplete": self.race_finished,
                "startPosition": position_summary.get("startPosition"),
                "finishPosition": position_summary.get("finishPosition"),
                "netPositionDelta": position_summary.get("netPositionDelta"),
                "cleanLaps": len(clean_laps),
                "scoredLaps": len(scored_laps),
                "excludedLaps": len(laps) - len(scored_laps),
                "invalidLaps": len(laps) - len(clean_laps),
                "practiceFlaggedLaps": sum(1 for lap in laps if lap.game_invalid and not lap.invalid),
                "validPct": valid_pct,
                "scoredPct": scored_pct,
                "raceTimeMs": total_ms,
                "raceTime": self._format_duration_ms(total_ms),
                "bestLap": self._race_best_lap_summary(best_lap, reference),
                "averageLapTimeMs": avg_lap_ms,
                "averageLapTime": self._format_ms(avg_lap_ms) if avg_lap_ms is not None else "--",
                "averageDeltaToBestMs": avg_delta_to_best,
                "averageDeltaToReferenceMs": avg_delta_to_reference,
                "consistencyMs": stdev_ms,
                "consistency": f"{stdev_ms / 1000:.2f}s stdev" if stdev_ms is not None else "--",
                "volatilityMs": volatility_ms,
                "volatility": f"{volatility_ms / 1000:.2f}s lap-to-lap" if volatility_ms is not None else "--",
                "longestCleanStreak": self._longest_clean_streak(laps),
                "longestScoredStreak": longest_scored_streak,
                "trendMs": trend_ms,
                "trend": self._race_trend_label(trend_ms),
            },
            "powerRanking": {
                "score": score,
                "label": label,
                "confidence": confidence,
                "explanation": self._power_explanation(score, factors, len(scored_laps)),
            },
            "factors": factors,
            "phaseBreakdown": phases,
            "sectorTrend": self._race_sector_trend(scored_laps, reference),
            "standoutLaps": self._race_standout_laps(scored_laps, rows, reference),
            "riskRegister": risk_register,
            "recommendations": recommendations,
            "trends": trends,
            "funStats": fun_stats,
            "lapTable": rows,
        }

    def _race_lap_rows(
        self,
        laps: list[CompletedLap],
        reference: ReferenceProfile | None,
    ) -> list[dict[str, Any]]:
        clean_laps = [lap for lap in laps if not lap.invalid]
        best_lap = min(clean_laps, key=lambda lap: lap.lap_time_ms, default=None)
        rows: list[dict[str, Any]] = []
        previous_clean: CompletedLap | None = None
        for lap in laps:
            overview = self._lap_overview(lap)
            notes = self._lap_overview_notes(lap)
            exclusion_reason = self._race_lap_exclusion_reason(lap)
            delta_to_best = lap.lap_time_ms - best_lap.lap_time_ms if best_lap is not None else None
            delta_to_previous = (
                lap.lap_time_ms - previous_clean.lap_time_ms
                if previous_clean is not None and not lap.invalid
                else None
            )
            row = {
                "lapNum": lap.lap_num,
                "lapTimeMs": lap.lap_time_ms,
                "lapTime": self._format_ms(lap.lap_time_ms),
                "status": "Invalid" if lap.invalid else "Practice flag" if lap.game_invalid else "Clean",
                "rankingEligible": exclusion_reason is None,
                "exclusionReason": exclusion_reason,
                "deltaToBestMs": delta_to_best,
                "deltaToReferenceMs": self._lap_reference_delta(lap, reference),
                "deltaToPreviousCleanMs": delta_to_previous,
                "sector1": self._format_ms(lap.sector1_time_ms) if lap.sector1_time_ms else "--",
                "sector2": self._format_ms(lap.sector2_time_ms) if lap.sector2_time_ms else "--",
                "sector3": self._format_ms(lap.sector3_time_ms) if lap.sector3_time_ms else "--",
                "controlScore": overview.get("controlScore"),
                "avgSpeedKmh": overview.get("avgSpeedKmh"),
                "topSpeedKmh": overview.get("topSpeedKmh"),
                "fullThrottlePct": overview.get("fullThrottlePct"),
                "brakingPct": overview.get("brakingPct"),
                "coastPct": overview.get("coastPct"),
                "overlapPct": overview.get("brakeThrottleOverlapPct"),
                "steeringThrottlePct": overview.get("steeringThrottlePct"),
                "highSlipPct": overview.get("highSlipPct"),
                "avgSlipRatio": overview.get("avgSlipRatio"),
                "ersUsedKj": self._lap_ers_used_kj(lap),
                "fuelKg": round(lap.fuel_kg, 2) if lap.fuel_kg is not None else None,
                "fuelRemainingLaps": round(lap.fuel_remaining_laps, 2)
                if lap.fuel_remaining_laps is not None
                else None,
                "tyreCompound": self._tyre_label(lap.visual_tyre_compound, lap.actual_tyre_compound),
                "position": lap.race_position,
                "pitLap": lap.pit_lap,
                "safetyCarLap": lap.safety_car_lap,
                "nonGreenFlagLap": lap.non_green_flag_lap,
                "note": exclusion_reason or (notes[0] if notes else ""),
            }
            rows.append(row)
            if not lap.invalid:
                previous_clean = lap
        self._mark_non_representative_pace_outliers(rows)
        return rows

    @staticmethod
    def _empty_race_trends() -> dict[str, list[dict[str, Any]]]:
        return {
            "pace": [],
            "position": [],
            "control": [],
            "energy": [],
        }

    def _race_trends(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        trends = self._empty_race_trends()
        if self.race_start_position is not None:
            trends["position"].append(
                {
                    "lapNum": 0,
                    "position": self.race_start_position,
                    "rankingEligible": False,
                    "label": "Start",
                }
            )
        for row in rows:
            lap_num = row["lapNum"]
            trends["pace"].append(
                {
                    "lapNum": lap_num,
                    "lapTimeMs": row.get("lapTimeMs"),
                    "deltaToBestMs": row.get("deltaToBestMs"),
                    "deltaToPreviousCleanMs": row.get("deltaToPreviousCleanMs"),
                    "rankingEligible": row.get("rankingEligible"),
                    "status": row.get("status"),
                }
            )
            if row.get("position") is not None:
                trends["position"].append(
                    {
                        "lapNum": lap_num,
                        "position": row.get("position"),
                        "rankingEligible": row.get("rankingEligible"),
                    }
                )
            trends["control"].append(
                {
                    "lapNum": lap_num,
                    "controlScore": row.get("controlScore"),
                    "highSlipPct": row.get("highSlipPct"),
                    "avgSlipRatio": row.get("avgSlipRatio"),
                    "overlapPct": row.get("overlapPct"),
                    "steeringThrottlePct": row.get("steeringThrottlePct"),
                }
            )
            trends["energy"].append(
                {
                    "lapNum": lap_num,
                    "ersUsedKj": row.get("ersUsedKj"),
                    "fuelKg": row.get("fuelKg"),
                    "fuelRemainingLaps": row.get("fuelRemainingLaps"),
                    "tyreCompound": row.get("tyreCompound"),
                }
            )
        return trends

    def _race_position_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        position_rows = [row for row in rows if row.get("position") is not None]
        if not position_rows and self.race_start_position is None:
            return {
                "startPosition": None,
                "finishPosition": None,
                "bestPosition": None,
                "worstPosition": None,
                "netPositionDelta": None,
                "positionRows": [],
            }
        start_position = self.race_start_position
        if start_position is None and position_rows:
            start_position = int(position_rows[0]["position"])
        finish_position = int(position_rows[-1]["position"]) if position_rows else start_position
        positions = [int(row["position"]) for row in position_rows]
        if start_position is not None:
            positions.append(int(start_position))
        best_position = min(positions) if positions else None
        worst_position = max(positions) if positions else None
        net_delta = (
            int(start_position) - int(finish_position)
            if start_position is not None and finish_position is not None
            else None
        )
        synthetic_rows: list[dict[str, Any]] = []
        if self.race_start_position is not None:
            synthetic_rows.append({"lapNum": 0, "position": self.race_start_position, "label": "Start"})
        synthetic_rows.extend(position_rows)
        return {
            "startPosition": start_position,
            "finishPosition": finish_position,
            "bestPosition": best_position,
            "worstPosition": worst_position,
            "netPositionDelta": net_delta,
            "positionRows": synthetic_rows,
        }

    def _race_fun_stats(
        self,
        rows: list[dict[str, Any]],
        phases: list[dict[str, Any]],
        longest_scored_streak: int,
        position_summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        stats: list[dict[str, Any]] = []
        position_rows = position_summary.get("positionRows") or []
        if position_summary.get("startPosition") is not None and position_summary.get("finishPosition") is not None:
            start_position = int(position_summary["startPosition"])
            finish_position = int(position_summary["finishPosition"])
            best_position = int(position_summary["bestPosition"])
            worst_position = int(position_summary["worstPosition"])
            net_gain = int(position_summary["netPositionDelta"])
            stats.append(
                {
                    "label": "Positions",
                    "value": self._position_delta_label(net_gain),
                    "detail": f"Started P{start_position}, finished P{finish_position}; best running spot P{best_position}.",
                    "tone": "gain" if net_gain > 0 else "loss" if net_gain < 0 else "neutral",
                }
            )
            stats.append(
                {
                    "label": "Position Range",
                    "value": f"P{best_position}-P{worst_position}",
                    "detail": f"Covered {worst_position - best_position + 1} track positions during the run.",
                    "tone": "neutral",
                }
            )
            biggest_position_gain = self._biggest_position_gain(position_rows)
            if biggest_position_gain is not None:
                stats.append(biggest_position_gain)

        ers_rows = [
            row for row in rows if row.get("ersUsedKj") is not None and isinstance(row.get("ersUsedKj"), (int, float))
        ]
        if ers_rows:
            max_ers = max(ers_rows, key=lambda row: float(row["ersUsedKj"]))
            avg_ers = self._avg(float(row["ersUsedKj"]) for row in ers_rows)
            stats.append(
                {
                    "label": "Biggest ERS Lap",
                    "value": f"L{max_ers['lapNum']} {self._format_kj(float(max_ers['ersUsedKj']))}",
                    "detail": f"Average spend was {self._format_kj(avg_ers)} per measured lap.",
                    "tone": "gain",
                }
            )

        fuel_rows = [
            row for row in rows if row.get("fuelKg") is not None and isinstance(row.get("fuelKg"), (int, float))
        ]
        if len(fuel_rows) >= 2:
            fuel_burn = float(fuel_rows[0]["fuelKg"]) - float(fuel_rows[-1]["fuelKg"])
            per_lap = fuel_burn / max(1, len(fuel_rows) - 1)
            stats.append(
                {
                    "label": "Fuel Burn",
                    "value": f"{fuel_burn:.1f} kg",
                    "detail": f"About {per_lap:.2f} kg per completed lap with fuel telemetry.",
                    "tone": "neutral",
                }
            )

        if longest_scored_streak:
            stats.append(
                {
                    "label": "Clean Streak",
                    "value": f"{longest_scored_streak} laps",
                    "detail": "Longest run of representative green-flag laps used by the ranking model.",
                    "tone": "gain" if longest_scored_streak >= 5 else "neutral",
                }
            )

        if phases:
            fastest_phase = min(phases, key=lambda phase: phase["averageLapTimeMs"])
            slowest_phase = max(phases, key=lambda phase: phase["averageLapTimeMs"])
            stats.append(
                {
                    "label": "Fastest Phase",
                    "value": fastest_phase["name"],
                    "detail": f"{fastest_phase['averageLapTime']} average across laps {fastest_phase['lapRange']}.",
                    "tone": "gain",
                }
            )
            if slowest_phase["name"] != fastest_phase["name"]:
                stats.append(
                    {
                        "label": "Slowest Phase",
                        "value": slowest_phase["name"],
                        "detail": f"{slowest_phase['averageLapTime']} average across laps {slowest_phase['lapRange']}.",
                        "tone": "loss",
                    }
                )

        return stats[:8]

    def _biggest_position_gain(self, position_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
        for previous, current in zip(position_rows, position_rows[1:]):
            gain = int(previous["position"]) - int(current["position"])
            if gain <= 0:
                continue
            if best is None or gain > best[0]:
                best = (gain, previous, current)
        if best is None:
            return None
        gain, previous, current = best
        previous_label = "Start" if int(previous["lapNum"]) == 0 else f"Lap {previous['lapNum']}"
        current_label = f"lap {current['lapNum']}" if int(current["lapNum"]) != 0 else "start"
        return {
            "label": "Best Position Jump",
            "value": f"+{gain}",
            "detail": f"{previous_label} to {current_label}: P{previous['position']} to P{current['position']}.",
            "tone": "gain",
        }

    def _race_lap_exclusion_reason(self, lap: CompletedLap) -> str | None:
        if lap.lap_num == 1:
            return "Excluded from ranking: lap 1 race start."
        if lap.pit_lap:
            return "Excluded from ranking: pit lane lap."
        if lap.safety_car_lap:
            return "Excluded from ranking: safety car or VSC."
        if lap.non_green_flag_lap:
            return "Excluded from ranking: non-green FIA flag."
        if lap.invalid:
            return "Excluded from ranking: invalid lap."
        if not lap.samples:
            return "Excluded from ranking: no telemetry trace."
        return None

    def _mark_non_representative_pace_outliers(self, rows: list[dict[str, Any]]) -> None:
        candidates = [
            row for row in rows if row.get("rankingEligible") and isinstance(row.get("lapTimeMs"), int)
        ]
        if len(candidates) < 4:
            return
        median_ms = self._median(row["lapTimeMs"] for row in candidates)
        slow_cutoff_ms = max(6_000.0, median_ms * 0.08)
        fast_cutoff_ms = max(4_000.0, median_ms * 0.055)
        for row in candidates:
            lap_time_ms = row["lapTimeMs"]
            if lap_time_ms - median_ms > slow_cutoff_ms:
                row["rankingEligible"] = False
                row["exclusionReason"] = "Excluded from ranking: non-representative slow lap."
                row["note"] = row["exclusionReason"]
            elif median_ms - lap_time_ms > fast_cutoff_ms:
                row["rankingEligible"] = False
                row["exclusionReason"] = "Excluded from ranking: non-representative fast outlier."
                row["note"] = row["exclusionReason"]

    def _race_power_factors(
        self,
        laps: list[CompletedLap],
        rows: list[dict[str, Any]],
        scored_laps: list[CompletedLap],
        reference: ReferenceProfile | None,
        avg_delta_to_best: int | None,
        stdev_ms: int | None,
        volatility_ms: int | None,
        trend_ms: int | None,
        longest_scored_streak: int,
        position_summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not scored_laps:
            return [
                {
                    "name": "Representative laps",
                    "score": 0.0,
                    "weight": 1.0,
                    "detail": "No representative green-flag racing laps are available for a power ranking.",
                    "evidence": ["Lap 1, pit laps, safety-car laps, non-green-flag laps, invalid laps, and telemetry gaps are excluded."],
                }
            ]

        scored_rows = [row for row in rows if row.get("rankingEligible")]
        penalty_rows = [row for row in rows if self._ranking_exclusion_counts_as_penalty(row)]
        neutral_exclusions = [
            row
            for row in rows
            if not row.get("rankingEligible") and not self._ranking_exclusion_counts_as_penalty(row)
        ]

        best_lap = min(scored_laps, key=lambda lap: lap.lap_time_ms)
        best_delta_to_reference = best_lap.lap_time_ms - reference.lap_time_ms if reference is not None else 0
        pace_score = self._clamp_score(
            10.0
            - max(0, best_delta_to_reference) / 1200
            - (avg_delta_to_best or 0) / 1400
        )

        consistency_score = 7.5
        if stdev_ms is not None:
            consistency_score += 2.0
            consistency_score -= max(0, stdev_ms - 850) / 900
        if volatility_ms is not None:
            consistency_score -= max(0, volatility_ms - 450) / 350
        consistency_score = self._clamp_score(consistency_score)

        control_values = [
            float(row["controlScore"])
            for row in scored_rows
            if row.get("controlScore") is not None
        ]
        avg_control = self._avg(control_values) if control_values else 0.0
        penalty_base = len(scored_rows) + len(penalty_rows)
        penalty_ratio = len(penalty_rows) / penalty_base if penalty_base else 0.0
        high_slip = self._avg(
            float(row["highSlipPct"] or 0)
            for row in scored_rows
            if row.get("highSlipPct") is not None
        )
        avg_slip_ratio = self._avg(
            float(row["avgSlipRatio"] or 0)
            for row in scored_rows
            if row.get("avgSlipRatio") is not None
        )
        steering_throttle = self._avg(
            float(row["steeringThrottlePct"] or 0) for row in scored_rows
        )
        overlap = self._avg(float(row["overlapPct"] or 0) for row in scored_rows)
        trend_penalty = max(0, (trend_ms or 0) - 1500) / 800
        slip_ratio_penalty = max(0.0, avg_slip_ratio - 0.18) * 16.0
        control_score = self._clamp_score(
            avg_control / 10
            - penalty_ratio * 3.0
            - high_slip * 0.08
            - steering_throttle * 0.03
            - overlap * 0.04
            - slip_ratio_penalty * 0.45
        )
        tyre_energy_score = self._clamp_score(
            10.0
            - high_slip * 0.28
            - steering_throttle * 0.12
            - overlap * 0.14
            - slip_ratio_penalty
            - trend_penalty
        )

        representative_base = len(scored_rows) + len(penalty_rows)
        valid_pct = len(scored_rows) / representative_base * 100 if representative_base else 100.0
        streak_score = min(10.0, longest_scored_streak / max(1, representative_base) * 12.0)
        trend_score = 8.0
        if trend_ms is not None:
            trend_score = 10.0 - max(0, trend_ms) / 1000 + max(0, -trend_ms) / 2500
        execution_score = self._clamp_score((valid_pct / 10) * 0.48 + streak_score * 0.32 + trend_score * 0.20)
        position_score = self._race_position_score(position_summary)
        net_position_delta = position_summary.get("netPositionDelta")
        start_position = position_summary.get("startPosition")
        finish_position = position_summary.get("finishPosition")
        podium_bonus = self._race_podium_bonus(position_summary)

        reference_detail = (
            f"best lap {self._signed_delta(best_delta_to_reference)} vs {reference.name}"
            if reference is not None
            else "rated against your race best"
        )
        return [
            {
                "name": "Track Position",
                "score": position_score,
                "weight": 0.44,
                "positionsLost": max(0, -int(net_position_delta)) if net_position_delta is not None else 0,
                "podiumBonus": podium_bonus,
                "detail": (
                    f"Started P{start_position}, finished P{finish_position}; net {self._position_delta_label(int(net_position_delta))}."
                    if start_position is not None and finish_position is not None and net_position_delta is not None
                    else "No reliable start and finish position telemetry was available."
                ),
                "evidence": [
                    f"Best running spot P{position_summary['bestPosition']}" if position_summary.get("bestPosition") is not None else "Best position unknown",
                    f"Worst running spot P{position_summary['worstPosition']}" if position_summary.get("worstPosition") is not None else "Worst position unknown",
                    f"Podium result bonus +{podium_bonus:.1f}" if podium_bonus > 0 else "No podium result bonus",
                ],
            },
            {
                "name": "Pace",
                "score": pace_score,
                "weight": 0.16,
                "detail": f"{reference_detail}; average representative lap is {self._format_delta(avg_delta_to_best or 0)} off your race best.",
                "evidence": [
                    f"Best lap {best_lap.lap_num}: {self._format_ms(best_lap.lap_time_ms)}",
                    f"Scored-lap average: {self._format_ms(round(self._avg(lap.lap_time_ms for lap in scored_laps)))}",
                    f"{len(neutral_exclusions)} neutral laps ignored",
                ],
            },
            {
                "name": "Consistency",
                "score": consistency_score,
                "weight": 0.12,
                "detail": "Rewards repeatable lap time without large lap-to-lap spikes.",
                "evidence": [
                    f"Stdev {stdev_ms / 1000:.2f}s" if stdev_ms is not None else "Need more clean laps",
                    f"Volatility {volatility_ms / 1000:.2f}s" if volatility_ms is not None else "No lap-to-lap trend yet",
                ],
            },
            {
                "name": "Control",
                "score": control_score,
                "weight": 0.10,
                "detail": "Combines input quality, invalid laps, overlap, and traction stability.",
                "evidence": [
                    f"Average control {avg_control:.0f}/100",
                    f"High-slip samples {high_slip:.1f}%",
                    f"{len(penalty_rows)} driver/error laps counted as penalties",
                ],
            },
            {
                "name": "Tyre and Energy",
                "score": tyre_energy_score,
                "weight": 0.10,
                "detail": "Looks for slip, throttle-with-lock, pedal overlap, and late-run degradation.",
                "evidence": [
                    f"High-slip samples {high_slip:.1f}%",
                    f"Average slip ratio {avg_slip_ratio:.2f}",
                    f"Throttle with steering {steering_throttle:.1f}%",
                    f"Pedal overlap {overlap:.1f}%",
                ],
            },
            {
                "name": "Race Execution",
                "score": execution_score,
                "weight": 0.08,
                "detail": "Scores representative green-flag streaks, finish trend, and avoidable lap exclusions.",
                "evidence": [
                    f"{valid_pct:.1f}% representative laps",
                    f"Longest scored streak {longest_scored_streak}",
                    self._race_trend_label(trend_ms),
                ],
            },
        ]

    @staticmethod
    def _ranking_exclusion_counts_as_penalty(row: dict[str, Any]) -> bool:
        reason = str(row.get("exclusionReason") or "")
        if not reason:
            return False
        neutral_reasons = (
            "lap 1 race start",
            "pit lane lap",
            "safety car",
            "non-green FIA flag",
        )
        if any(text in reason for text in neutral_reasons):
            return False
        return True

    def _race_phase_breakdown(
        self,
        clean_laps: list[CompletedLap],
        reference: ReferenceProfile | None,
    ) -> list[dict[str, Any]]:
        if not clean_laps:
            return []
        if len(clean_laps) == 1:
            phase_slices = [("Full run", clean_laps)]
        elif len(clean_laps) == 2:
            phase_slices = [("Opening", clean_laps[:1]), ("Closing", clean_laps[1:])]
        else:
            first_end = max(1, len(clean_laps) // 3)
            second_end = max(first_end + 1, (len(clean_laps) * 2) // 3)
            phase_slices = [
                ("Opening", clean_laps[:first_end]),
                ("Middle", clean_laps[first_end:second_end]),
                ("Closing", clean_laps[second_end:]),
            ]

        phases: list[dict[str, Any]] = []
        race_best = min(clean_laps, key=lambda lap: lap.lap_time_ms)
        for name, laps in phase_slices:
            if not laps:
                continue
            times = [lap.lap_time_ms for lap in laps]
            avg_ms = round(self._avg(times))
            best = min(laps, key=lambda lap: lap.lap_time_ms)
            control_values = []
            for lap in laps:
                control_score = self._lap_overview(lap).get("controlScore")
                if control_score is not None:
                    control_values.append(control_score)
            avg_control = round(self._avg(float(value) for value in control_values)) if control_values else None
            delta_to_reference = avg_ms - reference.lap_time_ms if reference is not None else None
            delta_to_race_best = avg_ms - race_best.lap_time_ms
            phases.append(
                {
                    "name": name,
                    "lapRange": self._lap_range_label(laps),
                    "lapCount": len(laps),
                    "averageLapTimeMs": avg_ms,
                    "averageLapTime": self._format_ms(avg_ms),
                    "bestLapNum": best.lap_num,
                    "bestLapTime": self._format_ms(best.lap_time_ms),
                    "deltaToReferenceMs": delta_to_reference,
                    "deltaToRaceBestMs": delta_to_race_best,
                    "averageControlScore": avg_control,
                    "note": self._phase_note(name, delta_to_race_best, avg_control),
                }
            )
        return phases

    def _race_sector_trend(
        self,
        clean_laps: list[CompletedLap],
        reference: ReferenceProfile | None,
    ) -> list[dict[str, Any]]:
        sectors = [
            ("S1", "sector1_time_ms", reference.sector1_time_ms if reference is not None else None),
            ("S2", "sector2_time_ms", reference.sector2_time_ms if reference is not None else None),
            ("S3", "sector3_time_ms", reference.sector3_time_ms if reference is not None else None),
        ]
        result: list[dict[str, Any]] = []
        for label, attr, reference_ms in sectors:
            entries = [(lap, int(getattr(lap, attr))) for lap in clean_laps if int(getattr(lap, attr)) > 0]
            if not entries:
                continue
            best_lap, best_ms = min(entries, key=lambda item: item[1])
            values = [value for _, value in entries]
            avg_ms = round(self._avg(values))
            spread_ms = max(values) - min(values) if len(values) > 1 else 0
            result.append(
                {
                    "sector": label,
                    "bestLapNum": best_lap.lap_num,
                    "bestMs": best_ms,
                    "best": self._format_ms(best_ms),
                    "averageMs": avg_ms,
                    "average": self._format_ms(avg_ms),
                    "spreadMs": spread_ms,
                    "spread": self._format_delta(spread_ms),
                    "deltaToReferenceMs": best_ms - reference_ms if reference_ms is not None else None,
                }
            )
        return result

    def _race_standout_laps(
        self,
        clean_laps: list[CompletedLap],
        rows: list[dict[str, Any]],
        reference: ReferenceProfile | None,
    ) -> list[dict[str, Any]]:
        if not clean_laps:
            return []
        by_lap = {row["lapNum"]: row for row in rows}
        avg_time = self._avg(lap.lap_time_ms for lap in clean_laps)
        best_lap = min(clean_laps, key=lambda lap: lap.lap_time_ms)
        most_average = min(clean_laps, key=lambda lap: abs(lap.lap_time_ms - avg_time))
        best_control_row = max(
            (row for row in rows if row.get("rankingEligible") and row.get("controlScore") is not None),
            key=lambda row: row["controlScore"],
            default=None,
        )
        biggest_gain_row = min(
            (row for row in rows if row.get("rankingEligible") and row.get("deltaToPreviousCleanMs") is not None),
            key=lambda row: row["deltaToPreviousCleanMs"],
            default=None,
        )
        biggest_drop_row = max(
            (row for row in rows if row.get("rankingEligible") and row.get("deltaToPreviousCleanMs") is not None),
            key=lambda row: row["deltaToPreviousCleanMs"],
            default=None,
        )

        moments: list[dict[str, Any]] = [
            {
                "type": "best",
                "title": "Best lap",
                "lapNum": best_lap.lap_num,
                "metric": self._format_ms(best_lap.lap_time_ms),
                "detail": (
                    f"{self._signed_delta(best_lap.lap_time_ms - reference.lap_time_ms)} vs {reference.name}."
                    if reference is not None
                    else "Fastest scored lap of the run."
                ),
                "tone": "gain",
            },
            {
                "type": "benchmark",
                "title": "Representative lap",
                "lapNum": most_average.lap_num,
                "metric": self._format_ms(most_average.lap_time_ms),
                "detail": f"{self._signed_delta(int(most_average.lap_time_ms - avg_time))} from scored-lap average.",
                "tone": "neutral",
            },
        ]
        if best_control_row is not None:
            moments.append(
                {
                    "type": "control",
                    "title": "Cleanest inputs",
                    "lapNum": best_control_row["lapNum"],
                    "metric": f"{best_control_row['controlScore']}/100",
                    "detail": by_lap[best_control_row["lapNum"]].get("note") or "Highest control score in the run.",
                    "tone": "gain",
                }
            )
        if biggest_gain_row is not None and biggest_gain_row["deltaToPreviousCleanMs"] < -100:
            moments.append(
                {
                    "type": "gain",
                    "title": "Biggest lap-to-lap gain",
                    "lapNum": biggest_gain_row["lapNum"],
                    "metric": self._signed_delta(biggest_gain_row["deltaToPreviousCleanMs"]),
                    "detail": "Largest improvement against the previous clean lap.",
                    "tone": "gain",
                }
            )
        if biggest_drop_row is not None and biggest_drop_row["deltaToPreviousCleanMs"] > 600:
            moments.append(
                {
                    "type": "drop",
                    "title": "Largest fade",
                    "lapNum": biggest_drop_row["lapNum"],
                    "metric": self._signed_delta(biggest_drop_row["deltaToPreviousCleanMs"]),
                    "detail": "Largest slowdown against the previous clean lap.",
                    "tone": "loss",
                }
            )
        return moments[:5]

    def _race_risk_register(
        self,
        rows: list[dict[str, Any]],
        scored_laps: list[CompletedLap],
        stdev_ms: int | None,
        volatility_ms: int | None,
        trend_ms: int | None,
    ) -> list[dict[str, Any]]:
        reviewable = [row for row in rows if row.get("rankingEligible")]
        if not reviewable:
            return [
                {
                    "title": "No representative baseline",
                    "severity": "high",
                    "detail": "No completed lap qualifies for green-flag power ranking.",
                    "action": "Finish at least one clean, non-pit, green-flag lap before judging race pace.",
                }
            ]

        risks: list[dict[str, Any]] = []
        penalty_rows = [row for row in rows if self._ranking_exclusion_counts_as_penalty(row)]
        if penalty_rows:
            risks.append(
                {
                    "title": "Track limits or invalidation",
                    "severity": "high" if len(penalty_rows) / max(1, len(scored_laps) + len(penalty_rows)) >= 0.2 else "medium",
                    "detail": f"{len(penalty_rows)} avoidable laps were excluded from the representative sample.",
                    "action": "Trade a small entry margin for a fully classified stint.",
                }
            )
        avg_overlap = self._avg(float(row["overlapPct"] or 0) for row in reviewable)
        if avg_overlap >= 5.0:
            risks.append(
                {
                    "title": "Pedal overlap",
                    "severity": "medium",
                    "detail": f"Brake/throttle overlap averages {avg_overlap:.1f}% across scored laps.",
                    "action": "Separate brake release and throttle pickup before adding setup changes.",
                }
            )
        high_slip_values = [
            float(row["highSlipPct"] or 0) for row in reviewable if row.get("highSlipPct") is not None
        ]
        avg_high_slip = self._avg(high_slip_values)
        if high_slip_values and avg_high_slip >= 4.0:
            risks.append(
                {
                    "title": "Rear traction life",
                    "severity": "high" if avg_high_slip >= 9.0 else "medium",
                    "detail": f"High-slip traction samples average {avg_high_slip:.1f}%.",
                    "action": "Delay full throttle until steering lock is unwinding, especially late stint.",
                }
            )
        if volatility_ms is not None and volatility_ms >= 1000:
            risks.append(
                {
                    "title": "Lap-to-lap volatility",
                    "severity": "medium",
                    "detail": f"Scored laps move by {volatility_ms / 1000:.2f}s on average.",
                    "action": "Pick one repeatable braking reference per heavy stop and defend it for three laps.",
                }
            )
        if stdev_ms is not None and stdev_ms >= 1800:
            risks.append(
                {
                    "title": "Stint spread",
                    "severity": "low",
                    "detail": f"Scored-lap standard deviation is {stdev_ms / 1000:.2f}s.",
                    "action": "Review whether traffic, tyre state, or ERS use is creating the spread.",
                }
            )
        if trend_ms is not None and trend_ms >= 1800:
            risks.append(
                {
                    "title": "Closing-stint fade",
                    "severity": "medium",
                    "detail": f"Closing phase is {self._format_delta(trend_ms)} slower than opening phase.",
                    "action": "Protect rear traction and avoid battery spend before compromised exits.",
                }
            )
        if not risks:
            risks.append(
                {
                    "title": "No major race risk",
                    "severity": "ok",
                    "detail": "The run is clean enough that gains should come from targeted corner work.",
                    "action": "Use the lap review view on the slowest representative lap.",
                }
            )
        return risks[:5]

    def _race_recommendations(
        self,
        factors: list[dict[str, Any]],
        risks: list[dict[str, Any]],
        phases: list[dict[str, Any]],
        reference: ReferenceProfile | None,
    ) -> list[str]:
        if not factors:
            return ["Complete clean laps to unlock a race plan."]
        weakest = min(factors, key=lambda factor: float(factor["score"]))
        recommendations: list[str] = []
        if weakest["name"] == "Track Position":
            recommendations.append("Treat position loss as the main race limiter: review starts, defense, and the laps where places were lost before chasing pace score.")
        elif weakest["name"] == "Pace":
            if reference is not None:
                recommendations.append(f"Use the lap review map against {reference.name} and fix the largest two loss zones first.")
            else:
                recommendations.append("Set a clean personal best early, then judge race pace against that stable reference.")
        elif weakest["name"] == "Consistency":
            recommendations.append("Run three laps with the same braking markers before changing ERS or setup targets.")
        elif weakest["name"] == "Control":
            recommendations.append("Prioritize classified laps: leave margin on entry, then build rotation and exit speed gradually.")
        elif weakest["name"] == "Tyre and Energy":
            recommendations.append("Treat exits as tyre-management zones: steering unwind first, then full throttle and ERS.")
        else:
            recommendations.append("Stabilize the stint structure before chasing single-lap pace.")

        for risk in risks:
            if risk["severity"] in {"high", "medium"} and risk["action"] not in recommendations:
                recommendations.append(risk["action"])
            if len(recommendations) >= 4:
                break
        if phases:
            slowest_phase = max(phases, key=lambda phase: phase["deltaToRaceBestMs"])
            recommendations.append(
                f"Audit the {slowest_phase['name'].lower()} phase laps {slowest_phase['lapRange']}; average pace there is {self._format_delta(slowest_phase['deltaToRaceBestMs'])} off the race-best lap."
            )
        return recommendations[:5]

    def _race_best_lap_summary(
        self,
        lap: CompletedLap | None,
        reference: ReferenceProfile | None,
    ) -> dict[str, Any] | None:
        if lap is None:
            return None
        return {
            "lapNum": lap.lap_num,
            "lapTimeMs": lap.lap_time_ms,
            "lapTime": self._format_ms(lap.lap_time_ms),
            "deltaToReferenceMs": lap.lap_time_ms - reference.lap_time_ms if reference is not None else None,
        }

    def _race_position_score(self, position_summary: dict[str, Any]) -> float:
        start_position = position_summary.get("startPosition")
        finish_position = position_summary.get("finishPosition")
        net_delta = position_summary.get("netPositionDelta")
        if start_position is None or finish_position is None or net_delta is None:
            return 5.5
        start = int(start_position)
        finish = int(finish_position)
        net = int(net_delta)
        if net >= 0:
            score = 7.2 + net * 0.9
            if finish <= 3:
                score += 0.6
            if start <= 3 and finish <= start:
                score += 0.7
        else:
            positions_lost = abs(net)
            score = 7.0 - positions_lost * 1.25
            if start <= 3:
                score -= positions_lost * 0.45
            if finish > 3:
                score -= 0.4
        return self._clamp_score(score)

    @staticmethod
    def _race_podium_bonus(position_summary: dict[str, Any]) -> float:
        start_position = position_summary.get("startPosition")
        finish_position = position_summary.get("finishPosition")
        net_delta = position_summary.get("netPositionDelta")
        if start_position is None or finish_position is None or net_delta is None:
            return 0.0
        finish = int(finish_position)
        net = int(net_delta)
        if finish > 3 or net < 0:
            return 0.0
        return 1.0

    @staticmethod
    def _weighted_power_score(factors: list[dict[str, Any]]) -> float | None:
        if not factors:
            return None
        total_weight = sum(float(factor.get("weight", 0.0)) for factor in factors)
        if total_weight <= 0:
            return None
        score = sum(float(factor["score"]) * float(factor.get("weight", 0.0)) for factor in factors) / total_weight
        position_factor = next((factor for factor in factors if factor.get("name") == "Track Position"), None)
        if position_factor is not None:
            positions_lost = int(position_factor.get("positionsLost") or 0)
            if positions_lost > 0:
                score = min(score, max(4.0, 7.5 - positions_lost * 0.5))
            podium_bonus = float(position_factor.get("podiumBonus") or 0.0)
            if podium_bonus > 0:
                score += podium_bonus
        return round(max(0.0, min(10.0, score)), 1)

    @staticmethod
    def _power_label(score: float | None) -> str:
        if score is None:
            return "No rating"
        if score >= 9.0:
            return "Elite race drive"
        if score >= 8.0:
            return "Front-running form"
        if score >= 7.0:
            return "Strong points finish"
        if score >= 6.0:
            return "Solid but exposed"
        if score >= 5.0:
            return "Mixed execution"
        return "Needs reset"

    @staticmethod
    def _power_confidence(clean_lap_count: int) -> str:
        if clean_lap_count >= 10:
            return "Full-race confidence"
        if clean_lap_count >= 5:
            return "Stint confidence"
        if clean_lap_count >= 2:
            return "Provisional"
        if clean_lap_count == 1:
            return "Single-lap sample"
        return "No clean sample"

    @staticmethod
    def _power_explanation(score: float | None, factors: list[dict[str, Any]], clean_lap_count: int) -> str:
        if score is None or not factors:
            return "Complete laps to build a race power ranking."
        strongest = max(factors, key=lambda factor: float(factor["score"]))
        weakest = min(factors, key=lambda factor: float(factor["score"]))
        sample_note = "Rating is provisional until the run has at least five clean laps. " if clean_lap_count < 5 else ""
        return (
            f"{sample_note}Strongest area: {strongest['name']} ({strongest['score']}/10). "
            f"Main limiter: {weakest['name']} ({weakest['score']}/10)."
        )

    @staticmethod
    def _clamp_score(value: float) -> float:
        return round(max(0.0, min(10.0, value)), 1)

    @staticmethod
    def _stddev(values: list[int]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))

    @staticmethod
    def _lap_time_volatility(clean_laps: list[CompletedLap]) -> int | None:
        if len(clean_laps) < 2:
            return None
        deltas = [
            abs(current.lap_time_ms - previous.lap_time_ms)
            for previous, current in zip(clean_laps, clean_laps[1:])
            if current.lap_num - previous.lap_num <= 2
        ]
        if not deltas:
            deltas = [
                abs(current.lap_time_ms - previous.lap_time_ms)
                for previous, current in zip(clean_laps, clean_laps[1:])
            ]
        return round(sum(deltas) / len(deltas)) if deltas else None

    @staticmethod
    def _longest_clean_streak(laps: list[CompletedLap]) -> int:
        longest = 0
        current = 0
        for lap in laps:
            if lap.invalid:
                current = 0
            else:
                current += 1
                longest = max(longest, current)
        return longest

    @staticmethod
    def _longest_scored_streak(rows: list[dict[str, Any]]) -> int:
        longest = 0
        current = 0
        for row in rows:
            if row.get("rankingEligible"):
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    @staticmethod
    def _lap_range_label(laps: list[CompletedLap]) -> str:
        if not laps:
            return "--"
        if len(laps) == 1:
            return str(laps[0].lap_num)
        return f"{laps[0].lap_num}-{laps[-1].lap_num}"

    @staticmethod
    def _phase_note(name: str, delta_to_race_best: int, avg_control: int | None) -> str:
        control = f" Control {avg_control}/100." if avg_control is not None else ""
        if delta_to_race_best <= 400:
            return f"{name} phase is close to race-best pace.{control}"
        if delta_to_race_best <= 1200:
            return f"{name} phase is usable but leaves repeatability time.{control}"
        return f"{name} phase is the main pace-loss window.{control}"

    def _race_trend_label(self, trend_ms: int | None) -> str:
        if trend_ms is None:
            return "--"
        if trend_ms < -250:
            return f"{self._format_delta(abs(trend_ms))} faster at the end"
        if trend_ms > 250:
            return f"{self._format_delta(trend_ms)} slower at the end"
        return "Stable finish"

    @classmethod
    def _format_duration_ms(cls, milliseconds: int) -> str:
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, ms = divmod(remainder, 1000)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}.{ms:03d}"
        return f"{minutes}:{seconds:02d}.{ms:03d}"

    @staticmethod
    def _lap_reference_delta(lap: CompletedLap, reference: ReferenceProfile | None) -> int | None:
        if reference is None:
            return None
        return lap.lap_time_ms - reference.lap_time_ms

    @staticmethod
    def _lap_ers_used_kj(lap: CompletedLap) -> float | None:
        deployed = [
            sample.ers_deployed_this_lap_j
            for sample in lap.samples
            if sample.ers_deployed_this_lap_j is not None
        ]
        if not deployed:
            return None
        return max(deployed) / 1000

    @staticmethod
    def _tyre_label(visual_compound: int | None, actual_compound: int | None) -> str | None:
        visual_labels = {
            7: "Inter",
            8: "Wet",
            16: "Soft",
            17: "Medium",
            18: "Hard",
        }
        actual_labels = {
            7: "Inter",
            8: "Wet",
            16: "C5",
            17: "C4",
            18: "C3",
            19: "C2",
            20: "C1",
            21: "C0",
        }
        if visual_compound in visual_labels:
            return visual_labels[visual_compound]
        if actual_compound in actual_labels:
            return actual_labels[actual_compound]
        if visual_compound is not None:
            return f"Tyre {visual_compound}"
        if actual_compound is not None:
            return f"Tyre {actual_compound}"
        return None

    def _lap_overview(self, lap: CompletedLap) -> dict[str, Any]:
        samples = sorted(lap.samples, key=lambda sample: sample.normalized_distance)
        if not samples:
            return {}
        slip_values = [sample.avg_slip_ratio for sample in samples if sample.avg_slip_ratio is not None]
        avg_slip = self._avg(slip_values) if slip_values else None
        overlap_pct = self._sample_pct(samples, lambda sample: sample.brake > 0.05 and sample.throttle > 0.05)
        steering_throttle_pct = self._sample_pct(
            samples,
            lambda sample: sample.throttle > 0.70 and abs(sample.steer) > 0.22,
        )
        high_slip_pct = self._sample_pct(
            samples,
            lambda sample: sample.avg_slip_ratio is not None
            and sample.avg_slip_ratio > 0.28
            and sample.throttle > 0.55,
        )
        full_throttle_pct = self._sample_pct(samples, lambda sample: sample.throttle >= 0.95)
        braking_pct = self._sample_pct(samples, lambda sample: sample.brake >= 0.10)
        coast_pct = self._sample_pct(samples, lambda sample: sample.throttle < 0.05 and sample.brake < 0.05)
        throttle_snap_pct = self._transition_pct(samples, lambda previous, current: abs(current.throttle - previous.throttle) >= 0.42)
        brake_snap_pct = self._transition_pct(samples, lambda previous, current: abs(current.brake - previous.brake) >= 0.38)
        steer_snap_pct = self._transition_pct(samples, lambda previous, current: abs(current.steer - previous.steer) >= 0.34)
        control_score = 100.0
        control_score -= overlap_pct * 2.2
        control_score -= steering_throttle_pct * 1.2
        control_score -= high_slip_pct * 1.5
        control_score -= max(0.0, coast_pct - 8.0) * 0.65
        control_score -= max(0.0, braking_pct - 24.0) * 0.25
        control_score -= max(0.0, 46.0 - full_throttle_pct) * 0.20
        control_score -= throttle_snap_pct * 0.45
        control_score -= brake_snap_pct * 0.45
        control_score -= steer_snap_pct * 0.35
        if avg_slip is not None and avg_slip > 0.22:
            control_score -= (avg_slip - 0.22) * 120

        return {
            "sampleCount": len(samples),
            "avgSpeedKmh": round(self._avg(sample.speed_kmh for sample in samples), 1),
            "topSpeedKmh": max(sample.speed_kmh for sample in samples),
            "fullThrottlePct": full_throttle_pct,
            "brakingPct": braking_pct,
            "coastPct": coast_pct,
            "brakeThrottleOverlapPct": overlap_pct,
            "steeringThrottlePct": steering_throttle_pct,
            "highSlipPct": high_slip_pct if slip_values else None,
            "avgSlipRatio": round(avg_slip, 3) if avg_slip is not None else None,
            "throttleSnapPct": throttle_snap_pct,
            "brakeSnapPct": brake_snap_pct,
            "steerSnapPct": steer_snap_pct,
            "controlScore": round(max(0.0, min(100.0, control_score))),
        }

    def _lap_overview_notes(self, lap: CompletedLap) -> list[str]:
        overview = self._lap_overview(lap)
        if not overview:
            return []

        notes: list[str] = []
        overlap = float(overview.get("brakeThrottleOverlapPct") or 0.0)
        steering_throttle = float(overview.get("steeringThrottlePct") or 0.0)
        high_slip = overview.get("highSlipPct")
        coast = float(overview.get("coastPct") or 0.0)
        full_throttle = float(overview.get("fullThrottlePct") or 0.0)
        throttle_snap = float(overview.get("throttleSnapPct") or 0.0)
        brake_snap = float(overview.get("brakeSnapPct") or 0.0)

        if overlap >= 6.0:
            notes.append(f"Brake/throttle overlap is {overlap:.1f}% of samples; separate the pedals in braking zones.")
        if steering_throttle >= 12.0:
            notes.append(
                f"High throttle with steering lock appears in {steering_throttle:.1f}% of samples; unwind earlier on exits."
            )
        if high_slip is not None and float(high_slip) >= 8.0:
            notes.append(f"Driven-wheel slip is elevated on {float(high_slip):.1f}% of traction samples.")
        if throttle_snap >= 12.0 or brake_snap >= 12.0:
            notes.append("Input transitions are abrupt; smooth the brake release and throttle pickup.")
        if coast >= 18.0:
            notes.append(f"Coasting is {coast:.1f}% of samples; check whether entries need a cleaner brake release.")
        if full_throttle >= 58.0 and overlap < 4.0 and (high_slip is None or float(high_slip) < 5.0):
            notes.append("Lap profile looks committed: high full-throttle time with low overlap and stable traction.")
        if not notes:
            notes.append("Lap profile is balanced; use the trace to hunt for smaller timing differences.")
        return notes[:3]

    @staticmethod
    def _sample_pct(samples: list[LapSample], predicate: Any) -> float:
        if not samples:
            return 0.0
        matches = sum(1 for sample in samples if predicate(sample))
        return round(matches / len(samples) * 100, 1)

    @staticmethod
    def _transition_pct(samples: list[LapSample], predicate: Any) -> float:
        pairs = [
            (previous, current)
            for previous, current in zip(samples, samples[1:])
            if 0 < current.normalized_distance - previous.normalized_distance <= 0.06
        ]
        if not pairs:
            return 0.0
        matches = sum(1 for previous, current in pairs if predicate(previous, current))
        return round(matches / len(pairs) * 100, 1)

    def _live_sample(self) -> LapSample | None:
        if self.latest_lap is None or self.latest_telemetry is None:
            return None
        return self._sample_from_lap(self.latest_lap)

    def _current_samples_with_live(self, current_sample: LapSample | None) -> list[LapSample]:
        if current_sample is None:
            return self.active_samples
        if not self.active_samples:
            return [current_sample]
        last = self.active_samples[-1]
        if (
            last.lap_time_ms == current_sample.lap_time_ms
            and abs(last.normalized_distance - current_sample.normalized_distance) < 0.0001
        ):
            return [*self.active_samples[:-1], current_sample]
        return [*self.active_samples, current_sample]

    def _static_map_samples(self, reference: ReferenceProfile | None) -> tuple[str | None, list[LapSample]]:
        if self.external_reference is not None and self._has_world_positions(self.external_reference.samples):
            return self.external_reference.name, self.external_reference.samples
        if self.best_lap is not None and self._has_world_positions(self.best_lap.samples):
            return f"Best lap {self.best_lap.lap_num}", self.best_lap.samples
        if reference is not None and self._has_world_positions(reference.samples):
            return reference.name, reference.samples
        for lap in reversed(self.completed_laps):
            if not lap.invalid and self._has_world_positions(lap.samples):
                return f"Lap {lap.lap_num}", lap.samples
        return None, []

    @staticmethod
    def _has_world_positions(samples: list[LapSample]) -> bool:
        return any(sample.world_position is not None for sample in samples)

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
            "segments": [asdict(segment) for segment in reference.segments],
            "samples": [self._sample_to_dict(sample) for sample in self._samples_for_dashboard(reference.samples)],
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
            "ersDeployMode": sample.ers_deploy_mode,
            "ersDeployedThisLapJ": sample.ers_deployed_this_lap_j,
            "fuelKg": sample.fuel_kg,
            "lateralG": sample.lateral_g,
            "longitudinalG": sample.longitudinal_g,
            "avgSlipRatio": sample.avg_slip_ratio,
            "avgSlipAngle": sample.avg_slip_angle,
            "worldPosition": sample.world_position,
        }

    @staticmethod
    def _telemetry_to_dict(telemetry: CarTelemetrySnapshot | None) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "speedKmh": telemetry.speed_kmh,
            "throttle": telemetry.throttle,
            "brake": telemetry.brake,
            "steer": telemetry.steer,
            "gear": telemetry.gear,
            "engineRpm": telemetry.engine_rpm,
        }

    @staticmethod
    def _avg(values: Any) -> float:
        collected = list(values)
        return sum(collected) / len(collected) if collected else 0.0

    @staticmethod
    def _median(values: Any) -> float:
        collected = sorted(values)
        if not collected:
            return 0.0
        midpoint = len(collected) // 2
        if len(collected) % 2:
            return collected[midpoint]
        return (collected[midpoint - 1] + collected[midpoint]) / 2

    def _area_name(self, normalized_distance: float) -> str:
        reference = self.reference_profile()
        official_label = self._official_corner_label(normalized_distance)
        if official_label is not None:
            return f"{official_label} ({normalized_distance * 100:.1f}% lap)"
        corner_index = self._corner_index_at(normalized_distance, reference.samples if reference else self.active_samples)
        if corner_index is not None:
            return f"Corner {corner_index} ({normalized_distance * 100:.1f}% lap)"
        pct = normalized_distance * 100
        return f"Track zone {int(pct // 3.333) + 1} ({pct:.1f}% lap)"

    def _area_range(self, start: float, end: float) -> str:
        reference = self.reference_profile()
        samples = reference.samples if reference else self.active_samples
        context = self._dynamic_segment_context(start, end, [], [], samples)
        if context.corner_index is not None:
            return context.label
        return f"Zone {int(start * 30) + 1} ({start * 100:.0f}-{end * 100:.0f}% lap)"

    def _dynamic_segment_context(
        self,
        start: float,
        end: float,
        lap_segment: list[LapSample],
        ref_segment: list[LapSample],
        reference_samples: list[LapSample] | None = None,
    ) -> DynamicSegmentContext:
        samples = ref_segment or lap_segment
        reference_samples = reference_samples or ref_segment or lap_segment
        midpoint = (start + end) / 2
        corner_index = self._corner_index_at(midpoint, reference_samples)
        official_label = self._official_corner_label(midpoint)
        avg_speed = self._avg(sample.speed_kmh for sample in samples)
        min_speed = min((sample.speed_kmh for sample in samples), default=0)
        avg_brake = self._avg(sample.brake for sample in samples)
        avg_throttle = self._avg(sample.throttle for sample in samples)
        avg_steer = self._avg(abs(sample.steer) for sample in samples)
        speed_change = samples[-1].speed_kmh - samples[0].speed_kmh if len(samples) >= 2 else 0
        elevation_change = self._elevation_change(samples)
        slip_values = [sample.avg_slip_ratio for sample in lap_segment if sample.avg_slip_ratio is not None]
        avg_slip = self._avg(slip_values) if slip_values else None
        ers_used = self._ers_used(lap_segment)
        ers_used_kj = ers_used / 1000 if ers_used is not None else None
        straight_after = self._has_straight_after(end, reference_samples)

        if avg_brake > 0.18 or speed_change < -22:
            phase = "entry"
        elif avg_throttle > 0.58 and speed_change > 14:
            phase = "exit"
        elif avg_steer > 0.18:
            phase = "mid-corner"
        elif straight_after and avg_throttle > 0.75:
            phase = "straight"
        else:
            phase = "transition"

        corner_label = official_label or (f"Corner {corner_index}" if corner_index is not None else None)
        if corner_label is not None:
            label = f"{corner_label} {phase}" if phase != "straight" else f"Straight after {corner_label}"
        else:
            label = f"Zone {int(start * 30) + 1} {phase} ({start * 100:.0f}-{end * 100:.0f}% lap)"

        return DynamicSegmentContext(
            label=label,
            corner_index=corner_index,
            phase=phase,
            start_pct=start * 100,
            end_pct=end * 100,
            avg_speed_kmh=avg_speed,
            min_speed_kmh=min_speed,
            avg_brake=avg_brake,
            avg_throttle=avg_throttle,
            avg_steer=avg_steer,
            speed_change_kmh=speed_change,
            elevation_change_m=elevation_change,
            avg_slip_ratio=avg_slip,
            ers_used_kj=ers_used_kj,
            straight_after=straight_after,
        )

    def _live_dynamic_hint(self, sample: LapSample, hint: str) -> str | None:
        reference = self.reference_profile()
        context = self._dynamic_segment_context(
            max(0.0, sample.normalized_distance - 0.015),
            min(0.999, sample.normalized_distance + 0.015),
            [sample],
            [],
            reference.samples if reference else self.active_samples,
        )
        if context.corner_index is None:
            return None
        if "braking" in hint or sample.brake > 0.20:
            return "Try this: keep peak brake straighter, then release before adding more steering."
        if "traction" in hint or sample.throttle > 0.60:
            return "Try this: unwind steering first, then build throttle and ERS once the car is straightening."
        if abs(sample.steer) > 0.20:
            return "Try this: reduce the second steering input and let the car take a cleaner arc."
        return None

    def _dynamic_setup_hint(self, category: str, context: DynamicSegmentContext) -> str | None:
        downhill = context.elevation_change_m is not None and context.elevation_change_m < -0.5
        low_grip = context.avg_slip_ratio is not None and context.avg_slip_ratio > 0.26
        if category in {"braking", "under-braking"}:
            if downhill:
                return "If front locking repeats here, try one click rearward brake bias; if the rear moves on release, restore forward bias and soften the pedal release."
            return "Repeated locking here points first to brake-bias or release timing, not wing level."
        if category in {"traction", "throttle"} and low_grip:
            return "If this repeats with clean steering, test lower on-throttle diff or a slightly softer rear platform."
        if category == "steering" and context.avg_speed_kmh > 150:
            return "If the line is already clean, repeated steering scrub can justify more front support or lower front tyre pressure."
        return None

    def _dynamic_setup_suggestions(
        self,
        insights: list[SegmentInsight],
        lap: CompletedLap,
        reference: ReferenceProfile,
    ) -> list[str]:
        suggestions: list[str] = []
        seen: set[str] = set()
        for insight in insights[:4]:
            lap_segment = self._samples_between(lap.samples, insight.start_pct / 100, insight.end_pct / 100)
            ref_segment = self._samples_between(reference.samples, insight.start_pct / 100, insight.end_pct / 100)
            context = self._dynamic_segment_context(
                insight.start_pct / 100,
                insight.end_pct / 100,
                lap_segment,
                ref_segment,
                reference.samples,
            )
            hint = self._dynamic_setup_hint(insight.category, context)
            if hint is None or context.label in seen:
                continue
            suggestions.append(f"{context.label}: {hint}")
            seen.add(context.label)
        return suggestions[:2]

    def _corner_index_at(self, normalized_distance: float, samples: list[LapSample]) -> int | None:
        windows = self._dynamic_corner_windows(samples)
        if not windows:
            return None
        for index, (start, end) in enumerate(windows, start=1):
            if start <= normalized_distance <= end:
                return index
        nearest: tuple[float, int] | None = None
        for index, (start, end) in enumerate(windows, start=1):
            distance = min(abs(normalized_distance - start), abs(normalized_distance - end))
            if distance <= 0.025 and (nearest is None or distance < nearest[0]):
                nearest = (distance, index)
        return nearest[1] if nearest is not None else None

    def _dynamic_corner_windows(self, samples: list[LapSample]) -> list[tuple[float, float]]:
        ordered = sorted(samples, key=lambda sample: sample.normalized_distance)
        if len(ordered) < 2:
            return []
        groups: list[list[LapSample]] = []
        current: list[LapSample] = []
        previous_distance: float | None = None
        for sample in ordered:
            active = (
                sample.brake > 0.08
                or abs(sample.steer) > 0.15
                or (sample.speed_kmh < 210 and sample.throttle < 0.96)
            )
            if not active:
                if current:
                    groups.append(current)
                    current = []
                previous_distance = sample.normalized_distance
                continue
            if current and previous_distance is not None and sample.normalized_distance - previous_distance > 0.035:
                groups.append(current)
                current = []
            current.append(sample)
            previous_distance = sample.normalized_distance
        if current:
            groups.append(current)

        windows: list[tuple[float, float]] = []
        for group in groups:
            start = max(0.0, group[0].normalized_distance - 0.008)
            end = min(0.999, group[-1].normalized_distance + 0.008)
            max_steer = max(abs(sample.steer) for sample in group)
            max_brake = max(sample.brake for sample in group)
            min_speed = min(sample.speed_kmh for sample in group)
            if end - start < 0.010 and max_brake < 0.18 and max_steer < 0.22 and min_speed > 170:
                continue
            if windows and start - windows[-1][1] < 0.018:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        return windows

    def _has_straight_after(self, normalized_distance: float, samples: list[LapSample]) -> bool:
        after = [
            sample
            for sample in samples
            if normalized_distance < sample.normalized_distance <= min(0.999, normalized_distance + 0.08)
        ]
        if len(after) < 3:
            return False
        avg_throttle = self._avg(sample.throttle for sample in after)
        avg_brake = self._avg(sample.brake for sample in after)
        speed_gain = after[-1].speed_kmh - after[0].speed_kmh
        avg_steer = self._avg(abs(sample.steer) for sample in after)
        return avg_throttle > 0.75 and avg_brake < 0.08 and speed_gain > 20 and avg_steer < 0.16

    @staticmethod
    def _elevation_change(samples: list[LapSample]) -> float | None:
        positions = [sample.world_position for sample in samples if sample.world_position is not None]
        if len(positions) < 2:
            return None
        return positions[-1][1] - positions[0][1]

    def _begin_corner_metadata_load(self) -> None:
        begin_load = getattr(self.corner_metadata, "begin_load", None)
        if begin_load is None:
            return
        begin_load(self.track_id, self.track_length_m)

    def _official_corner_label(self, normalized_distance: float) -> str | None:
        corner_label = getattr(self.corner_metadata, "corner_label", None)
        if corner_label is None:
            return None
        return corner_label(self.track_id, normalized_distance, self.track_length_m)

    @staticmethod
    def _track_label(track_id: int | None) -> str:
        name = track_name_for_id(track_id)
        if name is not None:
            return name
        return f"track {track_id}" if track_id is not None else "unknown track"

    @staticmethod
    def _normalize_driving_goal(goal: str) -> str:
        normalized = goal.strip().lower().replace("_", "-")
        if normalized in {"qualifying", "quali", "hotlap", "hot-lap", "time-trial"}:
            return "qualifying"
        if normalized in {"race", "race-pace", "stint", "long-run", "longrun"}:
            return "race"
        raise ValueError("Driving goal must be 'qualifying' or 'race'.")

    @staticmethod
    def _format_ms(milliseconds: int) -> str:
        minutes, remainder = divmod(milliseconds, 60_000)
        seconds, ms = divmod(remainder, 1000)
        return f"{minutes}:{seconds:02d}.{ms:03d}"

    @staticmethod
    def _format_delta(milliseconds: int) -> str:
        return f"{milliseconds / 1000:.3f}s"

    @staticmethod
    def _format_kj(kj: float) -> str:
        if abs(kj) >= 1000:
            return f"{kj / 1000:.1f} MJ"
        return f"{kj:.0f} kJ"

    @staticmethod
    def _position_delta_label(delta: int) -> str:
        if delta > 0:
            return f"+{delta}"
        if delta < 0:
            return str(delta)
        return "0"

    @classmethod
    def _signed_delta(cls, milliseconds: int) -> str:
        sign = "+" if milliseconds >= 0 else "-"
        return f"{sign}{cls._format_delta(abs(milliseconds))}"
